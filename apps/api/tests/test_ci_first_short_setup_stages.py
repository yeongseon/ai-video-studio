"""The first-Short runtime job installs its dependencies in named, ordered, unconditional stages (#961).

A single combined install step hid which command consumed the time when the job once ran out
of its 25 minute budget: the REST API reports durations per step, not per command. Each stage
now has its own step, so the next stall is attributable, and no stage may become optional.
"""

from pathlib import Path
from typing import Final

import yaml

ROOT: Final = Path(__file__).resolve().parents[3]
JOB: Final = "first-short-runtime"
FIRST_CONSUMER: Final = "Start disposable loopback databases"
STAGES: Final = (
    "apt-get install -y ffmpeg nginx",
    "-r apps/api/requirements.txt -r apps/api/requirements-dev.txt "
    "-r apps/worker-orchestrator/requirements.txt -r apps/worker-orchestrator/requirements-dev.txt",
    "-e packages/creator-domain -e packages/creator-provider -e packages/creator-service",
    "python3 -m playwright install chromium",
    "npm --prefix apps/studio-web ci",
)


def _steps() -> list[dict[str, object]]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    return workflow["jobs"][JOB]["steps"]


def _stage_steps() -> list[int]:
    steps = _steps()
    positions = []
    for stage in STAGES:
        owners = [index for index, step in enumerate(steps) if stage in str(step.get("run", ""))]
        assert len(owners) == 1, f"{stage!r} must appear in exactly one step, found {owners}"
        positions.append(owners[0])
    return positions


def test_each_dependency_stage_has_its_own_step_in_order() -> None:
    # Given the real first-Short job.
    steps = _steps()

    # When locating the step that runs each dependency command.
    positions = _stage_steps()

    # Then the commands are in distinct steps, in the original order, before any consumer.
    assert len(set(positions)) == len(positions), positions
    assert positions == sorted(positions)
    consumer = next(index for index, step in enumerate(steps) if step.get("name") == FIRST_CONSUMER)
    assert positions[-1] < consumer


def test_dependency_stages_are_named_unconditional_and_fail_fast() -> None:
    # Given the steps that install dependencies.
    steps = _steps()

    for index in _stage_steps():
        step = steps[index]
        # Then none is anonymous, conditional, allowed to fail or masked with a shell fallback.
        assert step.get("name"), step
        assert "if" not in step, step
        assert "continue-on-error" not in step, step
        script = str(step["run"])
        assert "||" not in script and ";" not in script.replace("\\;", ""), script


def test_resolved_tool_versions_are_recorded_before_the_runtime_tests() -> None:
    # Given the job's steps up to the first consumer of the installed tools.
    steps = _steps()
    consumer = next(index for index, step in enumerate(steps) if step.get("name") == FIRST_CONSUMER)
    scripts = "\n".join(str(step.get("run", "")) for step in steps[:consumer])

    # Then the resolved runtime and media tool versions are visible in the log of every run.
    for command in ("python3 --version", "node --version", "npm --version", "ffmpeg -version", "nginx -v"):
        assert command in scripts, command
