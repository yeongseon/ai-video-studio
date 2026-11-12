"""Free-space admission for jobs that write media (#1009)."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from collections import namedtuple
from pathlib import Path

import pytest

from creator_service import job_capacity
from creator_service.storage_policy import FilesystemLowSpace, StoragePolicy

MIB = 1024 * 1024
_Usage = namedtuple("_Usage", "total used free")


class FakeDisk:
    """Stands in for ``shutil.disk_usage``: answers per exact path and records every query."""

    def __init__(self, free_by_path: dict[Path, int]) -> None:
        self.free_by_path = free_by_path
        self.queried: list[Path] = []

    def __call__(self, path: str | os.PathLike[str]) -> _Usage:
        queried = Path(path)
        self.queried.append(queried)
        if queried not in self.free_by_path:
            raise AssertionError(f"unexpected filesystem query for {queried}")
        free = self.free_by_path[queried]
        return _Usage(total=1024 * 1024 * MIB, used=1024 * 1024 * MIB - free, free=free)


class FakeCounter:
    def __init__(self, sink: list[tuple[str, int, dict[str, str] | None]], name: str) -> None:
        self._sink = sink
        self._name = name

    def add(self, value: int, attributes: dict[str, str] | None = None) -> None:
        self._sink.append((self._name, value, attributes))


class FakeMeter:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, int, dict[str, str] | None]] = []

    def create_counter(self, name: str, **_kwargs: object) -> FakeCounter:
        return FakeCounter(self.recorded, name)


@pytest.fixture
def volumes(tmp_path: Path) -> tuple[Path, Path]:
    artifacts = tmp_path.resolve() / "artifacts"
    scratch = tmp_path.resolve() / "scratch"
    artifacts.mkdir()
    scratch.mkdir()
    return artifacts, scratch


def _policy(artifact_mib: int = 500, scratch_mib: int = 500) -> StoragePolicy:
    return StoragePolicy(
        artifact_disk_low_watermark_bytes=artifact_mib * MIB,
        temp_disk_low_watermark_bytes=scratch_mib * MIB,
    )


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    entries: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
        entries[str(path.relative_to(root))] = (digest, path.stat().st_mtime_ns)
    return entries


def test_policy_reads_both_job_watermarks_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an operator who sets one watermark per filesystem.
    monkeypatch.setenv("MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES", "2147483648")
    monkeypatch.setenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", "1073741824")
    # When the policy is read from the environment.
    policy = StoragePolicy.from_env()
    # Then each filesystem has its own floor.
    assert policy.artifact_disk_low_watermark_bytes == 2147483648
    assert policy.temp_disk_low_watermark_bytes == 1073741824


def test_both_job_watermarks_default_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given no watermark configuration at all.
    monkeypatch.delenv("MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES", raising=False)
    monkeypatch.delenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", raising=False)
    # When the policy is read.
    policy = StoragePolicy.from_env()
    # Then existing installations keep today's behavior: nothing is refused.
    assert policy.artifact_disk_low_watermark_bytes == 0
    assert policy.temp_disk_low_watermark_bytes == 0


def test_job_is_refused_when_the_artifact_filesystem_is_below_its_watermark(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given plenty of scratch space but an artifact filesystem under its floor.
    artifacts, scratch = volumes
    disk = FakeDisk({artifacts: 412 * MIB, scratch: 9000 * MIB})
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", disk)
    # When a render job asks to start.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then it is refused for the artifact filesystem, with the measured shortfall.
    refusal = raised.value
    assert (refusal.filesystem, refusal.free_bytes, refusal.required_bytes) == ("artifact", 412 * MIB, 500 * MIB)


def test_job_is_refused_when_the_scratch_filesystem_is_below_its_watermark(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given plenty of artifact space but a scratch filesystem under its floor.
    artifacts, scratch = volumes
    disk = FakeDisk({artifacts: 9000 * MIB, scratch: 120 * MIB})
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", disk)
    # When an image job asks to start.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity(
            "generate_scene_image", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then it is refused for the scratch filesystem.
    refusal = raised.value
    assert (refusal.filesystem, refusal.free_bytes, refusal.required_bytes) == ("scratch", 120 * MIB, 500 * MIB)


def test_job_proceeds_when_both_filesystems_are_above_their_watermarks(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given both filesystems above their floors.
    artifacts, scratch = volumes
    disk = FakeDisk({artifacts: 501 * MIB, scratch: 9000 * MIB})
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", disk)
    # When the job asks to start.
    job_capacity.ensure_job_capacity(
        "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
    )
    # Then it proceeds, and both filesystems were actually measured.
    assert sorted(disk.queried) == sorted([artifacts, scratch])


@pytest.mark.parametrize("filesystem", ["artifact", "scratch"])
def test_free_space_equal_to_the_watermark_is_enough_and_one_byte_less_is_not(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, filesystem: str,
) -> None:
    # Given a filesystem sitting exactly on its floor, then one byte under it.
    artifacts, scratch = volumes
    target = artifacts if filesystem == "artifact" else scratch
    other = scratch if filesystem == "artifact" else artifacts
    for free, refused in ((500 * MIB, False), (500 * MIB - 1, True)):
        monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({target: free, other: 9000 * MIB}))

        def start() -> None:
            job_capacity.ensure_job_capacity(
                "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
            )

        # When the job asks to start.
        # Then the floor itself is enough and one byte less is refused.
        if refused:
            with pytest.raises(FilesystemLowSpace):
                start()
        else:
            start()


@pytest.mark.parametrize("disabled", ["artifact", "scratch", "both"])
def test_a_zero_watermark_disables_that_filesystem_check_without_measuring_it(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, disabled: str,
) -> None:
    # Given a disabled watermark whose filesystem reports no free space at all.
    artifacts, scratch = volumes
    policy = StoragePolicy(
        artifact_disk_low_watermark_bytes=0 if disabled in ("artifact", "both") else 500 * MIB,
        temp_disk_low_watermark_bytes=0 if disabled in ("scratch", "both") else 500 * MIB,
    )
    free = {artifacts: 0 if disabled in ("artifact", "both") else 9000 * MIB,
            scratch: 0 if disabled in ("scratch", "both") else 9000 * MIB}
    disk = FakeDisk(free)
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", disk)
    # When the job asks to start.
    job_capacity.ensure_job_capacity(
        "render_video", policy=policy, artifact_root=artifacts, scratch_root=scratch,
    )
    # Then nothing is refused and the disabled filesystem was never even queried.
    if disabled in ("artifact", "both"):
        assert artifacts not in disk.queried
    if disabled in ("scratch", "both"):
        assert scratch not in disk.queried


def test_the_artifact_filesystem_is_reported_first_when_both_are_low(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given both filesystems under their floors.
    artifacts, scratch = volumes
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 1 * MIB, scratch: 2 * MIB}))
    # When the job asks to start.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then the durable-output filesystem is the one named.
    assert raised.value.filesystem == "artifact"


def test_a_missing_artifact_root_is_measured_through_its_nearest_existing_parent_and_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an artifact root that does not exist yet (a fresh checkout) below an existing directory.
    data = tmp_path.resolve() / "data"
    data.mkdir()
    scratch = tmp_path.resolve() / "scratch"
    scratch.mkdir()
    artifact_root = data / "artifacts" / "nested"
    disk = FakeDisk({data: 10 * MIB, scratch: 9000 * MIB})
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", disk)
    # When the job asks to start.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifact_root, scratch_root=scratch,
        )
    # Then the filesystem that would hold the root was measured, and the check created nothing.
    assert raised.value.filesystem == "artifact"
    assert disk.queried[0] == data
    assert not (data / "artifacts").exists()


@pytest.mark.parametrize("filesystem", ["artifact", "scratch"])
def test_a_filesystem_that_cannot_be_measured_does_not_block_the_job_and_is_logged(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, filesystem: str,
) -> None:
    # Given a filesystem whose usage cannot be read.
    artifacts, scratch = volumes
    broken = artifacts if filesystem == "artifact" else scratch

    def disk_usage(path: str | os.PathLike[str]) -> _Usage:
        if Path(path) == broken:
            raise OSError("statvfs failed")
        return _Usage(total=1024 * MIB, used=0, free=9000 * MIB)

    monkeypatch.setattr(job_capacity.shutil, "disk_usage", disk_usage)
    # When the job asks to start.
    with caplog.at_level(logging.WARNING, logger="creator_service.job_capacity"):
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then it proceeds (the write itself will report a real error) and the gap is visible.
    records = [r for r in caplog.records if r.name == "creator_service.job_capacity"]
    assert [getattr(r, "filesystem", None) for r in records] == [filesystem]
    assert getattr(records[0], "measured", None) is False


def test_refusal_message_names_the_filesystem_and_the_shortfall_but_no_path(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an artifact filesystem 88 MiB under its floor.
    artifacts, scratch = volumes
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 412 * MIB, scratch: 9000 * MIB}))
    # When the job is refused.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then the message an operator or API client reads is actionable and path-free.
    message = str(raised.value)
    assert message == raised.value.detail
    assert "Artifact storage" in message
    assert "412 MiB" in message and "500 MiB" in message and "88 MiB" in message
    assert str(artifacts) not in message and str(scratch) not in message


def test_scratch_refusal_message_names_scratch_storage(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a scratch filesystem under its floor.
    artifacts, scratch = volumes
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 9000 * MIB, scratch: 1 * MIB}))
    # When the job is refused.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then the message names the scratch filesystem, not the artifact one.
    assert "Scratch storage" in str(raised.value)
    assert "Artifact" not in str(raised.value)


def test_a_sub_mebibyte_shortfall_is_never_reported_as_zero(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a filesystem one byte under its floor.
    artifacts, scratch = volumes
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 500 * MIB - 1, scratch: 9000 * MIB}))
    # When the job is refused.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then the message still says that something is missing.
    assert "1 MiB short" in str(raised.value)


def test_refusal_is_logged_with_the_filesystem_and_the_shortfall(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given an artifact filesystem under its floor.
    artifacts, scratch = volumes
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 100 * MIB, scratch: 9000 * MIB}))
    # When a job is refused.
    with caplog.at_level(logging.WARNING, logger="creator_service.job_capacity"):
        with pytest.raises(FilesystemLowSpace):
            job_capacity.ensure_job_capacity(
                "generate_scene_image", run_id=42, policy=_policy(),
                artifact_root=artifacts, scratch_root=scratch,
            )
    # Then one structured warning carries what an operator needs.
    records = [r for r in caplog.records if r.name == "creator_service.job_capacity"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert (record.filesystem, record.task_name, record.run_id) == ("artifact", "generate_scene_image", 42)
    assert (record.free_bytes, record.required_bytes, record.shortfall_bytes) == (100 * MIB, 500 * MIB, 400 * MIB)
    assert record.path == str(artifacts)


@pytest.mark.parametrize("low", ["artifact", "scratch"])
def test_refusal_is_counted_per_filesystem(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, low: str,
) -> None:
    # Given a job refused for one filesystem.
    artifacts, scratch = volumes
    free = {artifacts: 100 * MIB if low == "artifact" else 9000 * MIB,
            scratch: 100 * MIB if low == "scratch" else 9000 * MIB}
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk(free))
    meter = FakeMeter()
    monkeypatch.setattr(job_capacity, "get_meter", lambda _name: meter)
    # When the refusal happens.
    with pytest.raises(FilesystemLowSpace):
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then a low-cardinality counter distinguishes the two filesystems.
    assert meter.recorded == [
        ("worker.job.low_space_refusals", 1, {"filesystem": low, "celery.task_name": "render_video"}),
    ]


def test_a_passing_check_neither_logs_nor_counts(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given both filesystems above their floors.
    artifacts, scratch = volumes
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 9000 * MIB, scratch: 9000 * MIB}))
    meter = FakeMeter()
    monkeypatch.setattr(job_capacity, "get_meter", lambda _name: meter)
    # When the job asks to start.
    with caplog.at_level(logging.DEBUG, logger="creator_service.job_capacity"):
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then there is no noise.
    assert meter.recorded == []
    assert [r for r in caplog.records if r.name == "creator_service.job_capacity"] == []


def test_refusal_leaves_existing_artifacts_byte_identical_and_creates_nothing(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an artifact root that already holds a published video and its metadata.
    artifacts, scratch = volumes
    (artifacts / "run_7").mkdir()
    (artifacts / "run_7" / "output.mp4").write_bytes(b"published-video-bytes" * 100)
    (artifacts / "run_7" / "metadata.json").write_text('{"duration": 12.5}', encoding="utf-8")
    before_artifacts, before_scratch = _snapshot(artifacts), _snapshot(scratch)
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 1 * MIB, scratch: 1 * MIB}))
    # When a job is refused.
    with pytest.raises(FilesystemLowSpace):
        job_capacity.ensure_job_capacity(
            "render_video", policy=_policy(), artifact_root=artifacts, scratch_root=scratch,
        )
    # Then not a byte, timestamp, file or directory changed on either filesystem.
    assert _snapshot(artifacts) == before_artifacts
    assert _snapshot(scratch) == before_scratch


def test_environment_supplies_the_artifact_root_and_the_scratch_directory(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the deployment's own configuration instead of explicit arguments.
    artifacts, scratch = volumes
    monkeypatch.setenv("ARTIFACT_ROOT", str(artifacts))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES", str(500 * MIB))
    monkeypatch.setenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", str(500 * MIB))
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 100 * MIB, scratch: 9000 * MIB}))
    # When a job asks to start.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity("render_video")
    # Then the configured locations and watermarks were used.
    assert raised.value.filesystem == "artifact"
    assert raised.value.required_bytes == 500 * MIB


def test_environment_supplies_the_scratch_directory(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given plenty of artifact space and a scratch filesystem under its floor, with both
    # locations coming from the process's own configuration.
    artifacts, scratch = volumes
    monkeypatch.setenv("ARTIFACT_ROOT", str(artifacts))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES", str(500 * MIB))
    monkeypatch.setenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", str(500 * MIB))
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 9000 * MIB, scratch: 120 * MIB}))
    # When a job asks to start.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity("render_video")
    # Then the temp directory the process is configured with is the one that was measured.
    assert (raised.value.filesystem, raised.value.free_bytes) == ("scratch", 120 * MIB)


def test_a_required_amount_that_is_not_a_whole_mebibyte_is_never_understated(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a floor of 1 000 000 000 bytes (953.67 MiB) and 900 MiB free.
    artifacts, scratch = volumes
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", FakeDisk({artifacts: 900 * MIB, scratch: 9000 * MIB}))
    policy = StoragePolicy(artifact_disk_low_watermark_bytes=1_000_000_000, temp_disk_low_watermark_bytes=500 * MIB)
    # When the job is refused.
    with pytest.raises(FilesystemLowSpace) as raised:
        job_capacity.ensure_job_capacity("render_video", policy=policy, artifact_root=artifacts, scratch_root=scratch)
    # Then free space is rounded down and both the requirement and the shortfall are rounded up,
    # so the three numbers in the message stay consistent and never make the disk look healthier.
    assert str(raised.value) == (
        "Artifact storage has 900 MiB free but at least 954 MiB is required before a new job can start (54 MiB short)"
    )


def test_nothing_is_checked_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a deployment that sets no watermark and a disk that would fail any check.
    monkeypatch.delenv("MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES", raising=False)
    monkeypatch.delenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", raising=False)

    def disk_usage(path: str | os.PathLike[str]) -> _Usage:
        raise AssertionError(f"disk must not be measured by default: {path}")

    monkeypatch.setattr(job_capacity.shutil, "disk_usage", disk_usage)
    # When a job asks to start.
    job_capacity.ensure_job_capacity("render_video")
    # Then it proceeds without measuring anything.
