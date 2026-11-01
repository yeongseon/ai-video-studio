"""Identity media routes served through the real nginx template, API and PostgreSQL (#824).

Raw ``/artifacts/...`` URLs fall through to the SPA fallback behind the shipped proxy, so the Studio
must receive media from authorized identity routes, in development and in production.
"""

from __future__ import annotations

import io
import struct
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import asyncpg
import httpx
import pytest
from PIL import Image

from studio_proxy_runtime_support import (
    StudioProxyRuntime,
    open_studio_proxy,
    prerequisites_or_skip,
)

MODES = ("development", "production")
SERVED = (
    "scene-image",
    "legacy-scene-image",
    "narration-audio",
    "captions-without-recorded-mime",
    "rendered-video",
    "legacy-absolute-path-video-artifact",
    "html-artifact-downloads-as-octet-stream",
    "workspace-image",
    "workspace-video",
    "project-image",
)
HIDDEN = (
    "absent-run",
    "absent-scene-asset",
    "scene-asset-of-another-run",
    "run-of-another-workspace",
    "absent-artifact",
    "artifact-of-another-run",
    "workspace-of-another-owner",
    "asset-of-another-workspace",
    "absent-workspace-asset",
    "asset-of-another-project",
    "absent-project-asset",
    "project-of-another-workspace",
    "scene-asset-symlinked-out-of-the-artifact-root",
    "scene-asset-with-traversal-key",
    "artifact-symlinked-out-of-the-artifact-root",
)
REJECTED = ("scene-asset-with-unsafe-image-type", "scene-asset-in-remote-storage")
ABSENT = 999_999
CREATOR = "/api/creator"


def _scene_url(run: int, asset: int) -> str:
    return f"{CREATOR}/runs/{run}/visual-assets/{asset}/content"


def _download_url(run: int, artifact: int) -> str:
    return f"{CREATOR}/runs/{run}/artifacts/{artifact}/download"


def _workspace_asset_url(workspace: int, asset: int) -> str:
    return f"{CREATOR}/workspaces/{workspace}/assets/{asset}/content"


def _project_asset_url(project: int, asset: int) -> str:
    return f"{CREATOR}/projects/{project}/assets/{asset}/content"


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _wav() -> bytes:
    samples = bytes(800)
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, 8_000, 16_000, 2, 16)
    size = struct.pack("<I", len(samples))
    return b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt " + fmt + b"data" + size + samples


def _mp4(directory: Path, color: str, seconds: float) -> bytes:
    target = directory / f"{color}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-nostdin", "-f", "lavfi", "-i", f"color=c={color}:size=16x16:duration={seconds}",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
        ],
        check=True, capture_output=True, timeout=30,
    )
    return target.read_bytes()


@dataclass(frozen=True, slots=True)
class Served:
    url: str
    data: bytes
    mime: str
    download: bool


@dataclass(frozen=True, slots=True)
class Seeded:
    runtime: StudioProxyRuntime
    served: dict[str, Served]
    hidden: dict[str, str]
    rejected: dict[str, tuple[str, int]]
    secret: bytes
    raw_paths: tuple[str, ...]


