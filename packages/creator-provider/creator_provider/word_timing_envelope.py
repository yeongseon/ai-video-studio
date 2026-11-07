from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Final, TypedDict

from creator_provider.exceptions import ProviderError

FORMAT: Final = "short-form-studio/tts-word-timings"
VERSION: Final = 2


class WordTimingRow(TypedDict):
    type: str
    offset: int
    duration: int
    text: str


class WordTimingEnvelope(TypedDict):
    format: str
    version: int
    audio_sha256: str
    script_sha256: str
    script_text: str
    words: list[WordTimingRow]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode())


def parse_sdk_rows(raw: str) -> list[WordTimingRow]:
    rows: list[WordTimingRow] = []
    for line in raw.splitlines():
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("type") != "WordBoundary":
            continue
        rows.append(
            {
                "type": "WordBoundary",
                "offset": int(value["offset"]),
                "duration": int(value["duration"]),
                "text": str(value["text"]),
            }
        )
    if not rows:
        raise ProviderError("No TTS word timing metadata found")
    return rows


def envelope(audio_path: Path, script: str, words: list[WordTimingRow]) -> WordTimingEnvelope:
    return {
        "format": FORMAT,
        "version": VERSION,
        "audio_sha256": sha256_bytes(audio_path.read_bytes()),
        "script_sha256": sha256_text(script),
        "script_text": script,
        "words": words,
    }


def read_envelope(
    path: Path,
    audio_path: Path,
    expected_script: str | None = None,
) -> WordTimingEnvelope:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError
        if value["format"] != FORMAT or value["version"] != VERSION:
            raise ValueError
        if value["audio_sha256"] != sha256_bytes(audio_path.read_bytes()):
            raise ProviderError("TTS word timing audio digest does not match audio")
        script_text = value["script_text"]
        if not isinstance(script_text, str) or value["script_sha256"] != sha256_text(script_text):
            raise ProviderError("TTS word timing script digest does not match persisted script")
        if expected_script is not None and script_text != expected_script:
            raise ProviderError("TTS word timing script digest does not match script")
        raw_rows = value["words"]
        if not isinstance(raw_rows, list):
            raise TypeError
        rows: list[WordTimingRow] = []
        previous_end = 0
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict) or raw_row.get("type") != "WordBoundary":
                raise TypeError
            offset = int(raw_row["offset"])
            duration = int(raw_row["duration"])
            text = raw_row["text"]
            if not isinstance(text, str) or not text or offset < previous_end or duration <= 0:
                raise ValueError
            rows.append(
                {
                    "type": "WordBoundary",
                    "offset": offset,
                    "duration": duration,
                    "text": text,
                }
            )
            previous_end = offset + duration
    except ProviderError:
        raise
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as exc:
        raise ProviderError(f"Invalid TTS word timing metadata: {path}") from exc
    if not rows:
        raise ProviderError(f"No TTS word timing metadata found in {path}")
    return {
        "format": FORMAT,
        "version": VERSION,
        "audio_sha256": value["audio_sha256"],
        "script_sha256": value["script_sha256"],
        "script_text": script_text,
        "words": rows,
    }


def read_bound_script(audio_path: Path) -> str:
    return read_envelope(Path(f"{audio_path}.words.jsonl"), audio_path)["script_text"]


def write_atomic(path: Path, value: WordTimingEnvelope) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(value, temporary, ensure_ascii=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
