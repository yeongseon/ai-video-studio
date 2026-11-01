"""Execute static Compose validation without relying on a developer's .env."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def smoke_checkout(tmp_path: Path) -> Path:
    files = (
        ".env.example",
        "docker-compose.yml",
        "scripts/quickstart_smoke.sh",
        "scripts/create_api_key.py",
        "apps/api/alembic.ini",
        "apps/studio-web/nginx.conf.template",
        "docs/QUICKSTART.md",
    )
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


def test_smoke_runs_in_a_fresh_checkout_without_creating_dotenv(smoke_checkout: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is needed for the executable config check")
    tmp_path = smoke_checkout

    result = subprocess.run(
        ["bash", "scripts/quickstart_smoke.sh"],
        cwd=tmp_path,
        env={"PATH": os.defpath, "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / ".env").exists()
    assert not list(tmp_path.glob("quickstart-smoke.*"))


@pytest.fixture
def shim_checkout(smoke_checkout: Path) -> Path:
    bin_dir = smoke_checkout / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in\n'
        '  *--quiet*) exit 0 ;;\n'
        '  *--profile*--services*) cat "$HOME/gpu-services"; exit "$(cat "$HOME/gpu-status")" ;;\n'
        '  *--services*) cat "$HOME/core-services"; exit "$(cat "$HOME/core-status")" ;;\n'
        '  *--format*) printf \'{"services":{"api":{"ports":[{"published":"8000",'
        '"host_ip":"127.0.0.1"}]}}}\\n\' ;;\n'
        '  *) exit 99 ;;\n'
        'esac\n'
    )
    docker.chmod(0o755)
    (smoke_checkout / "core-services").write_text("postgres\nredis\napi\nworker\nstudio-web\n")
    (smoke_checkout / "gpu-services").write_text("ollama\n")
    for profile in ("core", "gpu"):
        (smoke_checkout / f"{profile}-status").write_text("0")
    return smoke_checkout


def _run_shim_smoke(checkout: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/quickstart_smoke.sh"],
        cwd=checkout,
        env={
            "PATH": f"{checkout / 'bin'}:{os.defpath}",
            "HOME": str(checkout),
            "TMPDIR": str(checkout),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("profile", ["core", "gpu"])
def test_smoke_accepts_services_when_output_exceeds_pipe_capacity(
    shim_checkout: Path, profile: str,
) -> None:
    # Given: valid service output with trailing names larger than a pipe buffer.
    services = shim_checkout / f"{profile}-services"
    with services.open("a") as handle:
        handle.write("extra-service\n" * 100_000)

    # When: the actual smoke script checks the captured output.
    result = _run_shim_smoke(shim_checkout)

    # Then: a matching service never becomes a false failure from SIGPIPE.
    assert result.returncode == 0, result.stdout + result.stderr


def test_smoke_rejects_missing_api_even_when_similar_service_exists(shim_checkout: Path) -> None:
    # Given: a real missing service, not a producer failure.
    (shim_checkout / "core-services").write_text("postgres\nredis\napi-extra\nworker\nstudio-web\n")

    # When: checking exact service membership.
    result = _run_shim_smoke(shim_checkout)

    # Then: the CPU assertion still rejects the missing api.
    assert result.returncode == 1
    assert "SMOKE FAIL: CPU core service 'api' not defined" in result.stderr


def test_smoke_rejects_gpu_service_in_core_with_large_output(shim_checkout: Path) -> None:
    # Given: ollama erroneously starts without the GPU profile.
    with (shim_checkout / "core-services").open("a") as handle:
        handle.write("ollama\n" + "extra-service\n" * 100_000)

    # When: checking GPU exclusion.
    result = _run_shim_smoke(shim_checkout)

    # Then: early grep exit must not hide a forbidden service.
    assert result.returncode == 1
    assert "SMOKE FAIL: ollama must NOT start without --profile gpu" in result.stderr


@pytest.mark.parametrize("profile", ["core", "gpu"])
def test_smoke_rejects_failed_service_listing_with_valid_partial_output(
    shim_checkout: Path, profile: str,
) -> None:
    # Given: Compose emits matching names but exits unsuccessfully.
    (shim_checkout / f"{profile}-status").write_text("42")

    # When: running the smoke check.
    result = _run_shim_smoke(shim_checkout)

    # Then: partial output cannot turn a real Compose failure into success.
    assert result.returncode == 1
    assert f"SMOKE FAIL: docker compose {profile} service listing failed" in result.stderr


def test_smoke_rejects_missing_ollama_in_gpu_profile(shim_checkout: Path) -> None:
    # Given: the GPU profile does not define the local LLM service.
    (shim_checkout / "gpu-services").write_text("ollama-extra\n")

    # When: checking GPU membership.
    result = _run_shim_smoke(shim_checkout)

    # Then: exact matching still enforces the GPU service assertion.
    assert result.returncode == 1
    assert "SMOKE FAIL: ollama should exist under the gpu profile" in result.stderr