async def _insert_rows(runtime: StudioProxyRuntime) -> dict[str, int]:
    connection = await asyncpg.connect(runtime.database_url, server_settings={"search_path": runtime.schema})
    try:
        async def project(workspace_id: int) -> int:
            return int(await connection.fetchval(
                "INSERT INTO creator_projects (title, workspace_id) VALUES ('Authorized media', $1) RETURNING id",
                workspace_id,
            ))

        async def run(project_id: int, workspace_id: int) -> int:
            return int(await connection.fetchval(
                "INSERT INTO creator_runs (project_id, workspace_id, current_stage, status) "
                "VALUES ($1, $2, 'FINAL_REVIEW', 'running') RETURNING id",
                project_id, workspace_id,
            ))

        async def scene(
            run_id: int, scene_id: str, key: str, *, legacy: bool = False, provider: str = "local",
        ) -> int:
            return int(await connection.fetchval(
                "INSERT INTO creator_scene_assets (run_id, scene_id, asset_path, storage_key, storage_provider) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                run_id, scene_id, str(runtime.artifact_root / key),
                None if legacy else key, None if legacy else provider,
            ))

        async def artifact(run_id: int, kind: str, key: str, content_type: str | None) -> int:
            return int(await connection.fetchval(
                "INSERT INTO creator_artifacts (run_id, artifact_type, file_path, storage_key, content_type) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                run_id, kind, str(runtime.artifact_root / key), key, content_type,
            ))

        async def legacy_artifact(run_id: int, kind: str, key: str, mime_type: str) -> int:
            return int(await connection.fetchval(
                "INSERT INTO creator_artifacts (run_id, artifact_type, file_path, mime_type) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                run_id, kind, str(runtime.artifact_root / key), mime_type,
            ))

        ids: dict[str, int] = {}
        ids["project"] = await project(runtime.workspace_id)
        ids["other_project"] = await project(runtime.workspace_id)
        ids["foreign_project"] = await project(runtime.foreign_workspace_id)
        run_id = ids["run"] = await run(ids["project"], runtime.workspace_id)
        other_run = ids["other_run"] = await run(ids["project"], runtime.workspace_id)
        foreign_run = ids["foreign_run"] = await run(ids["foreign_project"], runtime.foreign_workspace_id)
        ids["scene"] = await scene(run_id, "scene-1", f"{run_id}/visual/scene-1.png")
        ids["legacy"] = await scene(run_id, "scene-2", f"{run_id}/visual/legacy.png", legacy=True)
        ids["other_scene"] = await scene(other_run, "scene-1", f"{other_run}/visual/other.png")
        ids["foreign_scene"] = await scene(foreign_run, "scene-1", f"{foreign_run}/visual/foreign.png")
        ids["symlinked_scene"] = await scene(run_id, "scene-3", f"{run_id}/visual/escape.png")
        ids["traversal_scene"] = await scene(run_id, "scene-4", "../outside-secret.png")
        ids["svg_scene"] = await scene(run_id, "scene-5", f"{run_id}/visual/unsafe.svg")
        ids["remote_scene"] = await scene(run_id, "scene-6", f"{run_id}/visual/remote.png", provider="s3")
        ids["audio"] = await artifact(run_id, "audio", f"{run_id}/audio/narration.wav", "audio/wav")
        ids["subtitle"] = await artifact(run_id, "subtitle", f"{run_id}/subtitles/captions.srt", None)
        ids["video"] = await artifact(run_id, "video", f"{run_id}/render/final.mp4", "video/mp4")
        ids["legacy_video"] = await legacy_artifact(run_id, "video", f"{run_id}/render/legacy.mp4", "video/mp4")
        ids["html"] = await artifact(run_id, "render_manifest", f"{run_id}/render/manifest.html", "text/html")
        ids["symlinked_artifact"] = await artifact(run_id, "render_manifest", f"{run_id}/render/escape.bin", None)
        ids["other_audio"] = await artifact(other_run, "audio", f"{other_run}/audio/narration.wav", "audio/wav")
        project_key = f"workspaces/{runtime.workspace_id}/assets/project-scene.png"
        ids["project_asset"] = int(await connection.fetchval(
            "INSERT INTO creator_media_assets "
            "(workspace_id, project_id, media_type, origin, storage_key, mime_type) "
            "VALUES ($1, $2, 'IMAGE', 'GENERATED', $3, 'image/png') RETURNING id",
            runtime.workspace_id, ids["project"], project_key,
        ))
        return ids
    finally:
        await connection.close()


