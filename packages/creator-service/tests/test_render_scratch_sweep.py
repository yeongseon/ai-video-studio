import hashlib
import importlib
import os
from pathlib import Path

import pytest


def sweep(root: Path, now: float, **overrides):
    module = importlib.import_module("creator_service.render_scratch_sweeper")
    policy = module.SweepPolicy(**overrides)
    return module.sweep_render_scratch(
        root, policy, module.SweepRuntime(now=lambda: now, hostname="local", pid_alive=lambda pid: pid == os.getpid()),
    )


def named(pid: str = "42", host: str = "local", prefix: str = ".render_tmp_") -> str:
    digest = hashlib.sha256(host.encode()).hexdigest()[:12]
    return f"{prefix}{digest}_{'a' * 12}_{pid}_abcdefgh"


@pytest.mark.parametrize("name,age,removed", [
    (named(), 61, True), (named(), 59, False),
    (named(str(os.getpid())), 1800, False), (named(str(os.getpid())), 3601, True),
    (named(host="remote"), 1800, False), (named(host="remote"), 3600, True),
    (".render_tmp_42_deadbeef", 1800, False), (".render_tmp_42_deadbeef", 3600, True),
    (".render_plan_42_deadbeef", 3600, True),
    ("timeline-abcdefgh", 1800, False), ("timeline-abcdefgh", 3600, True),
    ("output.tmp.42.deadbeef.mp4", 1800, False), ("output.tmp.42.deadbeef.mp4", 3600, True),
    (named("0"), 61, False), (named("4194305"), 61, False),
    (named("999999999999999999999999999999"), 61, False),
    (named("-1"), 7200, False), (named("abc"), 7200, False),
    (named(), -1, False), ("output.mp4", 7200, False), ("unrelated", 7200, False),
])
def test_sweep_eligibility_when_entry_has_owner_and_age(
    tmp_path: Path, name: str, age: float, removed: bool,
) -> None:
    # Given real direct children and a clock advanced from their actual ctime/mtime.
    render = tmp_path / "artifacts/1/render"
    render.mkdir(parents=True)
    path = render / name
    if name.endswith(".mp4"):
        path.write_bytes(b"video")
    else:
        path.mkdir()
        (path / "payload").write_bytes(b"scratch")
    info = path.lstat()
    # When a restart sweeps this exact filesystem layout.
    result = sweep(tmp_path / "artifacts", max(info.st_mtime, info.st_ctime) + age)
    # Then only a recognised, stale entry is removed.
    assert path.exists() is not removed
    assert result.removed == int(removed)


def test_sweep_confines_symlinks_and_published_subtrees(tmp_path: Path, caplog) -> None:
    # Given symlinks at every untrusted level and nested scratch under published content.
    root = tmp_path / "artifacts"
    render = root / "1/render"
    render.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    payload = victim / named()
    payload.mkdir()
    (payload / "keep").write_bytes(b"victim")
    (render / named()).symlink_to(payload, target_is_directory=True)
    (root / "2").symlink_to(victim, target_is_directory=True)
    (root / "3").mkdir()
    (root / "3/render").symlink_to(victim, target_is_directory=True)
    published = render / "published" / named()
    published.mkdir(parents=True)
    (published / "keep").write_bytes(b"published")
    # When every entry would qualify by age.
    result = sweep(root, payload.stat().st_ctime + 7200)
    # Then links stay links and both outside and nested published bytes survive.
    assert result.removed == 0
    assert (payload / "keep").read_bytes() == b"victim"
    assert (published / "keep").read_bytes() == b"published"
    assert (render / named()).is_symlink()
    assert result.skipped["symlink"] == 3
    assert sum(record.message == "Render scratch sweep skipped symlink" for record in caplog.records) == 1


def test_age_uses_last_change_not_backdated_mtime(tmp_path: Path) -> None:
    # Given a newly created legacy directory whose mtime alone is ancient.
    path = tmp_path / "1/render/.render_tmp_42_deadbeef"
    path.mkdir(parents=True)
    os.utime(path, (1, 1))
    # When sweeping just after its ctime; then the recent change protects it.
    assert sweep(tmp_path, path.stat().st_ctime + 1).removed == 0
    assert path.exists()


@pytest.mark.parametrize("setting", ["max_entries", "max_removals", "max_run_directories"])
def test_bounds_stop_in_deterministic_run_order(tmp_path: Path, setting: str) -> None:
    # Given three runs, each with a stale legacy directory.
    paths = [tmp_path / f"{run}/render/.render_tmp_42_deadbeef" for run in (1, 2, 10)]
    for path in paths:
        path.mkdir(parents=True)
    # When only one unit of the chosen budget is available.
    result = sweep(tmp_path, max(p.stat().st_ctime for p in paths) + 7200, **{setting: 1})
    # Then the newest run is reclaimed first and the bound is reported.
    assert result.removed == 1
    assert not paths[-1].exists()
    assert all(path.exists() for path in paths[:-1])
    assert setting in result.bounds


def test_second_sweep_is_idempotent(tmp_path: Path) -> None:
    # Given a stale scratch tree and a published sibling.
    path = tmp_path / "1/render/.render_tmp_42_deadbeef"
    path.mkdir(parents=True)
    published = path.parent / "output.mp4"
    published.write_bytes(b"published")
    now = path.stat().st_ctime + 7200
    assert sweep(tmp_path, now).removed == 1
    # When restarted again; then no further content is removed.
    assert sweep(tmp_path, now).removed == 0
    assert published.read_bytes() == b"published"
