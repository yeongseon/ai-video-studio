import re

import pytest
from creator_service.error_redaction import redact_log_text
from fastapi import FastAPI
from fastapi.routing import APIRoute
from shorts_api.main import app
from starlette.convertors import IntegerConvertor, StringConvertor
from starlette.routing import Mount, Route


def _assert_log_route_contract(application: FastAPI) -> None:
    pending = list(application.routes)
    while pending:
        route = pending.pop()
        if isinstance(route, Mount):
            pending.extend(route.routes)
            continue
        assert isinstance(route, Route), f"Unreviewed route type: {route}"
        path = route.path
        if re.match(r"/(?:api/(?:creator|admin)|release)(?:/|$)", path):
            for name, converter in route.param_convertors.items():
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