def _store(runtime: StudioProxyRuntime, key: str, data: bytes) -> None:
    target = runtime.artifact_root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _upload(base: str, route: str, name: str, data: bytes, mime: str, headers: dict[str, str]) -> int:
    with httpx.Client(base_url=base, timeout=60) as client:
        response = client.post(route, headers=headers, files={"file": (name, data, mime)})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _seed(runtime: StudioProxyRuntime, scratch: Path) -> Seeded:
    scene_png, legacy_png = _png((10, 20, 30)), _png((40, 50, 60))
    other_png, foreign_png = _png((70, 80, 90)), _png((130, 140, 150))
    workspace_png, project_png = _png((100, 110, 120)), _png((160, 170, 180))
    secret, remote_png = _png((200, 10, 10)), _png((10, 200, 10))
    audio, captions = _wav(), b"1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    html, svg = b"<script>alert(1)</script>", b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    rendered, uploaded = _mp4(scratch, "red", 0.1), _mp4(scratch, "blue", 0.2)
    legacy_video = _mp4(scratch, "green", 0.3)
    blobs = [
        scene_png, legacy_png, other_png, foreign_png, workspace_png, project_png, secret, remote_png,
        audio, captions, html, svg, rendered, uploaded, legacy_video,
    ]
    assert len(set(blobs)) == len(blobs), "each seeded object needs distinct bytes to detect cross-serving"

    ids = anyio.run(_insert_rows, runtime)
    run, other_run, foreign_run = ids["run"], ids["other_run"], ids["foreign_run"]
    for key, data in {
        f"{run}/visual/scene-1.png": scene_png,
        f"{run}/visual/legacy.png": legacy_png,
        f"{other_run}/visual/other.png": other_png,
        f"{foreign_run}/visual/foreign.png": foreign_png,
        f"{run}/visual/unsafe.svg": svg,
        f"{run}/visual/remote.png": remote_png,
        f"{run}/audio/narration.wav": audio,
        f"{run}/subtitles/captions.srt": captions,
        f"{run}/render/final.mp4": rendered,
        f"{run}/render/legacy.mp4": legacy_video,
        f"{run}/render/manifest.html": html,
        f"{other_run}/audio/narration.wav": audio,
        f"workspaces/{runtime.workspace_id}/assets/project-scene.png": project_png,
    }.items():
        _store(runtime, key, data)
    outside = runtime.artifact_root.parent / "outside-secret.png"
    outside.write_bytes(secret)
    for key in (f"{run}/visual/escape.png", f"{run}/render/escape.bin"):
        (runtime.artifact_root / key).symlink_to(outside)

    csrf = {"Origin": runtime.origin}
    workspace = runtime.workspace_id
    uploads = f"{CREATOR}/workspaces/{workspace}/assets"
    image_id = _upload(runtime.origin, uploads, "workspace.png", workspace_png, "image/png", csrf)
    video_id = _upload(runtime.origin, f"{uploads}/videos", "workspace.mp4", uploaded, "video/mp4", csrf)
    foreign_id = _upload(
        runtime.api_origin, f"{CREATOR}/workspaces/{runtime.foreign_workspace_id}/assets",
        "foreign.png", foreign_png, "image/png", {"X-API-Key": runtime.foreign_token},
    )

    served = {
        "scene-image": Served(_scene_url(run, ids["scene"]), scene_png, "image/png", False),
        "legacy-scene-image": Served(_scene_url(run, ids["legacy"]), legacy_png, "image/png", False),
        "narration-audio": Served(_download_url(run, ids["audio"]), audio, "audio/wav", True),
        "captions-without-recorded-mime": Served(
            _download_url(run, ids["subtitle"]), captions, "application/x-subrip", True,
        ),
        "rendered-video": Served(_download_url(run, ids["video"]), rendered, "video/mp4", True),
        "legacy-absolute-path-video-artifact": Served(
            _download_url(run, ids["legacy_video"]), legacy_video, "video/mp4", True,
        ),
        "html-artifact-downloads-as-octet-stream": Served(
            _download_url(run, ids["html"]), html, "application/octet-stream", True,
        ),
        "workspace-image": Served(_workspace_asset_url(workspace, image_id), workspace_png, "image/png", False),
        "workspace-video": Served(_workspace_asset_url(workspace, video_id), uploaded, "video/mp4", False),
        "project-image": Served(
            _project_asset_url(ids["project"], ids["project_asset"]), project_png, "image/png", False,
        ),
    }
    hidden = {
        "absent-run": _scene_url(ABSENT, ids["scene"]),
        "absent-scene-asset": _scene_url(run, ABSENT),
        "scene-asset-of-another-run": _scene_url(run, ids["other_scene"]),
        "run-of-another-workspace": _scene_url(foreign_run, ids["foreign_scene"]),
        "absent-artifact": _download_url(run, ABSENT),
        "artifact-of-another-run": _download_url(run, ids["other_audio"]),
        "workspace-of-another-owner": _workspace_asset_url(runtime.foreign_workspace_id, foreign_id),
        "asset-of-another-workspace": _workspace_asset_url(workspace, foreign_id),
        "absent-workspace-asset": _workspace_asset_url(workspace, ABSENT),
        "asset-of-another-project": _project_asset_url(ids["other_project"], ids["project_asset"]),
        "absent-project-asset": _project_asset_url(ids["project"], ABSENT),
        "project-of-another-workspace": _project_asset_url(ids["foreign_project"], ids["project_asset"]),
        "scene-asset-symlinked-out-of-the-artifact-root": _scene_url(run, ids["symlinked_scene"]),
        "scene-asset-with-traversal-key": _scene_url(run, ids["traversal_scene"]),
        "artifact-symlinked-out-of-the-artifact-root": _download_url(run, ids["symlinked_artifact"]),
    }
    rejected = {
        "scene-asset-with-unsafe-image-type": (_scene_url(run, ids["svg_scene"]), 415),
        "scene-asset-in-remote-storage": (_scene_url(run, ids["remote_scene"]), 409),
    }
    assert set(served) == set(SERVED) and set(hidden) == set(HIDDEN) and set(rejected) == set(REJECTED)
    raw_paths = (f"{run}/visual/scene-1.png", f"{run}/audio/narration.wav", f"{run}/render/final.mp4")
    return Seeded(runtime, served, hidden, rejected, secret, raw_paths)


@pytest.fixture(scope="module", params=MODES)
def media(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seeded]:
    needs = prerequisites_or_skip(required_in_ci=True)
    root = tmp_path_factory.mktemp(f"authorized-media-{request.param}")
    with open_studio_proxy(root, needs, environment=request.param) as runtime:
        yield _seed(runtime, root)


