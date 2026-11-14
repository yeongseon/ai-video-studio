"""Descriptor-relative deletion that never follows directory symlinks."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
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


def unlink_direct_child(parent_fd: int, name: str) -> None:
    """Unlink a plain direct-child name relative to an already confined descriptor."""
    if name in ("", ".", "..") or "/" in name:
        raise ValueError("unsafe path component")
    os.unlink(name, dir_fd=parent_fd)


def unlink_beneath(root: Path, parts: Sequence[str]) -> bool:
    """Unlink ``root/parts`` without following any symlink below ``root``.

    ``root`` itself may be a symlink (a bind mount) and is followed. Every later directory
    is opened relative to its already-open parent with O_NOFOLLOW, so a directory swapped
    for a symlink after the caller validated the path cannot redirect the unlink. A symlinked
    intermediate is refused; an absent file, or a regular file where a directory would have to
    be, means there is nothing to delete and returns False. Every component must be a plain
    entry name: ``..`` is not stopped by O_NOFOLLOW and would leave the tree.
    """
    if not parts or any(part in ("", ".", "..") or "/" in part for part in parts):
        raise ValueError("unsafe path component")
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
        for component in parts[:-1]:
            try:
                descriptors.append(os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptors[-1]))
            except NotADirectoryError:
                # O_NOFOLLOW|O_DIRECTORY reports ENOTDIR for a symlink and a regular file alike.
                if stat.S_ISLNK(os.stat(component, dir_fd=descriptors[-1], follow_symlinks=False).st_mode):
                    raise
                return False
        parent_fd = descriptors[-1]
        if stat.S_ISDIR(os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False).st_mode):
            raise IsADirectoryError
        unlink_direct_child(parent_fd, parts[-1])
        return True
    except FileNotFoundError:
        return False
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def delete_artifact_file(path: Path, artifact_root: Path | None = None) -> None:
    """Unlink a recorded file that must lie strictly beneath the artifact root."""
    root = Path(os.path.abspath(
        artifact_root if artifact_root is not None else os.getenv("ARTIFACT_ROOT", "data/artifacts"),
    ))
    absolute_path = Path(os.path.abspath(path))
    if absolute_path == root or not absolute_path.is_relative_to(root):
        raise ArtifactPathOutsideRootError
    unlink_beneath(root, absolute_path.relative_to(root).parts)


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
