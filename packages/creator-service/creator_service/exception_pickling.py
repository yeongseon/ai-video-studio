"""Pickle support for dataclass exceptions that may be built with keyword arguments (#1087, #1100)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import Field, fields
from typing import Any, ClassVar, Protocol

Reduced = tuple[Callable[..., BaseException], tuple[type[BaseException], dict[str, object]]]


class _DataclassInstance(Protocol):
    """What ``dataclasses.fields`` accepts, as the stub-only ``_typeshed.DataclassInstance`` defines it.

    The import contract (``test_python_import_contract.py``) declares every import, type-checking ones included,
    and ``_typeshed`` has no distribution, so the protocol is spelled out here.
    """

    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]


def rebuild_exception(cls: Callable[..., BaseException], values: dict[str, object]) -> BaseException:
    """Rebuild a dataclass exception by keyword, so a subclass keeps its own fields.

    The generated ``__init__`` takes keyword-friendly fields while ``BaseException.args`` holds only
    what was passed positionally, so the default pickle path (``cls(*args)``) cannot rebuild an
    instance created with keywords and drops a keyword mixed with positionals. Celery pickles task
    exceptions between pool processes. Every dataclass field must be accepted by the generated
    ``__init__`` (``init=True``), which holds for all exceptions of this package.
    """
    return cls(**values)


def field_values(error: _DataclassInstance) -> dict[str, object]:
    """The fields of a dataclass instance by name, the keywords ``rebuild_exception`` passes back."""
    return {field.name: getattr(error, field.name) for field in fields(error)}
