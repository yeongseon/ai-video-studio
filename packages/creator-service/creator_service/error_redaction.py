"""Bound untrusted diagnostics and convert them to redacted JSON primitives."""

from __future__ import annotations

import math
import re
from itertools import islice
from typing import Final, TypeAlias

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

_ABS_POSIX_PATH = re.compile(r"(?:/[\w.\-]+){2,}/?")
_WORKSPACE_PATH = re.compile(r"\bworkspaces/[\w./\-]+")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[\\\w.\- ]+")
_URL = re.compile(r"\bhttps?://[^\s'\"]+")
_CREDENTIAL_URI = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/'\"<>]*@[^\s;'\"<>]+")
_BEARER = re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE)
_TOKEN_PREFIX = re.compile(r"\b(?:(?:sk|gsk|ghp|xoxb)[-_]|AKIA|AIza)[A-Za-z0-9_\-]{6,}")
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{20,}\b")
_REDACTED: Final = "<redacted>"
_MAX_STRING: Final = 1024
_MAX_INPUT: Final = 8192
_MAX_ITEMS: Final = 50
_MAX_NODES: Final = 1000
_SENSITIVE_KEYS: Final = frozenset({"key", "secret", "token", "password", "credential"})
_LINE_BREAK = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_LOG_SEGMENTS = re.compile(r"[_\-. ]+")
_LOG_SECRETS: Final = frozenset({
    "secret", "token", "password", "passwd", "pwd", "passphrase", "credential", "credentials",
    "authorization", "bearer", "cookie", "apikey",
})
_LOG_KEY_PREFIXES: Final = frozenset({"api", "private", "access", "secret", "signing", "session"})
_MAX_LOG_LINES: Final = 400
_MAX_LOG_BYTES: Final = 65536
_MAX_LOG_LINE: Final = 2048
# One attempt per scheme-character run, including punctuation before a valid scheme.
# The API/DLQ patterns above deliberately retain their existing behavior.
_LOG_CREDENTIAL_URI = re.compile(
    r"(?<![A-Za-z0-9+.-])[A-Za-z0-9+.-]+://[^\s/'\"<>]*@[^\s;'\"<>]+"
)
_LOG_TOKEN_PREFIX = re.compile(
    r"(?<![^\W_])(?:(?:sk|gsk|ghp|xoxb)[-_]|AKIA|AIza)[A-Za-z0-9_\-]{6,}"
)
_LOG_CREDENTIAL_PATTERNS: Final = (
    _LOG_CREDENTIAL_URI, _URL, _BEARER, _LOG_TOKEN_PREFIX, _LONG_HEX,
)
# Consume the entire path once, including preserved routes: never restart inside a
# route at /workspaces or another suffix. Legacy artifact routes remain filesystem paths.
_LOG_PATH = re.compile(r"(?:/[\w.\-]+){2,}/?|\bworkspaces/[\w./\-]+")
_LOG_ROUTE_PREFIXES: Final = ("/api/creator/", "/api/admin/", "/release/")
_LOG_ROUTE_PATHS: Final = frozenset({"/api/creator", "/api/admin", "/docs/oauth2-redirect"})


def _redact_log_path(match: re.Match[str]) -> str:
    path = match.group()
    if path.startswith(_LOG_ROUTE_PREFIXES) or path in _LOG_ROUTE_PATHS:
        return _LONG_HEX.sub(_REDACTED, _LOG_TOKEN_PREFIX.sub(_REDACTED, path))
    return _REDACTED


def redact_error_message(text: str) -> str:
    # Reject oversized text before regex processing; truncating first can split a secret.
    if len(text) > _MAX_INPUT:
        return _REDACTED
    redacted = _CREDENTIAL_URI.sub(_REDACTED, text)
    redacted = _URL.sub(_REDACTED, redacted)
    redacted = _BEARER.sub(_REDACTED, redacted)
    redacted = _TOKEN_PREFIX.sub(_REDACTED, redacted)
    redacted = _WINDOWS_PATH.sub(_REDACTED, redacted)
    redacted = _WORKSPACE_PATH.sub(_REDACTED, redacted)
    redacted = _ABS_POSIX_PATH.sub(_REDACTED, redacted)
    return _LONG_HEX.sub(_REDACTED, redacted)[:_MAX_STRING]


