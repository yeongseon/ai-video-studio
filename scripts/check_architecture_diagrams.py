#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# ─── How to run ───
# uv run scripts/check_architecture_diagrams.py [repository-root]
# CI (stdlib only): python3 scripts/check_architecture_diagrams.py
"""Check the bounded Mermaid convention documented in SYSTEM_TOPOLOGY.md, not rendering."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final
from urllib.parse import unquote, urlsplit

if __package__:
    from scripts.check_package_dependencies import PACKAGES, check_source
else:
    from check_package_dependencies import PACKAGES, check_source

DOCUMENTS: Final = (
    "README.md", "docs/architecture/SYSTEM_TOPOLOGY.md",
    "docs/adr/001-package-dependency-rules.md", "docs/architecture/SHORT_FIRST_MEDIA_CORE.md",
    "docs/architecture/LONG_FORM_EXTENSION.md", "docs/DEPLOYMENT.md", "docs/README.md",
)
KINDS: Final = frozenset({"static-imports", "runtime-compose", "flow-generated", "flow-timeline", "planned", "deployment-contract"})
BASE: Final = frozenset({"api", "worker", "beat", "postgres", "redis", "studio-web"})
NODE: Final = re.compile(r'([A-Za-z_][\w]*)\["([^"\n]+)"\]')
EDGE: Final = re.compile(r'([A-Za-z_]\w*)\s+(-->|-\.->)(?:\|"([^"\n]+)"\|)?\s+([A-Za-z_]\w*)')
FENCE: Final = re.compile(r'^```mermaid[^\n]*\n(.*?)^```\s*$', re.M | re.S)
MAP: Final = re.compile(r'^\|\s*(\w+)\s*\|\s*([^|\s]+)\s*\|', re.M)


def check_graph(body: str, kind: str) -> tuple[str, ...]:
    """Parse declarations/edges; full-line matching enforces balanced quoted brackets."""
    issues: list[str] = []
    lines = body.strip().splitlines()
    if not lines or not re.fullmatch(r"(?:flowchart|graph) (?:LR|RL|TB|TD|BT)", lines[0]):
        return ("syntax: missing recognized diagram header",)
    nodes: dict[str, str] = {}
    edges: list[tuple[str, str, str]] = []
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith("%%"):
            continue
        node = NODE.fullmatch(line)
        edge = EDGE.fullmatch(line)
        if node:
            name, label = node.groups()
            if name in nodes:
                issues.append(f"duplicate node: {name}")
            nodes[name] = label
        elif edge:
            source, _, label, target = edge.groups()
            edges.append((source, label or "", target))
        else:
            issues.append(f"syntax: unsupported or unbalanced statement: {line}")
    if not nodes:
        issues.append("empty diagram: no declared nodes")
    for source, label, target in edges:
        if source not in nodes or target not in nodes:
            issues.append(f"unknown node: {source} -> {target}")
            continue
        if kind in {"static-imports"}:
            if label != "IMPORT":
                issues.append("static edges require IMPORT label")
            owner = nodes[source].replace("-", "_")
            dependency = nodes[target].replace("-", "_")
            if owner in PACKAGES:
                dependency = {"apps/api": "shorts_api", "apps/worker_orchestrator": "tasks"}.get(dependency, dependency)
                issues.extend(issue.reason for issue in check_source(f"import {dependency}", f"{owner}.diagram"))
            elif nodes[source] not in {"apps/api", "apps/worker-orchestrator"} or dependency not in PACKAGES:
                issues.append(f"unknown static dependency: {source} -> {target}")
    if kind in {"static-imports"}:
        allowed = {package.replace("_", "-") for package in PACKAGES} | {"apps/api", "apps/worker-orchestrator"}
        if set(nodes.values()) - allowed or len(set(nodes.values())) != len(nodes):
            issues.append("unknown or duplicate static package labels")
    if kind not in {"planned"} and re.search(r"planned|deferred|chunk[ _-]*render|\bsequence\b", body, re.I):
        issues.append("planned-only concept in current diagram")
    required = {
        "flow-generated": {"SCRIPT_REVIEW", "VISUAL_PLAN_REVIEW", "VISUAL_ASSET_REVIEW", "FINAL_REVIEW"},
        "flow-timeline": {"TIMELINE_REVIEW", "FINAL_REVIEW", "revision_rejection"},
    }.get(kind, set())
    if required - nodes.keys():
        issues.append(f"missing flow gates: {', '.join(sorted(required - nodes.keys()))}")
    if kind.startswith("flow-") and re.search(r"external[ _-]*publish|social[ _-]*(upload|publish)", body, re.I):
        issues.append("external publish is not an implemented flow")
    return tuple(issues)


def check_runtime(body: str, text: str, root: Path) -> tuple[str, ...]:
    """Check source-map paths and the bounded two-space Compose service/profile layout."""
    issues: list[str] = []
    nodes = dict(NODE.findall(body))
    entries: dict[str, str] = {}
    for node, source in MAP.findall(text):
        if node in entries:
            issues.append(f"duplicate source node: {node}")
        entries[node] = source
    services: set[str] = set()
    for node, label in nodes.items():
        source = entries.get(node)
        if source is None:
            issues.append(f"missing source path: {node}")
            continue
        path, _, service = source.partition("#")
        target = root / path
        if not target.exists():
            issues.append(f"missing source path: {path}")
            continue
        if service:
            compose = target.read_text(encoding="utf-8")
            block = re.search(rf"^  {re.escape(service)}:\s*\n(.*?)(?=^  \S|^\S|\Z)", compose, re.M | re.S)
            if not block:
                issues.append(f"unknown Compose service: {service}")
                continue
            services.add(service)
            if re.search(r"^    profiles:", block[1], re.M) and ("optional" not in label.lower() or "mandatory" in label.lower()):
                issues.append(f"profile node must be optional: {node}")
    if BASE - services:
        issues.append(f"missing mandatory base Compose nodes: {', '.join(sorted(BASE - services))}")
    return tuple(issues)


def anchors(text: str) -> set[str]:
    """Approximate GitHub heading slugs, including duplicate-heading suffixes."""
    result: set[str] = set(re.findall(r'<a\s+(?:id|name)="([^"]+)"', text))
    counts: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        result.add(f"{slug}-{count}" if count else slug)
        counts[slug] = count + 1
    return result


def check_links(text: str, path: Path) -> tuple[str, ...]:
    """Check inline/reference Markdown links outside code fences; never fetch external URLs."""
    prose = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", text, flags=re.M | re.S)
    links = re.findall(r"!?\[[^\]]*\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)", prose)
    links += re.findall(r"^\s*\[[^\]]+\]:\s*(\S+)", prose, re.M)
    issues: list[str] = []
    for link in links:
        parsed = urlsplit(link.strip("<>"))
        if parsed.scheme or parsed.netloc:
            continue
        target = path.parent / unquote(parsed.path) if parsed.path else path
        if not target.exists() or (parsed.fragment and target.suffix == ".md" and unquote(parsed.fragment) not in anchors(target.read_text(encoding="utf-8"))):
            issues.append(f"broken relative link: {link}")
    return tuple(issues)


def check_document(text: str, path: Path, root: Path) -> tuple[str, ...]:
    """Validate all Mermaid fences and local links in one source document."""
    issues = list(check_links(text, path))
    kinds: set[str] = set()
    matches = list(FENCE.finditer(text))
    if len(matches) != len(re.findall(r"^```mermaid", text, re.M)):
        issues.append("syntax: unterminated Mermaid fence")
    for fence in matches:
        annotation = re.search(r"<!-- diagram: ([\w-]+) -->\s*$", text[:fence.start()])
        if annotation is None or annotation[1] not in KINDS:
            issues.append("missing or unknown diagram annotation")
            continue
        kind = annotation[1]
        kinds.add(kind)
        issues.extend(check_graph(fence[1], kind))
        if kind in {"runtime-compose"}:
            issues.extend(check_runtime(fence[1], text, root))
    if path.name in {"README.md", "SYSTEM_TOPOLOGY.md"} and path.parent.name != "docs":
        required = {"runtime-compose", "static-imports", "flow-generated", "flow-timeline"}
        if required - kinds:
            issues.append(f"missing canonical entry diagrams: {', '.join(sorted(required - kinds))}")
    return tuple(issues)


def main() -> int:
    if len(sys.argv) > 2 or any(arg.startswith("-") for arg in sys.argv[1:]):
        print("Usage: python3 scripts/check_architecture_diagrams.py [repository-root]")
        return 2
    root = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else Path(__file__).resolve().parents[1]
    diagnostics: list[str] = []
    for name in DOCUMENTS:
        path = root / name
        try:
            issues = check_document(path.read_text(encoding="utf-8"), path, root)
        except (OSError, UnicodeError) as error:
            issues = (f"cannot check document: {error}",)
        diagnostics.extend(f"{name}: {issue}" for issue in issues)
    for diagnostic in diagnostics:
        print(diagnostic)
    print(f"Architecture: {len(diagnostics)} blocking diagnostic(s); bounded syntax, policy, sources and links (not a renderer).")
    return int(bool(diagnostics))


if __name__ == "__main__":
    raise SystemExit(main())
