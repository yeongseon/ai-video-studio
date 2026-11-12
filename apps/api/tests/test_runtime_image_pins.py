"""PostgreSQL and Redis run from exact-patch, digest-pinned images, identically everywhere (#1001).

`docker-compose.yml` is the source of truth. Every other place that starts these services
(CI service containers, `docker run` lines, scripts, test fixtures) must name the very same
reference, so a bumped patch or digest cannot be applied in one place and silently missed in
another, and no moving tag such as `postgres:16-alpine` or `latest` can come back.

Every tracked YAML, Python, shell, TOML, Makefile and Dockerfile file is scanned as text for a
reference written with any tag, qualified by a registry or namespace
(`docker.io/library/postgres:...`), inside a `${NAME:-default}` default, or untagged or digest-only
on an `image:` key or a Dockerfile `FROM` line. Tracked YAML is also parsed, and every `image`
value naming either image is read whatever its quoting, flow style, anchor or Compose tag. Each
reference is compared byte for byte with the compose pin. A floor on the references each known
consumer keeps means a line rewritten into a spelling the text scan cannot read cannot silently
disappear.

It does not read prose documentation (documentation-only pull requests run a reduced CI matrix
that does not include this module). It cannot see a reference assembled at run time (`$IMAGE`),
or an untagged name on a `docker run` line at a place that is not a known consumer. It also
cannot tell whether a tag and its digest belong together: a tag edited without its digest,
consistently everywhere, passes, which is why the follow-up runtime check (#1021) asserts the
version each running server reports.
"""

import re
import subprocess
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
import yaml

ROOT: Final = Path(__file__).resolve().parents[3]
SELF: Final = Path(__file__).resolve().relative_to(ROOT).as_posix()
SERVICES: Final = {"postgres": "postgres", "redis": "redis"}
SHAPE: Final = {
    "postgres": re.compile(r"postgres:16\.\d+-alpine@sha256:[0-9a-f]{64}"),
    "redis": re.compile(r"redis:7\.4\.\d+-alpine@sha256:[0-9a-f]{64}"),
}
# An optional registry host (with a port) and namespace path in front of the image name, as in
# `docker.io/library/` or `registry.example.com:5000/mirror/`.
QUALIFIER: Final = r"(?:[\w.-]+(?::\d+)?/)*"
# A tagged reference to either image: any tag (`latest`, `alpine`, `16`, an exact patch) and an
# optional digest. The lookbehind starts a match only at a token boundary, so URLs
# (`redis://redis:6379`), credentials (`user:pass@postgres:5432`) and longer names
# (`short-form-studio-redis:7.4`) are not read as images. A tag of three or more digits is a host
# port and is skipped by `find_references`.
TAGGED: Final = re.compile(
    rf"(?<![\w/.@:-]){QUALIFIER}(?P<image>postgres|redis):(?P<tag>\w[\w.-]*)"
    r"(?P<digest>@sha256:[0-9a-f]{64})?"
)
# The same images with no tag, which Docker resolves to `latest`. A bare name is only unambiguous as
# the value of an `image:` key or on a Dockerfile `FROM` line, so only those positions count.
UNTAGGED: Final = re.compile(
    rf"^\s*(?:-\s+)?(?:image:|(?i:FROM)(?:\s+--\S+)*)\s+[\"']?"
    rf"(?P<ref>{QUALIFIER}(?P<image>postgres|redis)(?:@[^\s\"']+)?)[\"']?"
    r"(?i:\s+AS\s+\S+)?\s*(?:#.*)?$"
)
# An `image` value of a parsed YAML document. Here every tag counts (`redis:123` is a tag, not a
# port) and so does a digest with no tag, malformed or not, because the key itself says it is an image.
YAML_IMAGE: Final = re.compile(rf"{QUALIFIER}(?P<image>postgres|redis)(?::\w[\w.-]*)?(?:@\S+)?")
# `${NAME:-default}` and its `-`, `=`, `+` and `?` variants, with or without the colon: the default
# text is a reference that a reader (and Compose) will use, so it is scanned like any other.
INTERPOLATION: Final = re.compile(r"\$\{[A-Za-z_]\w*:?[-=+?]([^{}]*)\}")


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


