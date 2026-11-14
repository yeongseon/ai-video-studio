"""Every dataclass exception of the service package survives pickling however it was built, and a scan keeps it that way (#1100)."""

from __future__ import annotations

import ast
import importlib
import pickle
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

import creator_service
import pytest
from creator_service.object_storage import StorageDeletionError, StorageReadError
from creator_service.render_scratch_monitor import RenderScratchLimitExceeded
from creator_service.storage_policy import FilesystemLowSpace, StorageBudgetExceeded, StorageLowWatermark
from creator_service.visual_asset_service import SceneAssetAcceptanceRejected

PACKAGE_ROOT = Path(creator_service.__file__).resolve().parent

MAKERS: dict[str, Callable[[], Any]] = {
    "deletion-keyword": lambda: StorageDeletionError(key="runs/7/final.mp4"),
    "deletion-positional": lambda: StorageDeletionError("runs/7/final.mp4"),
    "scene-asset-keyword": lambda: SceneAssetAcceptanceRejected(run_id=7),
    "scene-asset-positional": lambda: SceneAssetAcceptanceRejected(7),
    "read-keyword": lambda: StorageReadError(kind="timeout"),
    "read-positional": lambda: StorageReadError("generation_changed"),
    "read-default": lambda: StorageReadError(),
    "budget": lambda: StorageBudgetExceeded(
        workspace_id=7, incoming_bytes=10, stored_bytes=90, reserved_bytes=5, max_stored_bytes=100,
    ),
    "watermark": lambda: StorageLowWatermark(incoming_bytes=10, free_bytes=5),
    "filesystem": lambda: FilesystemLowSpace(incoming_bytes=0, free_bytes=412, filesystem="scratch", required_bytes=500),
    "scratch-limit": lambda: RenderScratchLimitExceeded(cap_bytes=1024, observed_bytes=2048, label="timeline"),
}


def field_values(error: Any) -> dict[str, object]:
    return {field.name: getattr(error, field.name) for field in fields(error)}


def qualified_name(error_type: type) -> str:
    return f"{error_type.__module__}.{error_type.__qualname__}"


@pytest.mark.parametrize("protocol", [pickle.DEFAULT_PROTOCOL, pickle.HIGHEST_PROTOCOL])
@pytest.mark.parametrize("make", MAKERS.values(), ids=MAKERS.keys())
def test_dataclass_exception_survives_a_pickle_round_trip(make: Callable[[], Any], protocol: int) -> None:
    # Given an exception built the way production code builds it, with keywords or positionally.
    error = make()
    # When it crosses a process boundary, as a Celery pool or a result may require.
    restored = pickle.loads(pickle.dumps(error, protocol=protocol))
    # Then it is the same typed error with every field and the same message.
    assert type(restored) is type(error)
    assert restored is not error
    assert field_values(restored) == field_values(error)
    assert str(restored) == str(error)


def decorated_subclasses(source: str) -> list[str]:
    """Names of the module-level classes that have a base class and a decorator, the shape of a dataclass exception."""
    return [
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.bases and node.decorator_list
    ]


def module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE_ROOT).with_suffix("").parts
    return ".".join(("creator_service", *(part for part in parts if part != "__init__")))


def dataclass_exceptions() -> set[type[BaseException]]:
    """Every module-level dataclass of the package that derives from BaseException.

    Only modules with a decorated subclass are imported, so the scan stays cheap and needs no optional dependency.
    """
    found: set[type[BaseException]] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        names = decorated_subclasses(path.read_text(encoding="utf-8"))
        if not names:
            continue
        module = importlib.import_module(module_name(path))
        for name in names:
            candidate = getattr(module, name)
            if is_dataclass(candidate) and issubclass(candidate, BaseException):
                found.add(candidate)
    return found


def defines_reduce(error_type: type[BaseException]) -> bool:
    """Whether the class, or a base other than BaseException itself, overrides ``__reduce__``."""
    return next(klass for klass in error_type.__mro__ if "__reduce__" in vars(klass)) is not BaseException


def test_every_dataclass_exception_has_a_round_trip_case() -> None:
    # Given every dataclass exception the package defines, found by scanning its source.
    found = {qualified_name(error_type) for error_type in dataclass_exceptions()}
    # When compared with the exception types the round-trip cases construct.
    covered = {qualified_name(type(make())) for make in MAKERS.values()}
    # Then none is missing a case and no case is stale, so a new exception cannot slip in untested.
    assert found == covered


def test_every_dataclass_exception_defines_a_pickle_rebuild() -> None:
    # Given every dataclass exception the package defines.
    # When one neither defines nor inherits a __reduce__, an instance built with keywords cannot be unpickled.
    unprotected = sorted(qualified_name(error_type) for error_type in dataclass_exceptions() if not defines_reduce(error_type))
    # Then there is none.
    assert unprotected == []


@dataclass(slots=True, eq=False)
class Unprotected(RuntimeError):
    key: str


@dataclass(slots=True, eq=False)
class Protected(RuntimeError):
    key: str

    def __reduce__(self) -> tuple[type[Protected], tuple[str]]:
        return type(self), (self.key,)


@dataclass(slots=True, eq=False)
class InheritsProtection(Protected):
    pass


def test_a_dataclass_exception_without_a_rebuild_cannot_be_unpickled_when_built_with_keywords() -> None:
    # Given the default exception pickling, which rebuilds from the positional arguments only.
    payload = pickle.dumps(Unprotected(key="runs/7"))
    # When the payload is loaded.
    # Then the keyword field is missing: the premise of the scan.
    with pytest.raises(TypeError, match="missing 1 required positional argument"):
        pickle.loads(payload)


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [(Unprotected, False), (Protected, True), (InheritsProtection, True)],
    ids=["none", "own", "inherited"],
)
def test_the_scan_recognizes_a_class_that_defines_or_inherits_a_rebuild(
    error_type: type[BaseException], expected: bool,
) -> None:
    # Given a dataclass exception with no rebuild, its own, or one inherited from a base.
    # When the scan classifies it.
    # Then only the first is reported as unprotected.
    assert defines_reduce(error_type) is expected


@pytest.mark.parametrize("source", [
    "from dataclasses import dataclass\n@dataclass\nclass Failure(RuntimeError):\n    key: str\n",
    "import dataclasses\n@dataclasses.dataclass(frozen=True, slots=True)\nclass Failure(ValueError):\n    key: str\n",
    "from dataclasses import dataclass as record\n@record(slots=True, eq=False)\nclass Failure(Exception):\n    key: str\n",
], ids=["plain", "module-attribute", "aliased"])
def test_the_scan_picks_up_a_dataclass_exception_however_it_is_decorated(source: str) -> None:
    # Given a module defining a decorated exception in one of the usual spellings.
    # When the scan reads its source, then the class is a candidate.
    assert decorated_subclasses(source) == ["Failure"]


@pytest.mark.parametrize("source", [
    "class Failure(RuntimeError):\n    pass\n",
    "from dataclasses import dataclass\n@dataclass\nclass Record:\n    key: str\n",
    "from dataclasses import dataclass\ndef build():\n    @dataclass\n    class Local(RuntimeError):\n        key: str\n",
], ids=["undecorated", "no-base", "function-local"])
def test_the_scan_skips_what_cannot_be_a_picklable_dataclass_exception(source: str) -> None:
    # Given classes that are not decorated, have no base, or are local and so cannot be pickled by reference.
    # When the scan reads their source, then none is a candidate.
    assert decorated_subclasses(source) == []
