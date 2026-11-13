import shlex
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, Field


class Step(BaseModel, frozen=True):
    run: str = ""
    condition: str | bool = Field(default="", alias="if")
    continue_on_error: bool | str = Field(default=False, alias="continue-on-error")


class Job(BaseModel, frozen=True):
    steps: tuple[Step, ...]


class Workflow(BaseModel, frozen=True):
    jobs: dict[str, Job]


@pytest.mark.parametrize(("job", "test_command"), [
    ("test", "python3 -m pytest packages/creator-domain/tests/"),
    ("test", "python3 -m pytest packages/creator-provider/tests/"),
    ("test", "python3 -m pytest packages/creator-service/tests/"),
    ("test", "cd apps/api && python3 -m pytest tests/"),
    ("test-worker", "cd apps/worker-orchestrator && python3 -m pytest tests/"),
])
def test_host_media_prerequisites_precede_test_execution(job: str, test_command: str) -> None:
    # Given the actual job that runs this suite; installs in Docker/runtime jobs do not count.
    root = Path(__file__).resolve().parents[3]
    workflow = Workflow.model_validate(
        yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    )
    steps = workflow.jobs[job].steps

    # When locating executable commands in unconditional, fail-fast setup steps.
    commands = [
        (step_index, shlex.split(line, comments=True))
        for step_index, step in enumerate(steps)
        if step.condition == "" and step.continue_on_error is False
        for line in step.run.splitlines()
    ]
    required = [
        ["sudo", "apt-get", "update"],
        ["sudo", "apt-get", "install", "-y", "ffmpeg"],
        ["ffmpeg", "-version"],
        ["ffprobe", "-version"],
    ]
    positions = []
    for command in required:
        matches = [index for index, (_, tokens) in enumerate(commands) if tokens == command]
        assert matches, f"{job} job must execute {shlex.join(command)}"
        positions.append(matches[0])
    test_steps = [index for index, step in enumerate(steps) if test_command in step.run]

    # Then update/install/version checks run in order before every real test suite.
    assert positions == sorted(positions)
    assert test_steps, f"{job} job must retain {test_command}"
    assert commands[positions[-1]][0] < min(test_steps)
