from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Final, TypedDict

from creator_provider.exceptions import ProviderError

FORMAT: Final = "short-form-studio/tts-word-timings"
VERSION: Final = 1


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
        "words": words,
    }


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
