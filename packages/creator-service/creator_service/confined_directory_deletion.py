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
