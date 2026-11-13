"""Studio's Node.js runtime is one supported LTS line, pinned identically everywhere (#996).

Node 20 reached end of life in April 2026. The version lives in `.node-version`; the Docker build
and dev stages and every CI job must agree with it, so a drifted image or an inline version in
a workflow step fails here instead of silently testing an unsupported runtime.
"""

from pathlib import Path
import re
from typing import Final

import yaml

ROOT: Final = Path(__file__).resolve().parents[3]
# Even-numbered LTS lines still receiving security fixes (https://nodejs.org/en/about/previous-releases).
# Node 20 is end of life, and odd-numbered lines never become LTS.
SUPPORTED_MAJORS: Final = frozenset({22, 24, 26})
EXACT_VERSION: Final = re.compile(r"(?P<major>\d+)\.\d+\.\d+")
NODE_IMAGE: Final = re.compile(r"node:(?P<version>\d+\.\d+\.\d+)-bookworm-slim@sha256:(?P<digest>[0-9a-f]{64})")
NODE_JOBS: Final = ("lint", "test", "first-short-runtime")


def _pinned_version() -> str:
    version = (ROOT / ".node-version").read_text(encoding="utf-8").strip()
    assert EXACT_VERSION.fullmatch(version), f".node-version must be an exact release, got {version!r}"
    return version


def test_node_version_file_pins_an_exact_supported_lts_release() -> None:
    # Given the single source of truth for the Node version.
    version = _pinned_version()

    # Then it is an exact release of a supported even-numbered LTS line.
    match = EXACT_VERSION.fullmatch(version)
    assert match is not None
    assert int(match["major"]) in SUPPORTED_MAJORS, f"Node {match['major']} is end of life or not an LTS line"


def test_dockerfile_build_and_dev_stages_use_one_pinned_digest_image() -> None:
    # Given the Studio Dockerfile.
    lines = (ROOT / "apps/studio-web/Dockerfile").read_text(encoding="utf-8").splitlines()

    # When reading the single Node image definition and every stage that starts from Node.
    arguments = [line for line in lines if line.startswith("ARG NODE_IMAGE=")]
    first_from = next(index for index, line in enumerate(lines) if line.startswith("FROM "))
    node_stages = [line for line in lines if line.startswith("FROM ${NODE_IMAGE} AS ")]

    # Then both stages share one exact-patch, Debian-variant, digest-pinned reference that
    # agrees with .node-version, and nothing starts from a floating Node tag.
    assert len(arguments) == 1 and lines.index(arguments[0]) < first_from, arguments
    pinned = NODE_IMAGE.fullmatch(arguments[0].removeprefix("ARG NODE_IMAGE="))
    assert pinned, arguments[0]
    assert pinned["version"] == _pinned_version()
    assert node_stages == ["FROM ${NODE_IMAGE} AS build", "FROM ${NODE_IMAGE} AS dev"], node_stages
    assert not [line for line in lines if re.match(r"FROM\s+node[:@]", line)], "floating Node tag"


def test_every_setup_node_step_reads_the_version_file() -> None:
    # Given the CI workflow.
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]

    for name in NODE_JOBS:
        # When locating the Node setup steps of each job that runs frontend tooling.
        setups = [step for step in jobs[name]["steps"] if str(step.get("uses", "")).startswith("actions/setup-node@")]

        # Then each reads the shared file, with no inline version that could drift.
        assert setups, f"{name} must set up Node"
        for step in setups:
            assert step["with"] == {"node-version-file": ".node-version"}, (name, step)
