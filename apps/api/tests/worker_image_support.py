"""Load worker modules the way the API image ships them: flat, beside celery_app.py."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest

WORKER_DIR = Path(__file__).resolve().parents[2] / "worker-orchestrator"


def load_image_sibling(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    spec = spec_from_file_location(name, WORKER_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
