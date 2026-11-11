"""Descriptor-relative deletion that never follows directory symlinks."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

_DIRECTORY_OPEN_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class SymlinkedDeletionRootError(OSError):
    """Raised when a requested deletion root is itself a symlink."""


class ArtifactPathOutsideRootError(OSError):
    """Raised when a recorded file is not strictly inside the artifact root."""


def delete_artifact_file(path: Path, artifact_root: Path | None = None) -> None:
    """Unlink a root-anchored file without following intermediate symlinks."""
    root = Path(os.path.abspath(
        artifact_root if artifact_root is not None else os.getenv("ARTIFACT_ROOT", "data/artifacts"),
    ))
    absolute_path = Path(os.path.abspath(path))
    if absolute_path == root or not absolute_path.is_relative_to(root):
        raise ArtifactPathOutsideRootError
    components = absolute_path.relative_to(root).parts
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY))
        for component in components[:-1]:
            descriptors.append(os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptors[-1]))
        parent_fd = descriptors[-1]
        file_stat = os.stat(components[-1], dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(file_stat.st_mode):
            raise IsADirectoryError
        os.unlink(components[-1], dir_fd=parent_fd)
    except FileNotFoundError:
        return
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _delete_children(directory_fd: int) -> None:
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                child_fd = os.open(entry.name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
                try:
                    _delete_children(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(entry.name, dir_fd=directory_fd)
            else:
                os.unlink(entry.name, dir_fd=directory_fd)


def delete_direct_child_tree(parent: Path, child_name: str) -> None:
    """Delete one real child directory without traversing any symlink."""
    try:
        resolved_parent = parent.resolve(strict=True)
    except FileNotFoundError:
        return
    parent_fd = os.open(resolved_parent, _DIRECTORY_OPEN_FLAGS)
    try:
        try:
            child_stat = os.stat(child_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(child_stat.st_mode):
            raise SymlinkedDeletionRootError
        if not stat.S_ISDIR(child_stat.st_mode):
            raise NotADirectoryError
        child_fd = os.open(child_name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        try:
            _delete_children(child_fd)
        finally:
            os.close(child_fd)
        os.rmdir(child_name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def delete_confined_file(path: Path) -> None:
    """Unlink one recorded file without following its final path component."""
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except FileNotFoundError:
        return
    parent_fd = os.open(resolved_parent, _DIRECTORY_OPEN_FLAGS)
    try:
        try:
            file_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(file_stat.st_mode):
            raise IsADirectoryError
        os.unlink(path.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
