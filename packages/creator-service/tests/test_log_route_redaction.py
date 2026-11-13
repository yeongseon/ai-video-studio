import io
import json
import logging
from uuid import uuid4

import pytest
from creator_service.logging_config import JsonFormatter


ROUTES = [
    "/api/creator/runs/12/scenes",
    "/api/creator/workspaces/1/assets/2/content",
    "/api/creator/projects/1/timeline/preview",
    "/api/admin/queue/depth",
    "/release/worker-identity",
    "/docs/oauth2-redirect",
]
FILES = [
    "/app/data/artifacts/1/2/x.png", "/tmp/private/file", "/home/user/private",
    "workspaces/private/file", "C:\\private\\file.txt",
    "/api/creator-private/file", "/api/admin-private/file", "/release-private/file",
    "/artifacts/1/private/file.png", "/api/artifacts/files/1/private/file.png",
]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("template", ["%s %s %d %.1fms", "Unhandled exception on %s %s"])
def test_routes_survive_when_logged_with_real_api_call_shapes(route: str, template: str) -> None:
    # Given the access/error call shapes from app_factory, through a real logger and handler.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("api"))
    logger = logging.Logger("shorts_api.app_factory")
    logger.addHandler(handler)
    args = ("GET", route, 200, 3.2) if "%d" in template else ("GET", route)

    # When
    logger.info(template, *args)

    # Then the route and status/timing diagnostics survive unchanged.
    assert json.loads(stream.getvalue())["message"] == template % args


@pytest.mark.parametrize("path", FILES)
def test_route_survives_when_filesystem_paths_share_the_message_and_extras(path: str) -> None:
    # Given a route next to filesystem diagnostics, including nested extra fields.
    route = "/api/creator/workspaces/1/assets"
    record = logging.makeLogRecord({"msg": "%s failed at %s", "args": (route, path),
                                    "detail": {"route": route, "file": path}})
    # When
    raw = JsonFormatter("api").format(record)
    # Then only the filesystem path is hidden.
    assert path not in raw
    assert json.loads(raw)["message"] == route + " failed at <redacted>"
    assert json.loads(raw)["detail"] == {"route": route, "file": "<redacted>"}


@pytest.mark.parametrize("credential", [
    "sk-EXAMPLEEXAMPLE", "gsk_syntheticPrivate_123456789", "ghp_syntheticPrivate123456",
    "xoxb-syntheticPrivate123456", "AKIA1234567890123456", "AIzaSyntheticPrivate_123456",
    "_sk-EXAMPLEEXAMPLE", "id_sk-EXAMPLEEXAMPLE", "0123456789abcdef0123456789abcdef",
    "Bearer synthetic.private_token-123+/=", "bEaReR\tsynthetic.private_token-123",
    "https://user:pass@host.example/x?api_key=SECRETVALUE12345",
    "postgresql://synthetic:private-password@db.invalid:5432/studio",
    ".redis://:private-password@redis.invalid:6379/0",
    "redis://:private-password@redis.invalid:6379/0",
])
def test_route_preservation_never_exempts_credentials(credential: str) -> None:
    # Given credentials within an otherwise preserved route or beside it.
    route = "/api/creator/runs/12/scenes"
    record = logging.makeLogRecord({"msg": route + " " + credential,
                                    "detail": "/api/creator/runs/12/" + credential})
    # When
    raw = JsonFormatter("api").format(record)
    # Then the route survives, but neither message nor extras expose the credential.
    assert credential not in raw
    assert json.loads(raw)["message"].startswith(route + " ")
    assert "<redacted>" in json.loads(raw)["message"]
    assert "<redacted>" in json.loads(raw)["detail"]


def test_health_route_survives_alongside_multisegment_routes() -> None:
    # Given both the one-segment health endpoints and a multi-segment route.
    message = "GET /health /healthz /api/admin/health"
    # When
    raw = JsonFormatter("api").format(logging.makeLogRecord({"msg": message}))
    # Then
    assert json.loads(raw)["message"] == message


@pytest.mark.parametrize("template,tail", [
    ("upload failed for workspaces/7/assets/{hex}-Q3_layoffs_board_deck.mp4",
     "-Q3_layoffs_board_deck.mp4"),
    ("[Errno 2] No such file or directory: "
     "'/app/data/workspaces/7/assets/{hex}-patient_jane_doe_mri.mp4'",
     "-patient_jane_doe_mri.mp4"),
    ("/app/data/{hex}/home_address_notes.txt", "/home_address_notes.txt"),
    ("/app/data/sk-proj-abcdef/file.txt", "/file.txt"),
])
def test_filesystem_tail_is_hidden_when_path_contains_credentials(template: str, tail: str) -> None:
    # Given real upload storage-key shapes and a token-bearing filesystem path.
    message = template.format(hex=uuid4().hex)
    record = logging.makeLogRecord({"msg": message, "detail": {"file": message}})

    # When the JSON boundary formats both message and nested extras.
    raw = JsonFormatter("api").format(record)

    # Then redacting a credential must not expose the remaining filename or path suffix.
    assert tail not in raw
    assert "<redacted>" in raw


@pytest.mark.parametrize("path,private_tail", [
    ("/api/creator/../../app/data/secret.txt", "secret.txt"),
    ("/api/admin/../../../etc/shadow", "shadow"),
    ("/release/../../root/.aws/credentials", "credentials"),
    ("/api/creator/..%2F..%2Fetc%2Fpasswd", "passwd"),
    ("/api/creator/x/app/data/artifacts/1/2/x.png", "x.png"),
    ("/home/user/my dir/api/admin/keys.json", "keys.json"),
    ("/home/u/~/release/creds.txt", "creds.txt"),
    ("/api/creator/runs/1/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc", "eyJhbGci"),
    ("/api/creator/hf_abcdefghijklmnopqrstuvwxyz/x", "hf_abcdefghijklmnopqrstuvwxyz"),
    ("/api/creator/r8_abcdefghijklmnopqrstuvwxyz/x", "r8_abcdefghijklmnopqrstuvwxyz"),
    ("/api/creator/sk-short/x", "sk-short"),
    ("/api/creator/0123456789abcdef0123456789abcdef/x", "/x"),
    ("/api/creator//private_upload", "private_upload"),
    ("/release/private_upload", "private_upload"),
])
def test_route_like_path_is_hidden_when_shape_is_unsafe(path: str, private_tail: str) -> None:
    # Given an untrusted path resembling an allowed route in message and nested extras.
    record = logging.makeLogRecord({"msg": path, "detail": {"file": path}})
    # When
    raw = JsonFormatter("api").format(record)
    # Then neither the route-like path nor its private tail survives.
    assert private_tail not in raw
    assert "<redacted>" in raw


@pytest.mark.parametrize("route", [
    "/api/creator", "/api/admin", "/api/creator/projects/slug_42-a/",
    "/release/identity", "/health", "/docs", "/redoc", "/openapi.json",
])
def test_safe_route_survives_when_shape_matches_exactly(route: str) -> None:
    # Given / When
    raw = JsonFormatter("api").format(logging.makeLogRecord({"msg": route}))
    # Then
    assert json.loads(raw)["message"] == route
