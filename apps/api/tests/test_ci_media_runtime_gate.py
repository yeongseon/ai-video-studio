"""The authorized-media runtime guard must execute in CI instead of skipping (#824)."""

import importlib
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Final, NamedTuple

import pytest
import yaml
from pydantic import BaseModel, Field

# Main pushes always run the job; a PR runs it only when the impact classifier selects it.
ALLOWED_JOB_CONDITION: Final = (
    "${{ github.event_name != 'pull_request' || needs.impact.outputs.first_short == 'true' }}"
)
ROOT: Final = Path(__file__).resolve().parents[3]
CI_WORKFLOW: Final = ROOT / ".github/workflows/ci.yml"
JOB: Final = "first-short-runtime"
GUARD: Final = "tests/integration/test_authorized_media_runtime.py"
SUPPORT: Final = "studio_proxy_runtime_support"
NARROWING_PREFIXES: Final = (
    "-k", "-m", "--deselect", "--ignore", "--collect", "-o", "--override-ini", "-c", "--config-file",
)
NARROWING_FLAGS: Final = frozenset({"--co"})
MASKING_TOKENS: Final = frozenset({"||", ";", "&"})
Environment = dict[str, str | int | bool]


class Step(BaseModel, frozen=True):
    run: str = ""
    env: Environment = Field(default_factory=dict)
    condition: str | bool = Field(default="", alias="if")
    continue_on_error: bool | str = Field(default=False, alias="continue-on-error")
    working_directory: str = Field(default="", alias="working-directory")


class Job(BaseModel, frozen=True):
    steps: tuple[Step, ...]
    env: Environment = Field(default_factory=dict)
    condition: str | bool = Field(default="", alias="if")
    continue_on_error: bool | str = Field(default=False, alias="continue-on-error")


class Workflow(BaseModel, frozen=True):
    jobs: dict[str, Job]
    env: Environment = Field(default_factory=dict)


class Command(NamedTuple):
    position: tuple[int, int]
    tokens: list[str]
    step: Step


def _commands(job: Job) -> list[Command]:
    return [
        Command((step_index, line_index), shlex.split(line, comments=True), step)
        for step_index, step in enumerate(job.steps)
        if step.condition == "" and step.continue_on_error is False
        for line_index, line in enumerate(step.run.replace("\\\n", " ").splitlines())
        if line.strip()
    ]


def _installs_nginx_and_ffmpeg(tokens: list[str]) -> bool:
    return {"apt-get", "install", "nginx", "ffmpeg"} <= set(tokens)


def _starts_disposable_postgres(tokens: list[str]) -> bool:
    return tokens[:2] == ["docker", "run"] and any(token.startswith("postgres:") for token in tokens)


def _builds_the_studio(tokens: list[str]) -> bool:
    build = ["npm", "--prefix", "apps/studio-web", "run", "build"]
    return any(tokens[index:index + len(build)] == build for index in range(len(tokens)))


PREREQUISITES: Final[dict[str, Callable[[list[str]], bool]]] = {
    "installing nginx and ffmpeg": _installs_nginx_and_ffmpeg,
    "starting the disposable PostgreSQL": _starts_disposable_postgres,
    "building the Studio": _builds_the_studio,
}


def violations(workflow_text: str, guard_module: str = GUARD) -> list[str]:
    workflow = Workflow.model_validate(yaml.safe_load(workflow_text))
    job = workflow.jobs[JOB]
    found: list[str] = []
    if job.condition not in ("", ALLOWED_JOB_CONDITION) or job.continue_on_error is not False:
        found.append(f"{JOB} must be unconditional on main (impact-gated on PRs only) and fail-fast")
    commands = _commands(job)
    environments = (workflow.env, job.env, *(step.env for step in job.steps))
    if any(name.startswith("PYTEST_") for env in environments for name in env) or any(
        token.startswith("PYTEST_") for command in commands for token in command.tokens
    ):
        found.append("PYTEST_* variables can narrow or silence the guard")
    guards = [command for command in commands if "pytest" in command.tokens and guard_module in command.tokens]
    if len(guards) != 1:
        return [*found, f"{JOB} must run {guard_module} exactly once, found {len(guards)}"]
    guard = guards[0]
    arguments = guard.tokens[guard.tokens.index("pytest") + 1:]
    if any(argument in NARROWING_FLAGS or argument.startswith(NARROWING_PREFIXES) for argument in arguments):
        found.append("the guard command is narrowed by a selection option")
    if MASKING_TOKENS & set(guard.tokens) or any(token.endswith(";") or "||" in token for token in guard.tokens):
        found.append("the guard command's failure is masked")
    if "PYTHONPATH=tests/integration" not in guard.tokens:
        found.append("PYTHONPATH=tests/integration is required to import the support module")
    if not guard.step.env.get("FIRST_SHORT_TEST_DATABASE_URL"):
        found.append("FIRST_SHORT_TEST_DATABASE_URL must be set on the guard's step")
    if guard.step.working_directory:
        found.append("the guard's step must run from the repository root")
    for name, provides in PREREQUISITES.items():
        setup = [command.position for command in commands if provides(command.tokens)]
        if not setup or min(setup) >= guard.position:
            found.append(f"{name} must run before the guard")
    return found


