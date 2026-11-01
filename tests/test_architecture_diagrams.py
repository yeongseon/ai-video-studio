from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def diagram(kind: str, body: str) -> str:
    return f'<!-- diagram: {kind} -->\n```mermaid\nflowchart LR\n{body}\n```\n'


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('SERVICE["creator-service"]\nPROVIDER["creator-provider"]\nSERVICE -->|"IMPORT"| PROVIDER', "forbidden-dependency"),
        ('DOMAIN["creator-domain"]\nSERVICE["creator-service"]\nDOMAIN -->|"IMPORT"| SERVICE', "forbidden-dependency"),
        ('SERVICE["creator-service"]\nAPI["apps/api"]\nSERVICE -->|"IMPORT"| API', "forbidden-dependency"),
        ('SERVICE["creator-service"]\nSERVICE -->|"IMPORT"| MISSING', "unknown node"),
        ('SERVICE["creator-service"]\nSERVICE["creator-service"]', "duplicate node"),
        ('SERVICE["creator-service"]\nDOMAIN["creator-domain"]\nSERVICE --> DOMAIN', "IMPORT label"),
        ('SERVICE["creator-service"', "syntax"),
    ],
)
def test_rejects_static_diagram_when_dependency_or_syntax_is_invalid(
    body: str, error: str, tmp_path: Path
) -> None:
    # Given a single mutated dependency graph, not application mocks.
    from scripts.check_architecture_diagrams import check_document

    # When the documented graph crosses the checker boundary.
    issues = check_document(diagram("static-imports", body), tmp_path / "doc.md", tmp_path)
    # Then the violated contract is reported.
    assert any(error in issue for issue in issues), issues


def test_accepts_static_diagram_when_apps_compose_independent_packages(tmp_path: Path) -> None:
    # Given legal app composition and a downward package import.
    from scripts.check_architecture_diagrams import check_document

    text = diagram("static-imports", 'API["apps/api"]\nSERVICE["creator-service"]\nDOMAIN["creator-domain"]\nAPI -->|"IMPORT"| SERVICE\nSERVICE -->|"IMPORT"| DOMAIN')
    # When the graph is checked using the package policy.
    issues = check_document(text, tmp_path / "doc.md", tmp_path)
    # Then allowed composition is not mistaken for a lateral dependency.
    assert issues == ()


@pytest.mark.parametrize(
    ("mutation", "error"),
    [("beat", "mandatory"), ("gpu", "optional"), ("chunk", "planned"), ("path", "source path"), ("service", "Compose service")],
)
def test_rejects_runtime_when_configuration_contract_is_mutated(
    mutation: str, error: str, tmp_path: Path
) -> None:
    # Given a real miniature Compose file and source table.
    from scripts.check_architecture_diagrams import check_document

    services = ("api", "worker", "beat", "postgres", "redis", "studio-web")
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n" + "".join(f"  {name}:\n    image: example\n" for name in services)
        + '  ollama:\n    profiles: ["gpu"]\n', encoding="utf-8"
    )
    ids = tuple(name.replace("-", "_") for name in services)
    body = "\n".join(f'{node}["{node}"]' for node in ids if not (mutation == "beat" and node == "beat"))
    table = "\n".join(f"| {node} | docker-compose.yml#{service} | Component |" for node, service in zip(ids, services, strict=True))
    if mutation == "gpu":
        body += '\nmodel["Ollama mandatory"]'
        table += "\n| model | docker-compose.yml#ollama | Model |"
    if mutation == "chunk":
        body += '\nchunk["chunk-render"]'
    if mutation == "path":
        table = table.replace("docker-compose.yml#api", "missing.py")
    if mutation == "service":
        table = table.replace("docker-compose.yml#api", "docker-compose.yml#absent")
    # When one topology fact is broken.
    issues = check_document(diagram("runtime-compose", body) + table, tmp_path / "doc.md", tmp_path)
    # Then it cannot be represented as a valid current topology.
    assert any(error in issue for issue in issues), issues


@pytest.mark.parametrize("target", ["missing.md", "target.md#absent"])
def test_rejects_link_when_relative_file_or_anchor_is_missing(target: str, tmp_path: Path) -> None:
    # Given a local target with a different anchor.
    from scripts.check_architecture_diagrams import check_document

    (tmp_path / "target.md").write_text("# Existing\n", encoding="utf-8")
    # When a broken file or fragment is referenced.
    issues = check_document(f"[target]({target})", tmp_path / "doc.md", tmp_path)
    # Then the broken link is actionable.
    assert any("broken relative link" in issue for issue in issues)


@pytest.mark.parametrize("text", ['```mermaid\n```', diagram("planned", ""), diagram("planned", 'A["unbalanced]'), '<!-- diagram: planned -->\n```mermaid\nunknownDiagram\n```'])
def test_rejects_fence_when_annotation_header_or_content_is_invalid(text: str, tmp_path: Path) -> None:
    # Given malformed Mermaid content.
    from scripts.check_architecture_diagrams import check_document

    # When fences are extracted and parsed.
    issues = check_document(text, tmp_path / "doc.md", tmp_path)
    # Then empty or unrecognized content does not silently pass.
    assert issues


@pytest.mark.parametrize("kind", ["flow-generated", "flow-timeline"])
def test_rejects_flow_when_final_review_is_missing(kind: str, tmp_path: Path) -> None:
    # Given a creation flow without its final approval gate.
    from scripts.check_architecture_diagrams import check_document

    # When the flow contract is checked.
    issues = check_document(diagram(kind, 'start["Start"]'), tmp_path / "doc.md", tmp_path)
    # Then a missing approval cannot validate.
    assert any("FINAL_REVIEW" in issue for issue in issues)


def test_cli_checks_repository_when_invoked_outside_checkout(tmp_path: Path) -> None:
    # Given the real document set and a different working directory.
    # When the stdlib-only CLI validates the checked-in diagrams and links.
    result = subprocess.run([sys.executable, str(ROOT / "scripts/check_architecture_diagrams.py")], cwd=tmp_path, capture_output=True, text=True, check=False)
    # Then the repository's canonical sources pass through the actual CLI surface.
    assert result.returncode == 0, result.stdout + result.stderr
