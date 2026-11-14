import hashlib
import importlib
import os
import socket
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", [".render_tmp_42_deadbeef", "output.tmp.42.deadbeef.mp4"])
def test_timeout_skips_fifo_without_claiming_removal(tmp_path: Path, caplog, name: str) -> None:
    # Given an attributable name with a filesystem type that is never scratch.
    from creator_service.render_scratch_cleanup import cleanup_delivery_scratch
    render = tmp_path / "1/render"
    render.mkdir(parents=True)
    fifo = render / name
    os.mkfifo(fifo)
    # When timeout reclamation examines the FIFO.
    with caplog.at_level("INFO"):
        cleanup_delivery_scratch(tmp_path, (1, 42), None)
    # Then it is retained without a false removal event.
    assert fifo.exists()
    assert not [r for r in caplog.records if hasattr(r, "scratch_name")]


def test_timeout_shared_entry_bound_is_logged_once(tmp_path: Path, monkeypatch, caplog) -> None:
    # Given both roots with entries after the shared timeout budget is consumed.
    module = importlib.import_module("creator_service.render_scratch_cleanup")
    render = tmp_path / "artifacts/1/render"
    render.mkdir(parents=True)
    (render / ".render_tmp_42_deadbeef").mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    host = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]
    run = hashlib.sha256(str(render.resolve()).encode()).hexdigest()[:12]
    (scratch / f".render_tmp_{host}_{run}_42_abcdefgh").mkdir()
    monkeypatch.setattr(module, "TIMEOUT_MAX_ENTRIES", 1)
    # When cleanup visits both roots with a single entry allowance.
    with caplog.at_level("INFO"):
        module.cleanup_delivery_scratch(tmp_path / "artifacts", (1, 42), scratch)
    # Then one distinct bound produces one event.
    assert sum(r.message == "Render timeout cleanup entry bound reached" for r in caplog.records) == 1
