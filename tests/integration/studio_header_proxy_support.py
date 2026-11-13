from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import subprocess
import threading

import httpx
import pytest

from studio_proxy_runtime_support import ROOT, _await_health, _free_port, _unavailable


@pytest.fixture(scope="module")
def header_proxy(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str]]:
    nginx = shutil.which("nginx")
    if nginx is None:
        _unavailable("nginx is required for the admin header runtime proof", required_in_ci=True)
    root = tmp_path_factory.mktemp("studio-header-proxy")
    token = "synthetic-proxy-creator-key-1036"

    class HeaderEcho(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = json.dumps(dict(self.headers.items())).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET

        def log_message(self, format: str, *args: str) -> None:
            return

    with ThreadingHTTPServer(("127.0.0.1", 0), HeaderEcho) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{_free_port()}"
        template = (ROOT / "apps/studio-web/nginx.conf.template").read_text()
        if getattr(request, "param", False):
            before, general = template.split("    location /api/ {", 1)
            template = before + "    location /api/ {" + general.replace(
                '        proxy_set_header X-Admin-Key "";\n', "", 1,
            )
        configured = template.replace("${API_KEY}", token).replace(
            "listen 8080;", f"listen {origin.removeprefix('http://')};",
        ).replace("http://api:8000", f"http://127.0.0.1:{upstream.server_port}")
        config = root / "nginx.conf"
        config.write_text(
            f"pid {root}/nginx.pid; error_log {root}/error.log;\n"
            f"events {{}}\nhttp {{ access_log off; client_body_temp_path {root}/body; "
            f"proxy_temp_path {root}/proxy;\n{configured}\n}}\n",
        )
        try:
            with subprocess.Popen(
                [nginx, "-p", str(root), "-c", str(config), "-g", "daemon off;"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ) as process:
                try:
                    with httpx.Client(base_url=origin, timeout=5) as client:
                        _await_health(client, process)
                    yield origin, token
                finally:
                    process.terminate()
                    process.wait(timeout=15)
        finally:
            upstream.shutdown()
            thread.join(timeout=5)
