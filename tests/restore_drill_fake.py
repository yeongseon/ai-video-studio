"""Subprocess-boundary sandbox for the restore drill; never invokes Docker."""
from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, TypedDict, Unpack


class ProcessOptions(TypedDict, total=False):
    cwd: Path | None
    env: Mapping[str, str] | None
    capture_output: bool
    text: bool
    check: bool
    timeout: int
    stdout: IO[bytes]
    stderr: int


class FakeProcess:
    def terminate(self) -> None:
        return

    def kill(self) -> None:
        return

    def wait(self, timeout: float | None = None) -> int:
        return 0


class DrillSandbox:
    """Record process environments while simulating disposable external tools."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []
        self.existing = ""
        self.failed_start = ""
        self.created_then_failed = ""
        self.start_failure: Exception | KeyboardInterrupt | None = None
        self.cleanup_failure: Exception | None = None
        self.containers: dict[str, tuple[str, set[str]]] = {}
        self.media: Path | None = None

    def popen(self, command: Sequence[str], **options: Unpack[ProcessOptions]) -> FakeProcess:
        self.calls.append((tuple(command), options.get("env")))
        return FakeProcess()

    def run(self, command: Sequence[str], **options: Unpack[ProcessOptions]) -> subprocess.CompletedProcess[str]:
        args = tuple(command)
        self.calls.append((args, options.get("env")))
        output = ""
        code = 0
        if args[0] == "docker":
            if args[1] == "ps":
                name = args[args.index("--filter") + 1].removeprefix("name=^/").removesuffix("$")
                if "-aq" in args:
                    if self.cleanup_failure is not None:
                        raise self.cleanup_failure
                    label = args[-1].removeprefix("label=")
                    container = self.containers.get(name)
                    output = container[0] if container and label in container[1] else ""
                else:
                    output = name if name.startswith(self.existing) and self.existing else ""
            elif args[1] == "run" and "--name" in args:
                name = args[args.index("--name") + 1]
                code = int(bool(self.failed_start and name.startswith(self.failed_start)))
                labels = {args[index + 1] for index, value in enumerate(args) if value == "--label"}
                if code:
                    self.containers[name] = (name, set())
                else:
                    self.containers[name] = (f"id-{name}", labels)
                if self.created_then_failed and name.startswith(self.created_then_failed):
                    if self.start_failure is not None:
                        raise self.start_failure
                    code = 1
            elif args[1] == "rm":
                self.containers = {
                    name: container for name, container in self.containers.items()
                    if name not in args[3:] and container[0] not in args[3:]
                }
            elif args[1] == "run" and "-v" in args:
                self.media = Path(args[args.index("-v") + 1].split(":")[0])
                (self.media / args[-1].split("/")[-1]).write_bytes(b"synthetic media")
            elif args[1] == "port":
                output = "127.0.0.1:" + ("15432" if args[2].startswith("restore-source-") else "25432")
            elif args[1:3] == ("image", "inspect"):
                output = "registry/image@sha256:" + "0123456789abcdef" * 4
            elif args[1] == "cp" and args[2].endswith(":/tmp/database.dump"):
                Path(args[3]).write_bytes(b"synthetic dump")
            elif "SELECT segments_json" in args[-1]:
                output = '[{"id": "first"}, {"id": "second"}]'
            elif "SELECT celery_task_id" in args[-1]:
                output = "restore-pending=failed\nrestore-queued=queued\nrestore-running=failed"
        elif "alembic" in args:
            output = "revision-1 (head)"
        elif args[0] == "git":
            output = "abc123def456"
        elif "restore-media" in args:
            assert self.media is not None
            shutil.copytree(self.media, Path(args[args.index("--target") + 1]))
        elif "-c" in args:
            program = args[args.index("-c") + 1]
            if "render_saved_timeline" in program:
                environment = options.get("env")
                assert environment is not None
                restored = Path(environment["ARTIFACT_ROOT"])
                (restored / "final.mp4").write_bytes(b"rendered")
                output = json.dumps({"count": 2, "artifact_id": 702, "audio_path": str(restored / "audio.wav")})
            elif "reconcile_stale_dispatches" in program:
                output = '{"rolled_back": 1, "stuck_failed": 1, "errors": []}'
        return subprocess.CompletedProcess(args, code, output, "synthetic start failure" if code else "")

    def docker_calls(self, operation: str) -> list[tuple[str, ...]]:
        return [command for command, _ in self.calls if command[:2] == ("docker", operation)]
