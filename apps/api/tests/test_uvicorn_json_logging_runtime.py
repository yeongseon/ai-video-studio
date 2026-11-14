"""Real server coverage of the production API logging wiring, without external services."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from threading import Event
from time import monotonic

import httpx
import pytest


@pytest.fixture(scope="module", params=[True, False])
def access_log_enabled(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.fixture(scope="module")
def server_output(tmp_path_factory: pytest.TempPathFactory, access_log_enabled: bool) -> str:
    """Use production composition and lifespan; only the failing route is test-only."""
    return run_server(tmp_path_factory, access_log_enabled)


def run_server(
    tmp_path_factory: pytest.TempPathFactory, access_log_enabled: bool, otel_enabled: bool = False,
) -> str:
    directory = tmp_path_factory.mktemp("uvicorn-json")
    (directory / "logging_probe.py").write_text(dedent('''\
        from shorts_api.app_factory import create_app

        app = create_app()

        @app.get("/logging-probe")
        async def fail() -> None:
            cause = ValueError("token=sk-EXAMPLE1234567890abcdef https://user:pass@example.invalid/x")
            raise RuntimeError("outer") from cause
    '''))
    environment = {
        **{key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "VIRTUAL_ENV", "LANG", "LC_ALL"}},
        "PYTHONPATH": os.pathsep.join([
            str(directory),
            str(Path(__file__).resolve().parents[3] / "apps/api/src"),
            str(Path(__file__).resolve().parents[3] / "packages/creator-service"),
            str(Path(__file__).resolve().parents[3] / "packages/creator-domain"),
            str(Path(__file__).resolve().parents[3] / "packages/creator-provider"),
            str(Path(__file__).resolve().parents[3] / "apps/worker-orchestrator"),
        ]),
        "ENVIRONMENT": "development", "REDIS_URL": "", "OTEL_ENABLED": str(otel_enabled).lower(),
        "ENABLE_PROCESS_RESOURCE_GUARD": "false", "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Given a reserved loopback socket, inherited rather than closed/rebound.
    with socket.socket() as sock, (directory / "output").open("w+") as output:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        with subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "logging_probe:app", "--fd", str(sock.fileno()),
             "--host", "127.0.0.1", "--log-level", "info",
             *([] if access_log_enabled else ["--no-access-log"])],
            cwd=directory, env=environment, pass_fds=(sock.fileno(),),
            stdout=output, stderr=subprocess.STDOUT,
        ) as process:
            try:
                # Bound readiness by an HTTP response, not an arbitrary startup sleep.
                deadline = monotonic() + 30
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2,
                                  trust_env=False) as client:
                    while True:
                        try:
                            response = client.get("/openapi.json")
                            assert response.status_code == 200
                            assert client.get("/openapi.json").status_code == 200
                            break
                        except httpx.TransportError:
                            if process.poll() is not None or monotonic() >= deadline:
                                output.seek(0)
                                pytest.fail(f"server failed readiness (exit={process.poll()}):\n"
                                            f"{output.read()}")
                            Event().wait(0.02)
                    # When a real HTTP exception is re-raised to Uvicorn, then another
                    # request supplies both credential-shaped and ordinary query data.
                    assert client.get("/logging-probe", timeout=2).status_code == 500
                    assert client.get(
                        "/healthz?token=sk-EXAMPLEQUERY1234567890abcd&api_key=sk-EXAMPLEQ2"
                        "&ordinary=private-query-value", timeout=2,
                    ).status_code == 200
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        output.seek(0)
        return output.read()


def test_no_raw_credential_when_uvicorn_logs_chained_exception(server_output: str) -> None:
    # Given / When the real server handled the failing request (module fixture).
    # Then every sink is free of both token and credential-URL fragments.
    assert "sk-EXAMPLE" not in server_output
    assert "pass@" not in server_output


def test_every_server_line_is_json_when_default_uvicorn_logging_precedes_app_import(
    server_output: str,
) -> None:
    # Given / When default Uvicorn startup, HTTP requests and shutdown completed.
    # Then there is no plain-text server handler, and both exception boundaries remain useful.
    records = [json.loads(line) for line in server_output.splitlines() if line.strip()]
    assert records
    errors = [record for record in records if record["logger"] == "uvicorn.error"
              and "exc_info" in record]
    assert len(errors) == 1
    trace = errors[0]["exc_info"]
    assert "RuntimeError: outer" in trace and "ValueError: token=<redacted>" in trace
    assert "direct cause" in trace and "<redacted>" in trace
    assert any(record["logger"] == "shorts_api.app_factory" and "exc_info" in record
               for record in records)
    assert all(record["service"] == "api" for record in records)


def test_uvicorn_access_omits_entire_query_when_request_has_credentials(
    server_output: str, access_log_enabled: bool,
) -> None:
    # Given / When the secret-bearing query was requested.
    # Then access logging stays enabled but even ordinary query values are absent.
    assert "?token=" not in server_output
    assert "private-query-value" not in server_output
    records = [json.loads(line) for line in server_output.splitlines() if line.strip()]
    access = [record["message"] for record in records if record["logger"] == "uvicorn.access"]
    if not access_log_enabled:
        assert access == []
        return
    assert all("?" not in message for message in access)
    assert sum('"GET /healthz HTTP/1.1" 200' in message for message in access) == 1
    assert sum('"GET /logging-probe HTTP/1.1" 500' in message for message in access) == 1


def test_otel_exporters_redact_when_real_server_handles_chained_exception(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # Given / When one extra production server run with console telemetry enabled.
    output = run_server(tmp_path_factory, True, True)
    decoder = json.JSONDecoder()
    records = []
    remaining = output.lstrip()
    while remaining:
        record, end = decoder.raw_decode(remaining)
        records.append(record)
        remaining = remaining[end:].lstrip()
    # Then both exporters really ran, including both application and Uvicorn log records.
    spans = [record for record in records if record.get("name") == "GET /logging-probe"]
    assert len(spans) == 1
    assert any(event["name"] == "exception" for event in spans[0]["events"])
    logs = [record for record in records if "body" in record]
    assert any("Unhandled exception" in record["body"] for record in logs)
    assert any("Exception in ASGI application" in record["body"] for record in logs)
    assert "sk-EXAMPLE" not in output
    assert "user:pass@" not in output
    assert "RuntimeError" in output and "ValueError" in output and "<redacted>" in output
    stream = [record for record in records if "logger" in record]
    assert stream
    assert all({"timestamp", "level", "service", "logger", "message"} <= record.keys()
               and record["service"] == "api" for record in stream)
    errors = [record for record in stream if record["logger"] == "uvicorn.error"
              and "exc_info" in record]
    assert len(errors) == 1
    assert "RuntimeError: outer" in errors[0]["exc_info"]
    assert "ValueError: token=<redacted>" in errors[0]["exc_info"]
