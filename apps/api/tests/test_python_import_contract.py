"""AST-only third-party declaration contract for workspace production modules (#1042)."""

import ast
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT: Final = Path(__file__).resolve().parents[3]
WORKSPACE: Final = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]
PACKAGES: Final = tuple(
    sorted(path for member in WORKSPACE["members"] for path in ROOT.glob(member))
)
FIRST_PARTY: Final = frozenset(
    source.name
    for package in PACKAGES
    for source in package.iterdir()
    if source.name != "tests" and source.is_dir() and (source / "__init__.py").is_file()
)
OTEL_API_MODULES: Final = (
    "trace", "context", "propagate", "propagators", "metrics", "_logs", "_events", "baggage",
    "util", "attributes", "environment_variables", "version",
)
IMPORT_DISTRIBUTIONS: Final = {
    "PIL": "pillow",
    "yaml": "pyyaml",
    "typing_extensions": "typing-extensions",
    "edge_tts": "edge-tts",
    "gti": "god-tibo-imagen",
    "azure.core": "azure-core",
    "azure.storage.blob": "azure-storage-blob",
    "opentelemetry.sdk": "opentelemetry-sdk",
    "opentelemetry.exporter.otlp.proto.grpc": "opentelemetry-exporter-otlp-proto-grpc",
    **{f"opentelemetry.{module}": "opentelemetry-api" for module in OTEL_API_MODULES},
}
EXACT_IMPORT_DISTRIBUTIONS: Final = {"opentelemetry": "opentelemetry-api"}
IMPORT_ERRORS: Final = frozenset({"ImportError", "ModuleNotFoundError"})


@dataclass(frozen=True, slots=True)
class ImportSite:
    module: str
    line: int
    lazy: bool

    @property
    def distribution(self) -> str:
        if self.module in EXACT_IMPORT_DISTRIBUTIONS:
            return EXACT_IMPORT_DISTRIBUTIONS[self.module]
        # Longest prefix distinguishes distributions sharing a namespace package.
        for prefix in sorted(IMPORT_DISTRIBUTIONS, key=len, reverse=True):
            if self.module == prefix or self.module.startswith(prefix + "."):
                return IMPORT_DISTRIBUTIONS[prefix]
        return canonicalize_name(self.module.split(".")[0])


def guards_import_error(handler: ast.ExceptHandler) -> bool:
    """An ImportError handler that raises makes the import mandatory, not optional."""
    caught = {node.id for node in ast.walk(handler.type) if isinstance(node, ast.Name)} if handler.type else set()
    return bool(caught & IMPORT_ERRORS) and not any(isinstance(node, ast.Raise) for node in ast.walk(handler))


def import_sites(node: ast.AST, lazy: bool = False) -> Iterator[ImportSite]:
    """Keep function and import-guarded try-body imports separate; other control flow stays eager."""
    match node:
        case ast.Import(names=names):
            for name in names:
                yield ImportSite(name.name, node.lineno, lazy)
        case ast.ImportFrom(module=str() as module, level=0):
            yield ImportSite(module, node.lineno, lazy)
    if isinstance(node, (ast.Try, ast.TryStar)):
        guarded = lazy or any(guards_import_error(handler) for handler in node.handlers)
        for child in node.body:
            yield from import_sites(child, guarded)
        for child in (*node.handlers, *node.orelse, *node.finalbody):
            yield from import_sites(child, lazy)
        return
    # AST is an open hierarchy: non-import nodes are intentionally traversed generically.
    nested = lazy or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    for child in ast.iter_child_nodes(node):
        yield from import_sites(child, nested)


def missing_declarations(
    sites: Iterator[ImportSite], required: set[str], optional: set[str]
) -> list[str]:
    """Report each import against its own package, never the installed environment."""
    missing: list[str] = []
    for site in sites:
        root = site.module.split(".")[0]
        if root in sys.stdlib_module_names or root in FIRST_PARTY:
            continue
        declared = required | optional if site.lazy else required
        if site.distribution not in declared:
            section = "dependencies or optional-dependencies" if site.lazy else "dependencies"
            missing.append(
                f"{site.line}: import {site.module} -> {site.distribution} missing from {section}"
            )
    return missing


@pytest.mark.parametrize("package", PACKAGES, ids=lambda path: path.name)
def test_workspace_imports_have_own_distribution_declarations(package: Path) -> None:
    # Given the importing package's own metadata and only its production sources.
    project = tomllib.loads((package / "pyproject.toml").read_text())["project"]
    required = {canonicalize_name(Requirement(value).name) for value in project["dependencies"]}
    optional = {
        canonicalize_name(Requirement(value).name)
        for extra in project.get("optional-dependencies", {}).values()
        for value in extra
    }
    sources = sorted(
        path
        for source in package.iterdir()
        if source.name in FIRST_PARTY
        for path in source.rglob("*.py")
        if "tests" not in path.relative_to(source).parts and not path.name.startswith("test_")
    )

    # When parsing imports without importing/executing any application or SDK code.
    missing = [
        f"{path.relative_to(ROOT)}:{diagnostic}"
        for path in sources
        for diagnostic in missing_declarations(
            import_sites(ast.parse(path.read_bytes())), required, optional
        )
    ]

    # Then transitive installs cannot conceal a missing direct distribution declaration.
    assert sources, f"No production modules found in {package}"
    assert not missing, "\n".join(missing)


