import io
import json
import logging

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