def _unwrap_defaults(text: str) -> str:
    unwrapped = INTERPOLATION.sub(r" \1 ", text)
    return text if unwrapped == text else _unwrap_defaults(unwrapped)


def find_references(line: str) -> list[tuple[str, str]]:
    text = _unwrap_defaults(line)
    found = [
        (match["image"], match[0])
        for match in TAGGED.finditer(text)
        if not (match["tag"].isdigit() and len(match["tag"]) >= 3)
    ]
    bare = UNTAGGED.match(text)
    if bare:
        found.append((bare["image"], bare["ref"]))
    return found


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that reads Compose's custom tags (`!override`, `!reset`) as plain values."""


def _plain(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_TolerantLoader.add_multi_constructor("!", _plain)


def _image_values(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "image":
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, str):
                        yield item
            yield from _image_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _image_values(item)


def find_yaml_images(text: str) -> list[tuple[str, str]]:
    found = []
    for document in yaml.load_all(text, Loader=_TolerantLoader):
        for value in _image_values(document):
            reference = _unwrap_defaults(value).strip()
            match = YAML_IMAGE.fullmatch(reference)
            if match:
                found.append((match["image"], reference))
    return found


def _references() -> list[tuple[str, int, str, str]]:
    found = []
    for path in _tracked_text_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            for image, reference in find_references(line):
                found.append((path.relative_to(ROOT).as_posix(), number, image, reference))
    return found


def _yaml_files() -> list[Path]:
    return [path for path in _tracked_text_files() if path.suffix in {".yml", ".yaml"}]


def _structural_references() -> list[tuple[str, str, str]]:
    found = []
    for path in _yaml_files():
        for image, reference in find_yaml_images(path.read_text(encoding="utf-8")):
            found.append((path.relative_to(ROOT).as_posix(), image, reference))
    return found


def test_compose_pins_each_service_to_an_exact_patch_and_digest() -> None:
    # Given the deployment definition of the database and the broker.
    for name, pattern in SHAPE.items():
        # Then each is a supported major line, an exact patch and an immutable digest.
        assert pattern.fullmatch(_pin(name)), (name, _pin(name))


def test_every_reference_in_the_repository_matches_the_compose_pin() -> None:
    # Given every tracked place that names either image, as text and as a YAML image value.
    references = _references()
    structural = _structural_references()

    # When comparing each with the deployment definition.
    drift = [
        f"{path}:{number}: {reference} (expected {_pin(image)})"
        for path, number, image, reference in references
        if reference != _pin(image)
    ] + [
        f"{path}: image {reference} (expected {_pin(image)})"
        for path, image, reference in structural
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


KNOWN_CONSUMERS: Final = {
    ".github/workflows/ci.yml": 7,
    "docker-compose.yml": 2,
    "scripts/restore_drill.py": 1,
    "scripts/release_lifecycle_acceptance.sh": 1,
    "tests/test_restore_database_negative.py": 1,
}


def test_every_known_consumer_still_names_the_images() -> None:
    # Given the places known to start these images and the references the text scan finds in each.
    found = Counter(path for path, _number, _image, _reference in _references())

    # When comparing each place with the number of references it had when this guard was written.
    shrunk = {
        path: (found[path], floor) for path, floor in KNOWN_CONSUMERS.items() if found[path] < floor
    }

    # Then a reference rewritten into a spelling the guard cannot read cannot silently disappear.
    assert not shrunk, shrunk


def test_every_scanned_yaml_file_can_be_read_structurally() -> None:
    # Given every tracked YAML file the guard scans.
    unreadable = []

    # When each is parsed for its image values.
    for path in _yaml_files():
        try:
            find_yaml_images(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            unreadable.append(f"{path.relative_to(ROOT).as_posix()}: {error}")

    # Then none is skipped, so quoting, flow style or an anchor cannot hide an image key.
    assert not unreadable, "\n".join(unreadable)


def test_core_services_never_use_a_moving_image() -> None:
    # Given the services that start without a profile, i.e. the default local stack.
    core = {name: service for name, service in _compose().items() if "profiles" not in service}

    # When reading the images they pull.
    pulled = {name: str(service["image"]) for name, service in core.items() if "image" in service}

    # Then each is pinned by digest and none is `latest`. Optional profile images are separate work.
    assert pulled, "the default stack must name its database and broker images"
    for name, image in pulled.items():
        assert "@sha256:" in image and not image.split("@")[0].endswith(":latest"), (name, image)


DIGEST: Final = "sha256:" + "0123456789abcdef" * 4
PINNED_POSTGRES: Final = f"postgres:16.15-alpine@{DIGEST}"
PINNED_REDIS: Final = f"redis:7.4.12-alpine@{DIGEST}"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            f"image: {PINNED_POSTGRES}", [("postgres", PINNED_POSTGRES)], id="the-pin-itself"
        ),
        pytest.param(
            f"image: ${{PG_IMAGE:-{PINNED_POSTGRES}}}",
            [("postgres", PINNED_POSTGRES)],
            id="interpolation-default-equal-to-the-pin",
        ),
        pytest.param(
            "image: postgres:16-alpine", [("postgres", "postgres:16-alpine")], id="floating-major"
        ),
        pytest.param("docker run -d redis:latest", [("redis", "redis:latest")], id="latest"),
        pytest.param("image: redis:alpine", [("redis", "redis:alpine")], id="named-moving-tag"),
        pytest.param(
            "image: postgres:alpine3.24",
            [("postgres", "postgres:alpine3.24")],
            id="named-moving-tag-with-release",
        ),
        pytest.param(
            "image: docker.io/library/postgres:latest",
            [("postgres", "docker.io/library/postgres:latest")],
            id="docker-hub-qualified",
        ),
        pytest.param(
            f"image: library/redis:7.4.12-alpine@{DIGEST}",
            [("redis", f"library/redis:7.4.12-alpine@{DIGEST}")],
            id="qualified-alias-of-a-pinned-form",
        ),
        pytest.param(
            "image: registry.example.com:5000/mirror/postgres:16",
            [("postgres", "registry.example.com:5000/mirror/postgres:16")],
            id="private-registry",
        ),
        pytest.param(
            "image: ${PG_IMAGE:-postgres:16}",
            [("postgres", "postgres:16")],
            id="interpolation-default",
        ),
        pytest.param(
            "image: ${REDIS_IMAGE:-redis:7.4-alpine}",
            [("redis", "redis:7.4-alpine")],
            id="interpolation-default-floating-minor",
        ),
        pytest.param('image: "postgres"', [("postgres", "postgres")], id="untagged-image-key"),
        pytest.param(
            "  - image: redis  # cache", [("redis", "redis")], id="untagged-list-item-with-comment"
        ),
        pytest.param(
            "image: ${REDIS_IMAGE:-redis}",
            [("redis", "redis")],
            id="untagged-interpolation-default",
        ),
        pytest.param("FROM postgres", [("postgres", "postgres")], id="untagged-from"),
        pytest.param(
            f"image: redis@{DIGEST}", [("redis", f"redis@{DIGEST}")], id="digest-only-image-key"
        ),
        pytest.param(f"FROM redis@{DIGEST}", [("redis", f"redis@{DIGEST}")], id="digest-only-from"),
        pytest.param(
            "FROM redis@sha256:abc",
            [("redis", "redis@sha256:abc")],
            id="malformed-digest-only-from",
        ),
        pytest.param(
            "image: ${A:-${B:-redis}}", [("redis", "redis")], id="untagged-nested-defaults"
        ),
        pytest.param(
            "FROM --platform=linux/amd64 library/redis AS cache",
            [("redis", "library/redis")],
            id="untagged-qualified-from-with-flag-and-alias",
        ),
        pytest.param(
            f"image: postgres:16.15-alpine@sha256:{'0' * 63}",
            [("postgres", "postgres:16.15-alpine")],
            id="truncated-digest-counts-as-no-digest",
        ),
    ],
)
def test_matcher_reports_every_form_that_names_these_images(
    line: str, expected: list[tuple[str, str]]
) -> None:
    # Given one line of a tracked file that names one of the two images in some spelling.
    # When the guard reads it.
    found = find_references(line)

    # Then the reference is reported exactly as written, so comparing it with the pin can reject it.
    assert found == expected


