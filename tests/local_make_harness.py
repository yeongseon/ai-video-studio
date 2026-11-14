"""Run real Make recipes with a recording or config-only Docker stand-in."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
FAKE_DOCKER: Final = '#!/bin/sh\necho "bind=[$STUDIO_WEB_BIND]" >> "$FAKE_DOCKER_LOG"\n'


def _run_make(
    tmp_path: Path, target: str, *, docker_script: str = FAKE_DOCKER, **settings: str,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Execute the real recipe without starting services; settings are its environment."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(docker_script, encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker-calls.log"
    log.write_text("", encoding="utf-8")
    shutil.copyfile(ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/validate_local_bind.py", tmp_path / "scripts/validate_local_bind.py")
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "FAKE_DOCKER_LOG": str(log), **settings}
    result = subprocess.run(
        ["make", "-s", *target.split()], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    return result, log.read_text(encoding="utf-8")