Edit = tuple[str, str]


class Break(NamedTuple):
    edits: tuple[Edit, ...]
    expected: str


def _edit(text: str, old: str, new: str) -> str:
    assert text.count(old) >= 1, f"the variant's target no longer matches ci.yml: {old[:70]!r}"
    return text.replace(old, new, 1)


def _guard_options(options: str) -> tuple[Edit, ...]:
    return ((f"{GUARD} -v", f"{GUARD} {options} -v"),)


def _in_guard_step(extra_line: str) -> tuple[Edit, ...]:
    return ((GUARD_STEP, GUARD_STEP + extra_line),)


RUNTIME_JOB = "  first-short-runtime:\n"
GUARD_STEP = "      - name: Verify durable assets and real first-video workflow\n"
REDIS_ENV = "          FIRST_SHORT_TEST_REDIS_URL: redis://127.0.0.1:56389/0\n"
DATABASE_ENV = (
    "          FIRST_SHORT_TEST_DATABASE_URL: postgresql://review:review-local-only@127.0.0.1:55439/first_short\n"
)
STUDIO_BUILD = "          STUDIO_BROWSER_TEST=true npm --prefix apps/studio-web run build\n"
LATER_SUITE = "          python3 -m pytest packages/creator-service/tests/test_spend_reservations.py"
GUARD_COMMAND_START = (
    "PYTHONPATH=tests/integration python3 -m pytest "
    "packages/creator-service/tests/test_postgres_media_asset_storage.py"
)
GUARD_COMMAND_END = "--basetemp=/tmp/first-short-runtime\n"
NEXT_COMMAND = (
    "          PYTHONPATH=tests/integration python3 -m pytest "
    "tests/integration/test_studio_upload_browser.py"
)
PUSH_ONLY = "        if: github.event_name == 'push'\n"
OTHER_DIRECTORY = "        working-directory: apps/api\n"
NARROWING_ENV = "          PYTEST_ADDOPTS: -k nothing\n"
EXPORTED_ADDOPTS = '          export PYTEST_ADDOPTS="-k nothing"\n'
BROKEN_WORKFLOWS: Final[dict[str, Break]] = {
    "guard module removed": Break(((" " + GUARD, ""),), "exactly once"),
    "guard deselected with -k": Break(_guard_options("-k nothing"), "narrowed"),
    "guard narrowed with an attached -k": Break(_guard_options("-knothing"), "narrowed"),
    "guard narrowed with -m=marker": Break(_guard_options("-m=smoke"), "narrowed"),
    "guard narrowed with --deselect=": Break(_guard_options(f"--deselect={GUARD}::test_x"), "narrowed"),
    "guard narrowed with --ignore=": Break(_guard_options(f"--ignore={GUARD}"), "narrowed"),
    "guard only collected": Break(_guard_options("--collect-only"), "narrowed"),
    "guard only collected with an abbreviation": Break(_guard_options("--collect"), "narrowed"),
    "pytest ini overridden with -o": Break(_guard_options("-o addopts=-knothing"), "narrowed"),
    "pytest config swapped with -c": Break(_guard_options("-c other.ini"), "narrowed"),
    "PYTEST_ADDOPTS set on the step": Break(((REDIS_ENV, REDIS_ENV + NARROWING_ENV),), "PYTEST_"),
    "PYTEST_ADDOPTS exported by the script": Break(((STUDIO_BUILD, STUDIO_BUILD + EXPORTED_ADDOPTS),), "PYTEST_"),
    "PYTEST_ADDOPTS given inline": Break(
        ((GUARD_COMMAND_START, 'PYTEST_ADDOPTS="-k nothing" ' + GUARD_COMMAND_START),), "PYTEST_",
    ),
    "failure masked by || true": Break(
        (
            (
                GUARD_COMMAND_END + NEXT_COMMAND,
                GUARD_COMMAND_END.rstrip("\n") + " || true\n" + NEXT_COMMAND,
            ),
        ),
        "masked",
    ),
    "PYTHONPATH=tests/integration removed": Break(
        ((GUARD_COMMAND_START, GUARD_COMMAND_START.removeprefix("PYTHONPATH=tests/integration ")),), "PYTHONPATH",
    ),
    "database url removed from the step": Break(((DATABASE_ENV, ""),), "FIRST_SHORT_TEST_DATABASE_URL"),
    "Studio build moved after the guard": Break(
        ((STUDIO_BUILD, ""), (LATER_SUITE, STUDIO_BUILD + LATER_SUITE)), "building the Studio",
    ),
    "nginx install removed": Break(
        (("install -y ffmpeg nginx", "install -y ffmpeg"),), "installing nginx and ffmpeg",
    ),
    "guard step made conditional": Break(_in_guard_step(PUSH_ONLY), "exactly once"),
    "guard step allowed to fail": Break(_in_guard_step("        continue-on-error: true\n"), "exactly once"),
    "whole job made conditional": Break(((f"    if: {ALLOWED_JOB_CONDITION}\n", "    " + PUSH_ONLY.lstrip()),), "unconditional"),
    "guard step run from another directory": Break(_in_guard_step(OTHER_DIRECTORY), "repository root"),
}