@pytest.mark.parametrize(
    "line",
    [
        "DATABASE_URL=postgresql://postgres:secret@postgres:5432/app",
        "REDIS_URL=redis://redis:6379/0",
        "  postgres:",
        "POSTGRES_USER: review",
        "depends_on: [postgres, redis]",
        "      - postgres",
        "host=postgres port=5432",
        "DB_ADDR=postgres:5432",
        "REDIS_ADDR=redis:6379",
        "redis-cli ping",
        "image: redis/redis-stack:latest",
        "image: short-form-studio-redis:7.4",
        "image: ollama/ollama:0.6.2",
        "# postgres: 16 is the supported major",
    ],
)
def test_matcher_ignores_text_that_is_not_an_image_reference(line: str) -> None:
    # Given a connection string, a service key, a host name, a port or a different image.
    # When the guard reads it.
    found = find_references(line)

    # Then nothing is reported, so the stricter spellings above cannot cause false alarms.
    assert found == []


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            f"image: {PINNED_POSTGRES}\n", [("postgres", PINNED_POSTGRES)], id="the-pin-itself"
        ),
        pytest.param('"image": postgres\n', [("postgres", "postgres")], id="quoted-key"),
        pytest.param(
            "services: {postgres: {image: postgres, env: {A: b}}}\n",
            [("postgres", "postgres")],
            id="flow-mapping",
        ),
        pytest.param(
            'image: "redis:alpine"  # cache\n',
            [("redis", "redis:alpine")],
            id="quoted-value-with-comment",
        ),
        pytest.param(
            "x: &pg postgres\ny:\n  image: *pg\n", [("postgres", "postgres")], id="anchor-alias"
        ),
        pytest.param(f"image: redis@{DIGEST}\n", [("redis", f"redis@{DIGEST}")], id="digest-only"),
        pytest.param(
            "image: redis@sha256:abc\n", [("redis", "redis@sha256:abc")], id="malformed-digest-only"
        ),
        pytest.param(
            "image: docker.io/library/postgres:latest\n",
            [("postgres", "docker.io/library/postgres:latest")],
            id="qualified-latest",
        ),
        pytest.param(
            "image: redis:123\n", [("redis", "redis:123")], id="numeric-tag-is-a-tag-here"
        ),
        pytest.param(
            "image: ${A:-${B:-redis}}\n", [("redis", "redis")], id="untagged-nested-defaults"
        ),
        pytest.param(
            "matrix:\n  image: [postgres:16, redis]\n",
            [("postgres", "postgres:16"), ("redis", "redis")],
            id="matrix-list",
        ),
        pytest.param(
            'services:\n  postgres:\n    ports: !override ["127.0.0.1:5432:5432"]\n'
            "    image: postgres:16\n",
            [("postgres", "postgres:16")],
            id="compose-custom-tag",
        ),
        pytest.param(
            "image: postgres\n---\nimage: redis:7\n",
            [("postgres", "postgres"), ("redis", "redis:7")],
            id="several-documents",
        ),
    ],
)
def test_yaml_reader_reports_every_image_value_that_names_these_images(
    document: str, expected: list[tuple[str, str]]
) -> None:
    # Given a YAML document that sets an image key in some presentation.
    # When the guard parses it.
    found = find_yaml_images(document)

    # Then the value is reported as written, whatever the quoting, flow style or anchor.
    assert found == expected


@pytest.mark.parametrize(
    "document",
    [
        "image: redis/redis-stack:latest\n",
        "image: ghcr.io/${{ github.repository }}/api@${{ needs.build.outputs.digest }}\n",
        "images: [postgres, redis]\n",
        "image:\n",
        "command: docker run postgres\n",
        "services:\n  postgres:\n    environment:\n      POSTGRES_DB: app\n",
    ],
)
def test_yaml_reader_ignores_values_that_are_not_these_images(document: str) -> None:
    # Given a different image, an unrelated key, an empty value or a service name.
    # When the guard parses it.
    found = find_yaml_images(document)

    # Then nothing is reported.
    assert found == []
