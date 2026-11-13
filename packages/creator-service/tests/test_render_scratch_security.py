"""Root trust and overlap are checked without relying on host privileges."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from creator_service import render_scratch as scratch


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "scratch"
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(root))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    return root


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o777])
def test_rejects_writable_root(root: Path, mode: int) -> None:
    # Given an existing root another principal can rename entries in.
    root.mkdir(mode=0o700)
    root.chmod(mode)
    # When validated, then it fails closed before allocation.
    with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR.*writ"):
        scratch.configured_scratch_root()


def test_rejects_foreign_owner(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a different effective uid (no privileged chown is needed).
    root.mkdir(mode=0o700)
    monkeypatch.setattr(scratch.os, "geteuid", lambda: root.stat().st_uid + 1)
    # When validated, then worker ownership is mandatory.
    with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR.*own"):
        scratch.configured_scratch_root()


@pytest.mark.parametrize("mode", [0o777, 0o775])
def test_rejects_mutable_ancestor(root: Path, mode: int) -> None:
    # Given a private root under an ancestor with no sticky protection.
    root.mkdir(mode=0o700)
    root.parent.chmod(mode)
    # When validated, then ancestor write access is rejected too.
    with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR.*ancestor"):
        scratch.configured_scratch_root()


def test_rejects_root_symlink(root: Path) -> None:
    # Given an alias rather than a real root directory.
    target = root.with_name("target")
    target.mkdir(mode=0o700)
    root.symlink_to(target, target_is_directory=True)
    # When validated, then the alias cannot be adopted.
    with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR.*symlink"):
        scratch.configured_scratch_root()


def test_creates_each_missing_component_privately(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a permissive process umask and two missing components.
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(root / "nested"))
    previous = os.umask(0o002)
    try:
        # When allocating, then both new components and the run are 0700.
        directory = scratch.create_render_scratch(root.parent / "output" / "video.mp4", "timeline-")
        assert [p.stat().st_mode & 0o777 for p in (root, root / "nested", directory)] == [0o700] * 3
    finally:
        os.umask(previous)


def test_accepts_private_root_and_root_owned_mount(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a private root with a simulated root-owned 0755 mount ancestor.
    root.mkdir(mode=0o700)
    original = os.lstat

    def mount_stat(path: str | os.PathLike[str]) -> os.stat_result:
        value = original(path)
        if Path(path) == root.parent:
            fields = list(value)
            fields[0], fields[4] = 0o40755, 0
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(scratch.os, "lstat", mount_stat)
    # When validated, then a root-owned read-only-to-others ancestor is accepted.
    assert scratch.configured_scratch_root() == root


@pytest.mark.parametrize("owner,mode", [(4242, 0o755), (0, 0o755), (0, 0o1777), (1234, 0o755), (1234, 0o1777)])
def test_ancestor_ownership_is_required_even_without_write_bits(
    root: Path, monkeypatch: pytest.MonkeyPatch, owner: int, mode: int,
) -> None:
    # Given a simulated worker uid and a mount with independently controlled ownership.
    root.mkdir(mode=0o700)
    original = os.lstat

    def mount_stat(path: str | os.PathLike[str]) -> os.stat_result:
        value = original(path)
        fields = list(value)
        fields[4] = 1234
        if Path(path) == root.parent:
            fields[0], fields[4] = 0o40000 | mode, owner
        return os.stat_result(fields)

    monkeypatch.setattr(scratch.os, "lstat", mount_stat)
    monkeypatch.setattr(scratch.os, "geteuid", lambda: 1234)
    # When inspected, then only root/worker-controlled ancestors are trusted.
    if owner == 4242:
        with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR.*ancestor.*own"):
            scratch.configured_scratch_root()
    else:
        assert scratch.configured_scratch_root() == root


@pytest.mark.parametrize("layout", ["double_slash", "artifact_inside", "symlink_alias", "output_inside", "output_parent"])
def test_rejects_overlap_in_both_directions(root: Path, monkeypatch: pytest.MonkeyPatch, layout: str) -> None:
    # Given alternate spellings or nesting of scratch and retained output.
    artifact = root.parent / "artifacts"
    output = root.parent / "published" / "output.mp4"
    match layout:
        case "double_slash":
            monkeypatch.setenv("RENDER_SCRATCH_DIR", "/" + str(artifact / "scratch"))
        case "artifact_inside":
            monkeypatch.setenv("ARTIFACT_ROOT", str(root / "artifacts"))
        case "symlink_alias":
            root.mkdir(mode=0o700)
            artifact.symlink_to(root, target_is_directory=True)
        case "output_inside":
            output = root / "published" / "output.mp4"
        case "output_parent":
            output = root.parent / "output.mp4"
        case _:
            raise AssertionError(layout)
    # When allocating, then neither tree may contain the other.
    with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR"):
        scratch.create_render_scratch(output, "timeline-")


@pytest.mark.parametrize("code", [errno.ENAMETOOLONG, errno.EACCES, errno.EPERM, errno.ENOTDIR, errno.ELOOP, errno.EINVAL])
@pytest.mark.parametrize("stage", ["inspect", "create"])
def test_configuration_oserrors_are_classified(root: Path, monkeypatch: pytest.MonkeyPatch, code: int, stage: str) -> None:
    # Given an OS configuration failure at inspection or allocation.
    def fail(*args: str, **kwargs: str) -> str:
        raise OSError(code, os.strerror(code), "not-an-operator-path")

    if stage == "inspect":
        monkeypatch.setattr(scratch.os, "lstat", fail)
    else:
        monkeypatch.setattr(scratch.tempfile, "mkdtemp", fail)
    # When allocating, then errors name the setting without the OS filename.
    with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR") as raised:
        scratch.create_render_scratch(root.parent / "published" / "output.mp4", "timeline-")
    assert "not-an-operator-path" not in str(raised.value)


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EMFILE])
def test_operational_oserrors_propagate(root: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    # Given an allocation-time capacity or hardware failure.
    error = OSError(code, os.strerror(code))

    def fail(*args: str, **kwargs: str) -> str:
        raise error

    monkeypatch.setattr(scratch.tempfile, "mkdtemp", fail)
    # When allocating, then the original operational condition survives unchanged.
    with pytest.raises(OSError) as raised:
        scratch.create_render_scratch(root.parent / "published" / "output.mp4", "timeline-")
    assert raised.value is error


def test_long_component_is_configuration_error(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a real filesystem name-length violation.
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(root / ("x" * 300)))
    # When validated, then no raw OSError escapes.
    with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR"):
        scratch.configured_scratch_root()


@pytest.mark.parametrize("control", ["\n", "\r", "\x00"])
def test_control_characters_are_rejected(root: Path, monkeypatch: pytest.MonkeyPatch, control: str) -> None:
    # Given an injected environment mapping, since the OS itself rejects NUL.
    monkeypatch.setattr(scratch.os, "environ", {"RENDER_SCRATCH_DIR": str(root) + control})
    # When validated, then malformed operator input is rejected consistently.
    with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR"):
        scratch.configured_scratch_root()


@pytest.mark.parametrize("replacement", ["symlink", "mode", "owner"])
def test_checks_allocated_run_directory(root: Path, monkeypatch: pytest.MonkeyPatch, replacement: str) -> None:
    # Given a compromised allocator result, simulated at its filesystem boundary.
    original = scratch.tempfile.mkdtemp

    def allocate(*, prefix: str, dir: Path) -> str:
        directory = Path(original(prefix=prefix, dir=dir))
        if replacement == "symlink":
            directory.rmdir()
            directory.symlink_to(root.parent, target_is_directory=True)
        elif replacement == "mode":
            directory.chmod(0o755)
        else:
            monkeypatch.setattr(scratch.os, "geteuid", lambda: directory.stat().st_uid + 1)
        return str(directory)

    monkeypatch.setattr(scratch.tempfile, "mkdtemp", allocate)
    # When allocation returns, then unsafe results are never handed to a renderer.
    with pytest.raises(scratch.RenderScratchConfigurationError, match="RENDER_SCRATCH_DIR"):
        scratch.create_render_scratch(root.parent / "published" / "output.mp4", "timeline-")
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("prefix", [".render_tmp_", ".render_plan_", "timeline-"])
def test_unset_run_directories_are_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    # Given legacy placement with no configured root.
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    # When allocating, then private permissions apply in unset mode as well.
    directory = scratch.create_render_scratch(tmp_path / "output.mp4", prefix)
    try:
        assert directory.stat().st_mode & 0o777 == 0o700
    finally:
        shutil.rmtree(directory)
