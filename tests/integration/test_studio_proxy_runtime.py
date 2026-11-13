from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from http.client import HTTPConnection
import json
from pathlib import Path
import socket
import struct
import subprocess
import time

import httpx
import pytest

from studio_proxy_runtime_support import StudioProxyRuntime, studio_proxy_runtime
from studio_header_proxy_support import header_proxy


@pytest.mark.parametrize("method, path", [
    ("POST", "/api/admin/cache/clear"),
    ("POST", "/api/creator/projects"),
    ("POST", "/api/creator/workspaces/1/assets"),
    ("POST", "/api/creator/workspaces/1/assets/audio"),
    ("POST", "/api/creator/workspaces/1/assets/videos"),
    ("GET", "/health"), ("GET", "/healthz"),
    ("GET", "/docs"), ("GET", "/openapi.json"),
])
def test_nginx_omits_client_admin_key_at_upstream(
    header_proxy: tuple[str, str], method: str, path: str,
) -> None:
    # Given the shipped template served by real nginx, with a wire-level header echo upstream.
    origin, token = header_proxy
    # When a same-origin client forges admin authorization and destructive confirmation.
    with httpx.Client(base_url=origin, timeout=5) as client:
        response = client.request(method, path, headers={
            "Origin": origin, "X-Admin-Key": "synthetic-forged-admin-key-1036",
            "X-Confirm-Action": "yes",
        })
    # Then admin credentials are absent on the wire, while the creator-key policy is unchanged.
    assert response.status_code == 200
    received = {name.lower(): value for name, value in response.json().items()}
    assert "x-admin-key" not in received
    assert received["x-confirm-action"] == "yes"
    if path != "/healthz":
        assert received["x-api-key"] == token


@pytest.mark.parametrize("header_names", [
    ("x-admin-key",), ("X-ADMIN-KEY",), ("X-Admin-Key", "X-Admin-Key"),
    ("X_Admin_Key",), ("X-Admin-Key",),
])
def test_nginx_omits_admin_header_variants_without_origin(
    header_proxy: tuple[str, str], header_names: tuple[str, ...],
) -> None:
    # Given the real nginx proxy and a plain curl-style admin GET without Origin.
    origin, token = header_proxy
    # When header names, including duplicate fields, arrive on the wire unchanged.
    with closing(HTTPConnection(origin.removeprefix("http://"), timeout=5)) as connection:
        connection.putrequest("GET", "/api/admin/health")
        for name in header_names:
            connection.putheader(name, "synthetic-forged-admin-key-1036")
        connection.putheader("X-Confirm-Action", "yes")
        connection.endheaders()
        with connection.getresponse() as response:
            received = {name.lower(): value for name, value in json.loads(response.read()).items()}
            assert response.status == 200
    # Then neither spelling reaches the upstream, while creator auth still does.
    assert "x-admin-key" not in received
    assert "x_admin_key" not in received
    assert received["x-api-key"] == token


@pytest.mark.parametrize("header_proxy", [True], indirect=True)
@pytest.mark.parametrize("header_names", [
    ("x-admin-key",), ("X-ADMIN-KEY",), ("X-Admin-Key", "X-Admin-Key"), ("X-Admin-Key",),
])
def test_admin_header_wire_assertion_detects_missing_strip(
    header_proxy: tuple[str, str], header_names: tuple[str, ...],
) -> None:
    # Given only a rendered temporary template has the general API strip removed.
    # When / Then the same wire assertion must detect leaked admin credentials.
    with pytest.raises(AssertionError, match="x-admin-key"):
        test_nginx_omits_admin_header_variants_without_origin(header_proxy, header_names)


@dataclass(frozen=True, slots=True)
class UploadBoundary:
    kind: str
    route_suffix: str
    api_limit: int
    nginx_limit: int
    filename: str
    content_type: str


UPLOAD_BOUNDARIES = (
    UploadBoundary("image", "assets", 25 * 1024 * 1024, 26 * 1024 * 1024, "image.png", "image/png"),
    UploadBoundary("audio", "assets/audio", 100 * 1024 * 1024, 101 * 1024 * 1024, "audio.mp3", "audio/mpeg"),
    UploadBoundary("video", "assets/videos", 500 * 1024 * 1024, 501 * 1024 * 1024, "video.mp4", "video/mp4"),
)


def _seed_media(path: Path, boundary: UploadBoundary, size: int) -> None:
    match boundary.kind:
        case "image":
            from PIL import Image

            Image.new("RGB", (16, 16), (10, 20, 30)).save(path, format="PNG")
        case "audio":
            data_size = size - 44
            path.write_bytes(
                b"RIFF"
                + struct.pack("<I", data_size + 36)
                + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, 8_000, 16_000, 2, 16)
                + b"data"
                + struct.pack("<I", data_size)
            )
        case "video":
            subprocess.run(
                [
                    "ffmpeg", "-y", "-nostdin", "-f", "lavfi", "-i", "color=size=16x16:duration=0.1",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
                ],
                check=True, capture_output=True, timeout=30,
            )
        case unreachable:
            raise AssertionError(f"unsupported test media kind: {unreachable}")


def _sized_media(tmp_path: Path, boundary: UploadBoundary, size: int) -> Path:
    upload = tmp_path / f"{size}-{boundary.filename}"
    _seed_media(upload, boundary, size)
    with upload.open("r+b") as stream:
        stream.truncate(size)
    return upload


def _upload(runtime: StudioProxyRuntime, boundary: UploadBoundary, upload: Path) -> httpx.Response:
    route = f"/api/creator/workspaces/{runtime.workspace_id}/{boundary.route_suffix}"
    with upload.open("rb") as body, httpx.Client(base_url=runtime.origin, timeout=180) as client:
        return client.post(
            route,
            headers={"Origin": runtime.origin},
            files={"file": (boundary.filename, body, boundary.content_type)},
        )


