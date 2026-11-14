import re

import pytest
from creator_service.error_redaction import (
    LOG_ROUTE_PREFIX,
    is_safe_log_route_segment,
    redact_log_text,
)
from fastapi import FastAPI
from fastapi.routing import APIRoute
from shorts_api.main import app
from starlette.convertors import IntegerConvertor, StringConvertor
from starlette.routing import Mount, Route, compile_path
from starlette.staticfiles import StaticFiles

PRESERVED_SAMPLES = ("/api/creator/x", "/api/admin/x", "/release/identity")


def _assert_log_route_contract(application: FastAPI) -> None:
    pending = [("", route, {}) for route in application.routes]
    while pending:
        prefix, route, inherited_convertors = pending.pop()
        path = prefix + route.path
        convertors = inherited_convertors | compile_path(route.path)[2]
        if isinstance(route, Mount):
            if re.match(LOG_ROUTE_PREFIX, path):
                assert route.routes, f"Uninspectable preserved mount: {path}"
                for name, converter in convertors.items():
                    assert re.fullmatch(r"[a-z][a-z0-9_]*_id", name), path
                    assert not re.search(r"token|signature|key|secret|credential|code", name), path
                    assert type(converter) in (IntegerConvertor, StringConvertor), path
            elif not route.routes:
                base = path.rstrip("/")
                assert not any(sample.startswith(base + "/") for sample in PRESERVED_SAMPLES), (
                    f"Route-less mount shadows a preserved prefix: {path or '/'}"
                )
            pending.extend((path, child, convertors) for child in route.routes)
            continue
        assert isinstance(route, Route), f"Unreviewed route type: {route}"
        if path.count("/") == 1:
            integer_names = set()
            if isinstance(route, APIRoute):
                dependencies = [route.dependant]
                while dependencies:
                    dependency = dependencies.pop()
                    integer_names.update(
                        field.name for field in dependency.path_params
                        if field.field_info.annotation is int
                    )
                    dependencies.extend(dependency.dependencies)
            for name, converter in convertors.items():
                assert (
                    type(converter) is IntegerConvertor or name in integer_names
                    or re.fullmatch(r"[a-z][a-z0-9_]*_id", name)
                ), path
                assert not re.search(r"token|signature|key|secret|credential|code", name), path
                assert type(converter) in (IntegerConvertor, StringConvertor), path
        if re.match(LOG_ROUTE_PREFIX, path):
            for name, converter in convertors.items():
                assert re.fullmatch(r"[a-z][a-z0-9_]*_id", name), path
                assert not re.search(r"token|signature|key|secret|credential|code", name), path
                assert type(converter) in (IntegerConvertor, StringConvertor), path
            if isinstance(route, APIRoute):
                dependencies = [route.dependant]
                while dependencies:
                    dependency = dependencies.pop()
                    for field in dependency.path_params:
                        assert field.field_info.annotation in (int, str), path
                    dependencies.extend(dependency.dependencies)
            sample = re.sub(r"\{[^}]+\}", "42", path)
            assert redact_log_text(sample) == sample, path
        else:
            assert path.count("/") == 1 or path in {
                "/docs/oauth2-redirect",
                "/artifacts/{artifact_path:path}",
                "/api/artifacts/files/{path:path}",
            }, f"Classify new multi-segment route for logging: {path}"
            if path != "/docs/oauth2-redirect" and path.count("/") > 1:
                assert redact_log_text(path) == "<redacted>", path


def test_log_route_contract_when_walking_real_api() -> None:
    # Given the same composed app used by the API client fixture.
    application = app
    # When / Then every route must satisfy the reviewed log-preservation boundary.
    _assert_log_route_contract(application)


@pytest.mark.parametrize("prefix", ["/api/creator", "/api/admin", "/release"])
@pytest.mark.parametrize("suffix", [
    "/{asset_id:path}", "/{token}", "/{signature_id}", "/{key_id}",
])
def test_log_route_contract_rejects_drift_when_sensitive_route_is_added(
    prefix: str, suffix: str,
) -> None:
    # Given an isolated app with a newly introduced unsafe path parameter.
    application = FastAPI()
    application.add_api_route(prefix + suffix, lambda: None)
    # When / Then the inventory guard must reject the new route.
    with pytest.raises(AssertionError):
        _assert_log_route_contract(application)


