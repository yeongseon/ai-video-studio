from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "apps/studio-web/nginx.conf.template"


def _location(pattern: str) -> str:
    configured = TEMPLATE.read_text()
    match = re.search(rf"location {pattern} \{{(?P<body>.*?)\n    \}}", configured, re.DOTALL)
    assert match is not None, f"missing nginx location: {pattern}"
    return match.group("body")


def test_upload_locations_allow_api_limit_plus_multipart_margin() -> None:
    assert "client_max_body_size 26m;" in _location(
        r'~ \^/api/creator/workspaces/\[0-9\]\+/assets\$'
    )
    assert "client_max_body_size 101m;" in _location(
        r'~ \^/api/creator/workspaces/\[0-9\]\+/assets/audio\$'
    )
    assert "client_max_body_size 501m;" in _location(
        r'~ \^/api/creator/workspaces/\[0-9\]\+/assets/videos\$'
    )


def test_general_api_location_keeps_small_json_limit() -> None:
    assert "client_max_body_size 1m;" in _location(r"/api/")


def test_every_api_proxy_location_strips_client_admin_key() -> None:
    configured = TEMPLATE.read_text()
    assert configured.count("proxy_pass") > 0
    assert _proxy_locations_missing_admin_strip(configured) == []


def _proxy_locations_missing_admin_strip(configured: str) -> list[str]:
    # Given all location bodies, including nested CSRF if blocks.
    locations = re.findall(r"location ([^\n]+) \{(.*?)\n    \}", configured, re.DOTALL)
    # When selecting every location forwarding to the API.
    proxied = [(name, body) for name, body in locations if re.search(r"\bproxy_pass\s", body)]
    assert len(proxied) == len(re.findall(r"\bproxy_pass\s", configured))
    # Then no upstream location can accidentally inherit client admin credentials.
    return [name for name, body in proxied if not re.search(
        r'^        proxy_set_header\s+X-Admin-Key\s+""\s*;\s*$', body, re.MULTILINE,
    )]


@pytest.mark.parametrize("upstream", ["http://api:8000", "http://api:8000/x", "$api_upstream"])
@pytest.mark.parametrize("strip", ["", '        if ($http_origin) {\n            proxy_set_header X-Admin-Key "";\n        }'])
def test_proxy_parser_reports_missing_location_level_strip(upstream: str, strip: str) -> None:
    # Given an alternate upstream with no strip or only a conditional strip.
    configured = f"    location /alternate/ {{\n{strip}\n        proxy_pass {upstream};\n    }}"
    # When / Then every proxied location needs an unconditional strip.
    assert _proxy_locations_missing_admin_strip(configured) == ["/alternate/"]


def test_upload_locations_preserve_general_api_proxy_and_csrf_policy() -> None:
    general = _location(r"/api/")
    policy_lines = {
        line.strip()
        for line in general.splitlines()
        if line.strip() and not line.strip().startswith(("#", "client_max_body_size"))
    }
    for pattern in (
        r'~ \^/api/creator/workspaces/\[0-9\]\+/assets\$',
        r'~ \^/api/creator/workspaces/\[0-9\]\+/assets/audio\$',
        r'~ \^/api/creator/workspaces/\[0-9\]\+/assets/videos\$',
    ):
        upload_lines = {line.strip() for line in _location(pattern).splitlines()}
        assert policy_lines <= upload_lines
