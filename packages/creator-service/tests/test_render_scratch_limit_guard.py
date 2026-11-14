import ast
from pathlib import Path

import pytest

LAUNCHERS = {
    "subprocess": {"Popen", "run", "call", "check_call", "check_output"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "os": {"system", "popen"},
}
ALLOWED = {
    ("ffmpeg_service.py", "_run_ffmpeg", "Popen"),
    ("ffmpeg_service.py", "get_audio_duration", "run"),
    ("render_segments.py", "_probe_source_duration", "run"),
}


def launches(owner: str, attribute: str) -> bool:
    return attribute in LAUNCHERS.get(owner, ()) or (owner == "os" and attribute.startswith("spawn"))


def bypasses(source: str, filename: str, allowed: set[tuple[str, str, str]]) -> list[str]:
    """Report process launches outside the allowed call sites, including imported and aliased forms."""
    tree = ast.parse(source)
    modules = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    violations = [
        f"{filename}:from-{node.module}-import-{alias.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module in LAUNCHERS
        for alias in node.names if launches(node.module, alias.name)
    ]
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = modules.get(ast.unparse(node.func.value), ast.unparse(node.func.value))
                if launches(owner, node.func.attr) and (filename, function.name, node.func.attr) not in allowed:
                    violations.append(f"{filename}:{function.name}:{node.lineno}")
    return violations


def test_render_ffmpeg_invocations_share_the_monitored_runner() -> None:
    # Given every heavy render module and its explicit short read-only probe exceptions.
    root = Path(__file__).resolve().parents[3]
    modules = [root / "packages/creator-service/creator_service" / name for name in (
        "ffmpeg_service.py", "render_segments.py", "bgm_service.py",
    )]
    modules.extend((root / "apps/worker-orchestrator/tasks").glob("legacy_render_*.py"))
    modules.extend(root / "apps/worker-orchestrator/tasks" / name for name in ("timeline_render.py", "render_video.py"))
    # When inspected, then no other heavy invocation can bypass the runner.
    violations = [line for path in modules for line in bypasses(path.read_text(), path.name, ALLOWED)]
    assert violations == []


@pytest.mark.parametrize("source", [
    "import subprocess\ndef f():\n    subprocess.check_output(['x'])",
    "import subprocess as sp\ndef f():\n    sp.run(['x'])",
    "from subprocess import run",
    "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')",
    "import os\ndef f():\n    os.system('x')",
    "import os\ndef f():\n    os.spawnvp(os.P_WAIT, 'x', ['x'])",
], ids=["check_output", "alias", "from-import", "asyncio", "os-system", "os-spawn"])
def test_guard_flags_process_launches_outside_the_runner(source: str) -> None:
    # Given a module launching a process in a form the guard must understand.
    # When it is inspected with no allowed call sites.
    violations = bypasses(source, "module.py", set())
    # Then the launch is reported.
    assert violations


def test_guard_accepts_an_explicitly_allowed_probe() -> None:
    # Given the documented short probe in its allowed call site.
    source = "import subprocess\ndef probe():\n    subprocess.run(['ffprobe'])"
    # When it is inspected with that exact call site allowed.
    # Then nothing is reported.
    assert bypasses(source, "module.py", {("module.py", "probe", "run")}) == []
