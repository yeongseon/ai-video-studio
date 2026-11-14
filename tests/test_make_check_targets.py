"""Exercise local check dispatch with isolated, recording toolchain stand-ins."""

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
SUITES: Final = ("api", "worker", "creator-domain", "creator-provider", "creator-service", "studio")
FULL_TARGETS: Final = ("lint", "test", "build", "check")
FAKE_COMMAND: Final = """#!/bin/sh
name=${0##*/}
if [ "$*" = '-c import pytest' ]; then
    exit "${PYTEST_IMPORT_EXIT:-0}"
fi
call="${PWD#"$FIXTURE_ROOT"}|$name $*"
printf '%s\\n' "$call" >> "$CALL_LOG"
if [ "$call" = "$FAIL_CALL" ]; then exit 19; fi
exit 0
"""


@dataclass(frozen=True, slots=True)
class Checkout:
    root: Path

    def run(self, *args: str, fail_call: str = "", pytest_exit: str = "0") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/make", "-s", "-C", str(self.root), *args],
            env={
                "PATH": str(self.root / "bin"),
                "FIXTURE_ROOT": str(self.root),
                "CALL_LOG": str(self.root / "calls"),
                "FAIL_CALL": fail_call,
                "PYTEST_IMPORT_EXIT": pytest_exit,
            },
            capture_output=True, text=True, timeout=10, check=False,
        )

    def calls(self) -> list[str]:
        return (self.root / "calls").read_text().splitlines()

    def install_web(self) -> None:
        (self.root / "apps/studio-web/node_modules").mkdir()


@pytest.fixture
def checkout(tmp_path: Path) -> Checkout:
    (tmp_path / "Makefile").write_text((ROOT / "Makefile").read_text())
    for path in ("apps/api", "apps/worker-orchestrator", "apps/studio-web", "packages", "bin"):
        (tmp_path / path).mkdir(parents=True, exist_ok=True)
    for name in ("python", "python3", "npm", "node", "ruff", "pytest", "basedpyright", "eslint", "npx"):
        command = tmp_path / "bin" / name
        command.write_text(FAKE_COMMAND)
        command.chmod(0o755)
    # Only make and fake tools are on PATH, including inside recursive make recipes.
    (tmp_path / "bin/make").symlink_to("/usr/bin/make")
    (tmp_path / "calls").write_text("")
    return Checkout(tmp_path)


def _suite_calls() -> list[str]:
    return [
        "/apps/api|python3 -m pytest tests/ -v",
        "/apps/worker-orchestrator|python3 -m pytest tests/ -v",
        *[f"|python3 -m pytest packages/{name}/tests/ -v" for name in SUITES[2:5]],
        "|npm --prefix apps/studio-web run test",
    ]


@pytest.mark.parametrize("target", FULL_TARGETS)
def test_full_checks_fail_before_work_when_web_is_absent(checkout: Checkout, target: str) -> None:
    # Given an installed Python toolchain but no Studio dependencies.
    # When selecting a full check.
    result = checkout.run(target)
    # Then fail with the locked recovery command before any check executes.
    assert result.returncode != 0, result.stdout
    assert "npm --prefix apps/studio-web ci" in result.stdout + result.stderr
    assert checkout.calls() == []


@pytest.mark.parametrize("target", ("lint-python", "test-python"))
def test_python_checks_work_when_web_is_absent(checkout: Checkout, target: str) -> None:
    # Given no Studio dependencies.
    # When explicitly selecting a Python-only loop.
    result = checkout.run(target)
    # Then run Python checks, never npm.
    assert result.returncode == 0, result.stderr
    expected = _suite_calls()[:-1] if target.startswith("test") else [
        "|ruff check apps packages",
        "|python3 -m compileall apps packages",
        "|basedpyright apps packages",
    ]
    assert checkout.calls() == expected


