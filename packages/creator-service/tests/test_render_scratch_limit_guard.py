import ast
from pathlib import Path


def test_render_ffmpeg_invocations_share_the_monitored_runner() -> None:
    # Given every heavy render module and its explicit short read-only probe exceptions.
    root = Path(__file__).resolve().parents[3]
    modules = [root / "packages/creator-service/creator_service" / name for name in (
        "ffmpeg_service.py", "render_segments.py", "bgm_service.py",
    )]
    modules.extend((root / "apps/worker-orchestrator/tasks").glob("legacy_render_*.py"))
    modules.extend(root / "apps/worker-orchestrator/tasks" / name for name in ("timeline_render.py", "render_video.py"))
    allowed = {
        ("ffmpeg_service.py", "_run_ffmpeg", "Popen"),
        ("ffmpeg_service.py", "get_audio_duration", "run"),
        ("render_segments.py", "_probe_source_duration", "run"),
    }
    violations = []
    # When inspected, then no other heavy invocation can bypass the runner.
    for path in modules:
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if ast.unparse(node.func.value) == "subprocess" and node.func.attr in {"Popen", "run"}:
                        if (path.name, function.name, node.func.attr) not in allowed:
                            violations.append(f"{path.name}:{function.name}:{node.lineno}")
    assert violations == []