def _get(base: str, url: str, token: str | None = None) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=30) as client:
        return client.get(url, headers={"X-API-Key": token} if token else {})


@pytest.mark.parametrize("name", SERVED)
def test_identity_routes_serve_exact_media_through_nginx(media: Seeded, name: str) -> None:
    # Given an object owned by the workspace behind the shipped nginx template.
    expected = media.served[name]

    # When the Studio requests its identity route.
    response = _get(media.runtime.origin, expected.url)

    # Then the bytes and type are the object's, never the SPA's HTML.
    assert response.status_code == 200, response.text[:200]
    assert response.headers["content-type"] == expected.mime
    assert response.content == expected.data
    assert set(response.headers.get_list("x-content-type-options")) == {"nosniff"}
    assert response.headers.get("content-disposition", "").startswith("attachment;") == expected.download


@pytest.mark.parametrize("name", SERVED)
def test_identity_routes_declare_their_own_safety_headers(media: Seeded, name: str) -> None:
    # Given the API itself, because nginx adds nosniff and would mask a missing route header.
    expected = media.served[name]

    # When an authorized caller requests the route directly.
    response = _get(media.runtime.api_origin, expected.url, media.runtime.token)

    # Then previews are private and unsniffable while downloads force an attachment.
    assert response.status_code == 200
    assert response.content == expected.data
    if expected.download:
        assert response.headers["content-disposition"].startswith("attachment;")
    else:
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert response.headers.get("cache-control") == "private, no-store"


@pytest.mark.parametrize("name", SERVED)
def test_identity_routes_reject_anonymous_and_foreign_callers(media: Seeded, name: str) -> None:
    # Given a real object and callers without access to it.
    expected = media.served[name]

    # When they reach the API without a key or with another workspace's key.
    anonymous = _get(media.runtime.api_origin, expected.url)
    foreign = _get(media.runtime.api_origin, expected.url, media.runtime.foreign_token)

    # Then identity is required, and the object's existence is not revealed.
    assert anonymous.status_code == 401
    assert foreign.status_code == 404
    assert expected.data not in foreign.content


@pytest.mark.parametrize("name", HIDDEN)
def test_absent_and_foreign_objects_return_404_through_nginx(media: Seeded, name: str) -> None:
    # Given an object that is absent, belongs to another owner, or points outside the artifact root.
    url = media.hidden[name]

    # When the workspace requests it through the proxy.
    response = _get(media.runtime.origin, url)

    # Then the route answers 404 itself, rather than FastAPI's unknown-route 404, with none of the seeded bytes.
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] != "Not Found"
    leaked = [served.data for served in media.served.values()] + [media.secret]
    assert not any(data in response.content for data in leaked)


@pytest.mark.parametrize("name", REJECTED)
def test_unsafe_or_remote_media_is_rejected_through_nginx(media: Seeded, name: str) -> None:
    # Given a scene asset with an unsafe image type, and one whose bytes live in remote storage.
    url, status = media.rejected[name]

    # When the workspace requests its preview through the proxy.
    response = _get(media.runtime.origin, url)

    # Then the route refuses it and never serves the local file that happens to share the key.
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    assert not response.headers["content-type"].startswith(("image/", "audio/", "video/"))


def test_proxy_does_not_serve_artifact_files_statically(media: Seeded) -> None:
    # Given artifact files on disk under the API's artifact root.
    present = [media.runtime.artifact_root / path for path in media.raw_paths]
    assert all(path.is_file() for path in present)

    # When the legacy raw URL shape is requested through nginx.
    responses = [_get(media.runtime.origin, f"/artifacts/{path}") for path in media.raw_paths]

    # Then no file bytes are served from the path, so raw URLs can never replace the identity routes.
    for path, response in zip(present, responses, strict=True):
        assert response.content != path.read_bytes()
        assert not response.headers.get("content-type", "").startswith(("image/", "audio/", "video/"))


@pytest.mark.parametrize("media", ["production"], indirect=True)
def test_production_disables_raw_artifact_path_routes(media: Seeded) -> None:
    # Given production mode and a caller that is authorized for the run.
    raw = media.raw_paths[0]

    # When it requests the raw path routes, through nginx and at the API.
    through_nginx = _get(media.runtime.origin, f"/api/artifacts/files/{raw}")
    api_files = _get(media.runtime.api_origin, f"/api/artifacts/files/{raw}", media.runtime.token)
    api_legacy = _get(media.runtime.api_origin, f"/artifacts/{raw}", media.runtime.token)

    # Then production never serves raw paths as a shortcut.
    assert (through_nginx.status_code, api_files.status_code, api_legacy.status_code) == (404, 404, 404)