@pytest.mark.parametrize("boundary", UPLOAD_BOUNDARIES, ids=lambda boundary: boundary.kind)
def test_real_api_accepts_valid_media_below_and_near_each_cap(
    tmp_path: Path,
    studio_proxy_runtime: StudioProxyRuntime,
    boundary: UploadBoundary,
) -> None:
    tiny = _sized_media(tmp_path, boundary, 1024)
    small = _sized_media(tmp_path, boundary, 2 * 1024 * 1024)
    near_cap = _sized_media(tmp_path, boundary, boundary.api_limit - 4096)

    tiny_response = _upload(studio_proxy_runtime, boundary, tiny)
    small_response = _upload(studio_proxy_runtime, boundary, small)
    near_cap_response = _upload(studio_proxy_runtime, boundary, near_cap)

    assert tiny_response.status_code == 201, tiny_response.text
    assert small_response.status_code == 201, small_response.text
    assert near_cap_response.status_code == 201, near_cap_response.text


@pytest.mark.parametrize("boundary", UPLOAD_BOUNDARIES, ids=lambda boundary: boundary.kind)
def test_real_api_rejects_file_over_each_api_cap(
    tmp_path: Path,
    studio_proxy_runtime: StudioProxyRuntime,
    boundary: UploadBoundary,
) -> None:
    upload = _sized_media(tmp_path, boundary, boundary.api_limit + 1)

    response = _upload(studio_proxy_runtime, boundary, upload)

    assert response.status_code == 400
    assert response.json()["error"]["category"] == "VALIDATION"


@pytest.mark.parametrize("boundary", UPLOAD_BOUNDARIES, ids=lambda boundary: boundary.kind)
def test_nginx_rejects_multipart_body_over_each_route_cap(
    tmp_path: Path,
    studio_proxy_runtime: StudioProxyRuntime,
    boundary: UploadBoundary,
) -> None:
    upload = _sized_media(tmp_path, boundary, boundary.nginx_limit + 1)

    response = _upload(studio_proxy_runtime, boundary, upload)

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("text/html")


def test_proxy_preserves_auth_workspace_and_origin_boundaries(
    tmp_path: Path,
    studio_proxy_runtime: StudioProxyRuntime,
) -> None:
    boundary = UPLOAD_BOUNDARIES[0]
    upload = _sized_media(tmp_path, boundary, 1024 * 1024)
    foreign_route = f"/api/creator/workspaces/{studio_proxy_runtime.foreign_workspace_id}/assets"
    with upload.open("rb") as body, httpx.Client(timeout=30) as client:
        unauthenticated = client.post(
            f"{studio_proxy_runtime.api_origin}/api/creator/workspaces/{studio_proxy_runtime.workspace_id}/assets",
            files={"file": (boundary.filename, body, boundary.content_type)},
        )
    with upload.open("rb") as body, httpx.Client(base_url=studio_proxy_runtime.origin, timeout=30) as client:
        foreign_workspace = client.post(
            foreign_route,
            headers={"Origin": studio_proxy_runtime.origin},
            files={"file": (boundary.filename, body, boundary.content_type)},
        )
    with upload.open("rb") as body, httpx.Client(base_url=studio_proxy_runtime.origin, timeout=30) as client:
        foreign_origin = client.post(
            f"/api/creator/workspaces/{studio_proxy_runtime.workspace_id}/assets",
            headers={"Origin": "https://foreign.invalid"},
            files={"file": (boundary.filename, body, boundary.content_type)},
        )

    assert unauthenticated.status_code == 401
    assert foreign_workspace.status_code == 404
    assert foreign_origin.status_code == 403


def test_general_json_over_one_mib_remains_rejected(studio_proxy_runtime: StudioProxyRuntime) -> None:
    with httpx.Client(base_url=studio_proxy_runtime.origin, timeout=30) as client:
        response = client.post(
            "/api/creator/projects",
            headers={"Origin": studio_proxy_runtime.origin, "Content-Type": "application/json"},
            content=b'"' + (b"x" * (1024 * 1024)) + b'"',
        )

    assert response.status_code == 413


def test_interrupted_upload_removes_nginx_and_api_staging_files(
    studio_proxy_runtime: StudioProxyRuntime,
) -> None:
    for target, headers in (
        (studio_proxy_runtime.origin, f"Origin: {studio_proxy_runtime.origin}\r\n"),
        (studio_proxy_runtime.api_origin, f"X-API-Key: {studio_proxy_runtime.token}\r\n"),
    ):
        host, port_text = target.removeprefix("http://").split(":")
        request = (
            f"POST /api/creator/workspaces/{studio_proxy_runtime.workspace_id}/assets/videos HTTP/1.1\r\n"
            f"Host: {host}:{port_text}\r\n{headers}"
            "Content-Type: multipart/form-data; boundary=interrupt\r\nContent-Length: 10485760\r\n\r\n"
            "--interrupt\r\nContent-Disposition: form-data; name=\"file\"; filename=\"cut.mp4\"\r\n\r\n"
        ).encode()
        with socket.create_connection((host, int(port_text)), timeout=5) as connection:
            connection.sendall(request + (b"x" * 1024 * 1024))

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(studio_proxy_runtime.body_temp.iterdir()):
        time.sleep(0.1)

    assert not any(studio_proxy_runtime.body_temp.iterdir())
    assert not list(studio_proxy_runtime.staging_parent.glob("media-upload-*"))