@pytest.mark.parametrize("nested", [False, True])
def test_log_route_contract_rejects_token_when_child_is_mounted(nested: bool) -> None:
    # Given a token parameter hidden behind one or two accumulated mount prefixes.
    child = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    child.add_api_route("/{share_token}", lambda: None)
    application = FastAPI()
    if nested:
        parent = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        parent.mount("/shared", child)
        application.mount("/api/creator", parent)
    else:
        application.mount("/api/creator/shared", child)
    # When / Then the guard must reject the effective route, not the relative child.
    with pytest.raises(AssertionError, match="share_token"):
        _assert_log_route_contract(application)


def test_log_route_contract_rejects_files_when_mount_has_no_routes() -> None:
    # Given a dynamic file-serving mount with no inspectable children.
    application = FastAPI()
    application.mount("/api/creator/files", StaticFiles())
    # When / Then an unbounded private suffix must force a conscious route review.
    with pytest.raises(AssertionError, match="/api/creator/files"):
        _assert_log_route_contract(application)


@pytest.mark.parametrize("mount_path", ["/", "/api"])
def test_log_route_contract_rejects_an_ancestor_mount_without_routes(mount_path: str) -> None:
    # Given a route-less file-serving app mounted above a preserved prefix.
    application = FastAPI()
    application.mount(mount_path, StaticFiles())
    # When / Then it could serve preserved-looking private paths, so the guard must reject it.
    with pytest.raises(AssertionError, match="shadows a preserved prefix"):
        _assert_log_route_contract(application)


def test_log_route_contract_accepts_a_route_less_mount_that_cannot_shadow_a_prefix() -> None:
    # Given a route-less mount that is unrelated to every preserved prefix.
    application = FastAPI()
    application.mount("/static", StaticFiles())
    # When / Then the guard has nothing to reject.
    _assert_log_route_contract(application)


def test_log_route_contract_accepts_ids_when_mounts_are_nested() -> None:
    # Given a clean nested sub-application with a numeric resource identifier.
    child = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    def endpoint(run_id: int) -> None:
        return None

    child.add_api_route("/{run_id}", endpoint)
    parent = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    parent.mount("/runs", child)
    application = FastAPI()
    application.mount("/api/creator", parent)
    # When / Then accumulated safe prefixes and child parameters satisfy the contract.
    _assert_log_route_contract(application)


def test_static_segments_satisfy_rule_when_walking_real_api() -> None:
    # Given the complete composed live route tree, including static framework routes.
    pending = [("", route) for route in app.routes]
    # When / Then every literal segment satisfies the production segment heuristic.
    while pending:
        prefix, route = pending.pop()
        path = prefix + route.path
        for segment in route.path.strip("/").split("/"):
            if segment and not segment.startswith("{"):
                assert is_safe_log_route_segment(segment), f"{segment}: {path}"
        if isinstance(route, Mount):
            pending.extend((path, child) for child in route.routes)


def test_static_drift_is_rejected_when_new_route_word_is_opaque() -> None:
    # Given a future static route that violates the lowercase, digit-free rule.
    application = FastAPI()
    application.add_api_route("/api/creator/version2", lambda: None)
    # When / Then the existing round-trip assertion catches static route drift.
    with pytest.raises(AssertionError, match="version2"):
        _assert_log_route_contract(application)


@pytest.mark.parametrize("path", ["/{share_token}", "/{asset_id:path}", "/{signature_id}"])
def test_log_route_contract_rejects_sensitive_parameter_when_route_is_single_segment(
    path: str,
) -> None:
    # Given an otherwise unclassified root route with an unsafe parameter.
    application = FastAPI()
    application.add_api_route(path, lambda: None)
    # When / Then root-level paths must not bypass parameter classification.
    with pytest.raises(AssertionError, match=path):
        _assert_log_route_contract(application)


@pytest.mark.parametrize("path", ["/{resource_id}", "/{number}", "/{number:int}"])
def test_log_route_contract_accepts_parameter_when_single_segment_is_id_or_integer(
    path: str,
) -> None:
    # Given safe resource names or an explicitly integer-typed parameter.
    application = FastAPI()

    def endpoint(number: int = 0, resource_id: str = "") -> None:
        return None

    application.add_api_route(path, endpoint)
    # When / Then a safe single-segment parameter remains supported.
    _assert_log_route_contract(application)
