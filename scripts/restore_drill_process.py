from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final


TIMEOUT = 120
# Executable discovery, user tool configuration, locale and temporary-file placement
# are the only inherited inputs. Application configuration and credentials are
# explicit overrides; in particular PYTHONPATH is always chosen by the drill.
CHILD_ENV_ALLOWLIST: Final = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES",
    "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "TMPDIR", "VIRTUAL_ENV",
})
DOCKER_ENV_ALLOWLIST: Final = frozenset({
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR",
})


@dataclass(frozen=True, slots=True)
class DrillError(RuntimeError):
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class ContainerNameCollisionError(DrillError):
    pass


def child_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build every drill subprocess environment from safe host inputs and owned values."""
    return {
        **{name: value for name, value in os.environ.items() if name in CHILD_ENV_ALLOWLIST},
        "REDIS_URL": "",
        **(overrides or {}),
    }


def run(*command: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=child_environment(env), capture_output=True, text=True, check=False, timeout=TIMEOUT)
    if result.returncode != 0:
        raise DrillError(f"command failed: {' '.join(command)}\n{result.stderr}")
    return result


def docker_environment() -> dict[str, str]:
    """Preserve daemon selection only for the Docker CLI, never app children."""
    return child_environment({
        name: value for name, value in os.environ.items() if name in DOCKER_ENV_ALLOWLIST
    })


def docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return run("docker", *arguments, env=docker_environment())
