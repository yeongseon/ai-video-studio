"""PostgreSQL and Redis run from exact-patch, digest-pinned images, identically everywhere (#1001).

`docker-compose.yml` is the source of truth. Every other place that starts these services
(CI service containers, `docker run` lines, scripts, test fixtures) must name the very same
reference, so a bumped patch or digest cannot be applied in one place and silently missed in
another, and no moving tag such as `postgres:16-alpine` or `latest` can come back.

The check is textual and covers tracked YAML, Python, shell, TOML, Makefile and Dockerfile
files. It does not read prose documentation (documentation-only pull requests run a reduced
CI matrix that does not include this module), and it cannot see a reference that is assembled
at run time or written without a tag.
"""

import re
import subprocess
from pathlib import Path
from typing import Final

import yaml

ROOT: Final = Path(__file__).resolve().parents[3]
SELF: Final = Path(__file__).resolve().relative_to(ROOT).as_posix()
SERVICES: Final = {"postgres": "postgres", "redis": "redis"}
SHAPE: Final = {
    "postgres": re.compile(r"postgres:16\.\d+-alpine@sha256:[0-9a-f]{64}"),
    "redis": re.compile(r"redis:7\.4\.\d+-alpine@sha256:[0-9a-f]{64}"),
}
# An image reference to either service: any tag (including latest) and an optional digest. A tag of
# three or more digits is a host port (`postgres:5432`), a bare `postgres:16` is a floating major
# tag, and a preceding `@`, `/` or `.` means the text is a URL or a path.
REFERENCE: Final = re.compile(
    r"(?<![\w/.@-])(?P<image>postgres|redis):(?P<tag>latest|\d+(?:\.\d+)*(?:-[A-Za-z0-9.]+)?)"
    r"(?P<digest>@sha256:[0-9a-f]{64})?"
)


def _compose() -> dict[str, dict[str, object]]:
    document = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return document["services"]


def _pin(name: str) -> str:
    return str(_compose()[SERVICES[name]]["image"])


def _tracked_text_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    paths = [ROOT / name for name in listing.split("\0") if name and name != SELF]
    return [
        path
        for path in paths
        if path.is_file()
        and (
            path.suffix in {".yml", ".yaml", ".py", ".sh", ".toml"}
            or path.name == "Makefile"
            or path.name.startswith("Dockerfile")
        )
    ]


def _references() -> list[tuple[str, int, str, str]]:
    found = []
    for path in _tracked_text_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            for match in REFERENCE.finditer(line):
                if match["tag"].isdigit() and len(match["tag"]) >= 3:
                    continue  # `postgres:5432`: a host port, not an image tag
                found.append((path.relative_to(ROOT).as_posix(), number, match["image"], match[0]))
    return found


def test_compose_pins_each_service_to_an_exact_patch_and_digest() -> None:
    # Given the deployment definition of the database and the broker.
    for name, pattern in SHAPE.items():
        # Then each is a supported major line, an exact patch and an immutable digest.
        assert pattern.fullmatch(_pin(name)), (name, _pin(name))


def test_every_reference_in_the_repository_matches_the_compose_pin() -> None:
    # Given every tracked place that names either image.
    references = _references()

    # When comparing each with the deployment definition.
    drift = [
        f"{path}:{number}: {reference} (expected {_pin(image)})"
        for path, number, image, reference in references
        if reference != _pin(image)
    ]

    # Then nothing runs a different patch, a moving tag or an unpinned reference.
    assert not drift, "\n".join(drift)
    seen = {image for _, _, image, _ in references}
    assert seen == set(SERVICES), f"expected references to {sorted(SERVICES)}, found {sorted(seen)}"


SCANNED_KINDS: Final = (
    ".github/workflows/ci.yml",
    "scripts/restore_drill.py",
    "scripts/release_lifecycle_acceptance.sh",
    "apps/api/Dockerfile",
    "apps/worker-orchestrator/Dockerfile",
    "Makefile",
)


def test_the_scan_reaches_every_kind_of_file_that_can_start_these_images() -> None:
    # Given the files the drift check reads.
    scanned = {path.relative_to(ROOT).as_posix() for path in _tracked_text_files()}

    # When looking for one file of each kind: workflow YAML, Python, shell, Dockerfile, Makefile.
    missing = sorted(set(SCANNED_KINDS) - scanned)

    # Then none is outside the scan, so a narrowed filter cannot silently blind the check.
    assert not missing, missing


def test_core_services_never_use_a_moving_image() -> None:
    # Given the services that start without a profile, i.e. the default local stack.
    core = {name: service for name, service in _compose().items() if "profiles" not in service}

    # When reading the images they pull.
    pulled = {name: str(service["image"]) for name, service in core.items() if "image" in service}

    # Then each is pinned by digest and none is `latest`. Optional profile images are separate work.
    assert pulled, "the default stack must name its database and broker images"
    for name, image in pulled.items():
        assert "@sha256:" in image and not image.split("@")[0].endswith(":latest"), (name, image)