def redact_log_text(text: str, trusted_lines: frozenset[str] = frozenset()) -> str:
    """Redact every visible line of ``text`` independently, within a fixed output budget.

    Any Unicode line break starts a new line, so a separator cannot hide content on a line
    that is kept as it is. ``trusted_lines`` are exact strings known from program metadata
    (traceback frame headers rebuilt from the live exception): only an exact match is kept
    verbatim, because text that merely looks like one is as untrusted as any other.

    The input is scanned lazily and an over-long line is never sliced, so the work and the
    allocation stay bounded however large the text is.
    """
    lines: list[str] = []
    size = 0
    start = 0
    truncated = False
    for _ in range(_MAX_LOG_LINES):
        match = _LINE_BREAK.search(text, start)
        end = match.start() if match else len(text)
        if end - start > _MAX_LOG_LINE:
            safe = _REDACTED
        else:
            line = text[start:end]
            if line in trusted_lines:
                safe = line
            else:
                safe = line
                for pattern in (_LOG_CREDENTIAL_URI, _URL, _BEARER, _WINDOWS_PATH):
                    safe = pattern.sub(_REDACTED, safe)
                safe = _LOG_PATH.sub(_redact_log_path, safe)
                safe = _LOG_TOKEN_PREFIX.sub(_REDACTED, safe)
                safe = _LONG_HEX.sub(_REDACTED, safe)
                safe = safe[:_MAX_STRING]
        line_size = len(safe.encode("utf-8", errors="replace")) + 1
        if size + line_size > _MAX_LOG_BYTES - 64:
            truncated = True
            break
        lines.append(safe)
        size += line_size
        if match is None:
            break
        start = match.end()
    else:
        truncated = True
    if truncated:
        lines.append("<output truncated>")
    return "\n".join(lines)


def carries_credential(text: str) -> bool:
    """True when frame metadata holds a credential shape recognized at the log boundary.

    Text too long to scan counts as carrying one, matching the log-line refusal.
    """
    return len(text) > _MAX_LOG_LINE or any(
        pattern.search(text)
        for pattern in _LOG_CREDENTIAL_PATTERNS
    )


def is_sensitive_log_key(name: str) -> bool:
    """Recognize credential labels without hiding key identifiers or token counts."""
    lower = name.lower()
    if lower == "token_count":
        return False
    if lower in {"key", "apikey", "x-api-key"}:
        return True
    segments = _LOG_SEGMENTS.split(_CAMEL_BOUNDARY.sub("_", name).lower())
    return any(segment in _LOG_SECRETS for segment in segments) or any(
        first in _LOG_KEY_PREFIXES and second == "key"
        for first, second in zip(segments, segments[1:])
    )


def sanitize_error_json(data: object, depth: int = 0) -> JsonValue:
    """Accept arbitrary boundary input, never invoking object stringification hooks.

    Only exact built-in containers/primitives are traversed. Unsupported objects
    (including subclasses with custom hooks) become opaque. A global node budget
    bounds branching as well as depth; cycles terminate at the same depth limit.
    """
    remaining = _MAX_NODES

    def visit(value: object, level: int) -> JsonValue:
        nonlocal remaining
        if level > 10:
            return "<nested>"
        if remaining <= 0:
            return "<truncated>"
        remaining -= 1
        match value:
            case None | bool():
                return value
            case str() if type(value) is str:
                return redact_error_message(value)
            case int() if type(value) is int:
                return value if value.bit_length() <= 256 else "<unsupported>"
            case float() if type(value) is float:
                return value if math.isfinite(value) else "<unsupported>"
            case dict() if type(value) is dict:
                result: dict[str, JsonValue] = {}
                for key, item in islice(value.items(), _MAX_ITEMS):
                    if remaining <= 0:
                        break
                    # Non-string keys are omitted, never passed to str/repr or JSON.
                    if not isinstance(key, str) or type(key) is not str:
                        continue
                    safe_key = redact_error_message(key)
                    sensitive = any(part in key.lower() for part in _SENSITIVE_KEYS)
                    result[safe_key] = _REDACTED if sensitive else visit(item, level + 1)
                return result
            case list() | tuple() if type(value) in (list, tuple):
                return [visit(item, level + 1) for item in value[:_MAX_ITEMS] if remaining > 0]
            case _:
                return "<unsupported>"

    return visit(data, depth)
