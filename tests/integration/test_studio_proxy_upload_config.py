from pathlib import Path
import re


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
    # Given all location bodies, including nested CSRF if blocks.
    configured = TEMPLATE.read_text()
    locations = re.findall(r"location ([^\n]+) \{(.*?)\n    \}", configured, re.DOTALL)
    # When selecting every location forwarding to the API.
    proxied = [(name, body) for name, body in locations if "proxy_pass http://api:8000;" in body]
    # Then no upstream location can accidentally inherit client admin credentials.
    assert len(proxied) == configured.count("proxy_pass http://api:8000;") > 0
    for name, body in proxied:
        assert re.search(r'proxy_set_header\s+X-Admin-Key\s+""\s*;', body), name


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