def test_runtime_job_executes_the_guard_without_narrowing_it() -> None:
    # Given the real workflow, whose runtime job provisions nginx, PostgreSQL and a built Studio.
    workflow = CI_WORKFLOW.read_text()

    # When checking every way the guard could silently stop running.
    found = violations(workflow)

    # Then none applies.
    assert found == []


TIMELINE_GUARD: Final = "tests/integration/test_timeline_preview_browser.py"


def test_runtime_job_executes_decoded_timeline_browser_guard() -> None:
    assert violations(CI_WORKFLOW.read_text(), TIMELINE_GUARD) == []


@pytest.mark.parametrize("option", ["removed", "-k nothing", "--collect-only", "|| true"])
def test_timeline_pin_detects_silent_browser_guard_removal(option: str) -> None:
    workflow = CI_WORKFLOW.read_text()
    replacement = "" if option == "removed" else f"{TIMELINE_GUARD} {option}"
    broken = _edit(workflow, TIMELINE_GUARD, replacement)
    assert violations(broken, TIMELINE_GUARD)


@pytest.mark.parametrize("name", BROKEN_WORKFLOWS)
def test_pin_detects_each_way_the_guard_could_stop_running(name: str) -> None:
    # Given the real workflow broken in one specific way.
    broken = CI_WORKFLOW.read_text()
    for old, new in BROKEN_WORKFLOWS[name].edits:
        broken = _edit(broken, old, new)

    # When checking it.
    found = violations(broken)

    # Then the break is reported, so the pin cannot pass while the guard is silently not run.
    assert any(BROKEN_WORKFLOWS[name].expected in violation for violation in found), found


def _support(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    monkeypatch.delitem(sys.modules, SUPPORT, raising=False)
    module = importlib.import_module(SUPPORT)
    monkeypatch.setitem(sys.modules, SUPPORT, module)
    monkeypatch.delenv("FIRST_SHORT_TEST_DATABASE_URL", raising=False)
    return module


class Outcome(NamedTuple):
    kind: str
    message: str
    value: Any


def _outcome(call: Callable[[], Any]) -> Outcome:
    try:
        value = call()
    except pytest.fail.Exception as failed:
        return Outcome("failed", str(failed), None)
    except pytest.skip.Exception as skipped:
        return Outcome("skipped", str(skipped), None)
    return Outcome("returned", "", value)


def test_guard_fails_instead_of_skipping_on_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a runner that lacks a prerequisite of the real runtime.
    support = _support(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    # When the guard asks for its prerequisites.
    outcome = _outcome(lambda: support.prerequisites_or_skip(required_in_ci=True))

    # Then it fails loudly, because a skip would turn the job green without running the guard.
    assert outcome.kind == "failed", outcome
    assert "PostgreSQL" in outcome.message


def test_guard_still_skips_outside_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a developer machine without the real runtime.
    support = _support(monkeypatch)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    # When the guard asks for its prerequisites.
    outcome = _outcome(lambda: support.prerequisites_or_skip(required_in_ci=True))

    # Then it skips instead of failing.
    assert outcome.kind == "skipped", outcome


def test_guard_fails_on_github_actions_when_the_studio_is_not_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Given a runner with nginx, ffmpeg and a database, but no built Studio.
    support = _support(monkeypatch)
    monkeypatch.setattr(support.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("FIRST_SHORT_TEST_DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setattr(support, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    # When the guard asks for its prerequisites.
    outcome = _outcome(lambda: support.prerequisites_or_skip(required_in_ci=True))

    # Then it fails loudly here too, because the dist check is a second place where it could skip.
    assert outcome.kind == "failed", outcome
    assert "Studio" in outcome.message


def test_prerequisites_are_returned_when_the_runtime_can_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Given nginx, ffmpeg, a database and a built Studio on a GitHub Actions runner.
    support = _support(monkeypatch)
    monkeypatch.setattr(support.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("FIRST_SHORT_TEST_DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setattr(support, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    dist = tmp_path / "apps/studio-web/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>")

    # When the guard asks for its prerequisites.
    outcome = _outcome(lambda: support.prerequisites_or_skip(required_in_ci=True))

    # Then it neither skips nor fails, so the strict rule cannot turn a healthy runner red.
    assert outcome.kind == "returned", outcome
    needs = outcome.value
    assert (needs.nginx, needs.database_url, needs.studio_dist) == (
        "/usr/bin/nginx", "postgresql://example.invalid/db", dist,
    )


def test_other_runtime_tests_keep_skipping_on_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the same lacking runner, and tests that did not opt in to the stricter rule.
    support = _support(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    # When they ask for the prerequisites.
    outcome = _outcome(support.prerequisites_or_skip)

    # Then their behavior is unchanged.
    assert outcome.kind == "skipped", outcome