@pytest.mark.parametrize(
    "source",
    ["import PIL", "if True:\n import PIL", "class Example:\n import PIL"],
    ids=["module", "conditional", "class-body"],
)
def test_extras_cannot_satisfy_an_eager_import(source: str) -> None:
    # Given Pillow declared only in an extra, including module-scope control flow.
    sites = import_sites(ast.parse(source))

    # When checking the package's declarations.
    missing = missing_declarations(sites, set(), {"pillow"})

    # Then the base install is still missing a required distribution.
    assert len(missing) == 1
    assert "PIL -> pillow missing from dependencies" in missing[0]


@pytest.mark.parametrize(
    "source",
    [
        "def load():\n import boto3",
        "async def load():\n import boto3",
        "try:\n import boto3\nexcept ImportError:\n pass",
    ],
    ids=["function", "async-function", "try-block"],
)
def test_lazy_import_requires_a_direct_or_extra_declaration(source: str) -> None:
    # Given an optional SDK import guarded by a function or try block.
    tree = ast.parse(source)

    # When checking absent, required and optional declarations independently.
    absent = missing_declarations(import_sites(tree), set(), set())
    required = missing_declarations(import_sites(tree), {"boto3"}, set())
    optional = missing_declarations(import_sites(tree), set(), {"boto3"})

    # Then only an explicit declaration satisfies it (no transitive allowlist).
    assert len(absent) == 1
    assert required == optional == []


@pytest.mark.parametrize(
    "source",
    [
        "try:\n import PIL\nexcept ImportError as error:\n raise RuntimeError() from error",
        "try:\n import PIL\nexcept ValueError:\n pass",
        "try:\n pass\nexcept ValueError:\n import PIL",
        "try:\n pass\nexcept ImportError:\n pass\nelse:\n import PIL",
        "try:\n pass\nfinally:\n import PIL",
    ],
    ids=["reraising-handler", "other-exception", "handler-body", "else-block", "finally-block"],
)
def test_try_statement_hides_only_imports_guarded_by_an_import_error_handler(source: str) -> None:
    # Given Pillow declared only in an extra and a try statement that does not make it optional.
    sites = import_sites(ast.parse(source))

    # When checking the package's declarations.
    missing = missing_declarations(sites, set(), {"pillow"})

    # Then the import still needs a base dependency.
    assert len(missing) == 1
    assert "PIL -> pillow missing from dependencies" in missing[0]


@pytest.mark.parametrize("handler", ["ImportError", "ModuleNotFoundError", "(ValueError, ImportError)"])
def test_try_body_guarded_by_an_import_error_handler_may_use_an_extra(handler: str) -> None:
    # Given an optional SDK imported inside a try body that tolerates its absence.
    source = f"try:\n import boto3\nexcept {handler}:\n pass"

    # When the SDK is declared only as an extra.
    missing = missing_declarations(import_sites(ast.parse(source)), set(), {"boto3"})

    # Then the optional import is accepted.
    assert missing == []


@pytest.mark.parametrize(
    "source",
    [
        "import opentelemetry.instrumentation.fastapi",
        "from opentelemetry.exporter.otlp.proto.http.trace_exporter import Exporter",
        "from opentelemetry.semconv.trace import SpanAttributes",
    ],
    ids=["instrumentation", "http-exporter", "semconv"],
)
def test_opentelemetry_api_does_not_cover_other_opentelemetry_distributions(source: str) -> None:
    # Given only the API distribution declared.
    sites = import_sites(ast.parse(source))

    # When a module that another opentelemetry distribution provides is imported.
    missing = missing_declarations(sites, {"opentelemetry-api"}, set())

    # Then that import must declare its own distribution.
    assert len(missing) == 1


@pytest.mark.parametrize(
    "source",
    [
        "from opentelemetry import trace",
        "import opentelemetry.trace",
        "from opentelemetry.propagate import inject",
        "from opentelemetry.context import attach",
    ],
    ids=["package", "trace", "propagate", "context"],
)
def test_opentelemetry_api_modules_are_covered_by_the_api_distribution(source: str) -> None:
    # Given only the API distribution declared.
    sites = import_sites(ast.parse(source))

    # When a module that the API distribution itself provides is imported.
    missing = missing_declarations(sites, {"opentelemetry-api"}, set())

    # Then the declaration is sufficient.
    assert missing == []


def test_namespace_imports_resolve_to_the_distribution_that_owns_them() -> None:
    # Given namespaces that are split across separate distributions.
    source = "from azure.core import MatchConditions\nfrom azure.storage.blob import BlobServiceClient"

    # When collecting the import distributions.
    distributions = {site.distribution for site in import_sites(ast.parse(source))}

    # Then the blob SDK alone cannot hide a direct azure-core import.
    assert distributions == {"azure-core", "azure-storage-blob"}