def test_full_test_dispatch_matches_declared_inventory(checkout: Checkout) -> None:
    # Given all toolchains and the inventory as evaluated by make itself.
    checkout.install_web()
    inventory = checkout.run("--eval=print-suites: ; @:; echo $(TEST_SUITES)", "print-suites")
    assert inventory.returncode == 0, inventory.stderr
    assert inventory.stdout.split() == list(SUITES)
    # When running the full test target.
    result = checkout.run("test")
    # Then every declared family executes exactly once in order, in separate processes.
    assert result.returncode == 0, result.stderr
    assert list(zip(inventory.stdout.split(), checkout.calls(), strict=True)) == list(
        zip(SUITES, _suite_calls(), strict=True)
    )


@pytest.mark.parametrize("failed_suite", SUITES)
def test_full_test_fails_fast_when_a_suite_fails(checkout: Checkout, failed_suite: str) -> None:
    # Given any one failing family, including package families omitted by the old target.
    checkout.install_web()
    index = SUITES.index(failed_suite)
    # When its fake runner fails.
    result = checkout.run("test", fail_call=_suite_calls()[index])
    # Then failure reaches make and later families never execute.
    assert result.returncode != 0
    assert checkout.calls() == _suite_calls()[:index + 1]


@pytest.mark.parametrize("target", FULL_TARGETS)
def test_full_recipes_have_no_skip_branch(checkout: Checkout, target: str) -> None:
    # Given the complete expanded recipe, without executing any tools.
    # When inspecting dispatch.
    result = checkout.run("-n", target)
    # Then none of its branches silently omit required work.
    assert result.returncode == 0, result.stderr
    assert "Skipping" not in result.stdout


@pytest.mark.parametrize("tool", ("python3", "ruff", "basedpyright"))
def test_full_lint_explains_missing_python_tools(checkout: Checkout, tool: str) -> None:
    # Given a missing required Python executable in an otherwise installed checkout.
    checkout.install_web()
    (checkout.root / "bin" / tool).unlink()
    # When selecting full lint.
    result = checkout.run("lint")
    # Then fail before checks with the Backend dependency installation command.
    assert result.returncode != 0
    assert tool in result.stdout + result.stderr
    assert "requirements-dev.txt" in result.stdout + result.stderr
    assert "python3 -m pip install" in result.stdout + result.stderr
    assert checkout.calls() == []


def test_full_test_explains_missing_pytest_module(checkout: Checkout) -> None:
    # Given a Python executable whose environment has no pytest module.
    checkout.install_web()
    # When selecting full test.
    result = checkout.run("test", pytest_exit="1")
    # Then provide recovery instructions rather than beginning any suite.
    assert result.returncode != 0
    assert "pytest" in result.stdout + result.stderr
    assert "requirements-dev.txt" in result.stdout + result.stderr
    assert checkout.calls() == []


@pytest.mark.parametrize("target", ("lint-web", "test-web", "build-web"))
def test_web_checks_run_only_their_family(checkout: Checkout, target: str) -> None:
    # Given Studio dependencies but no Python executable.
    checkout.install_web()
    (checkout.root / "bin/python3").unlink()
    # When explicitly selecting a frontend loop.
    result = checkout.run(target)
    # Then only the requested frontend commands execute.
    assert result.returncode == 0, result.stderr
    expected = [f"|npm --prefix apps/studio-web run {target.removesuffix('-web')}"]
    if target.startswith("lint"):
        expected.append("/apps/studio-web|npx --no-install tsc --noEmit")
    assert checkout.calls() == expected


def test_check_runs_full_phases_in_order_even_when_parallel(checkout: Checkout) -> None:
    # Given a fully installed checkout and parallel make enabled.
    checkout.install_web()
    # When requesting the aggregate check.
    result = checkout.run("-j4", "check")
    # Then lint, test and build stay ordered and include both typecheckers.
    assert result.returncode == 0, result.stderr
    assert checkout.calls() == [
        "|ruff check apps packages",
        "|python3 -m compileall apps packages",
        "|basedpyright apps packages",
        "|npm --prefix apps/studio-web run lint",
        "/apps/studio-web|npx --no-install tsc --noEmit",
        *_suite_calls(),
        "|python3 -m compileall apps packages",
        "|npm --prefix apps/studio-web run build",
    ]
