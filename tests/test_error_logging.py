import logging
import subprocess
import sys

import anyio
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.error_logging import log_failure
from app.errors import QueryValidationError


def test_deployed_errors_go_to_stdout_even_at_critical_level():
    code = "from app.error_logging import configure_logging,log_failure; import logging; configure_logging('CRITICAL'); log_failure(logging.getLogger('app.test'), 'test', RuntimeError('private SQL password'), code='TEST_FAILURE')"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert "ERROR app.test operation=test code=TEST_FAILURE" in result.stdout
    assert "private" not in result.stdout
    assert result.stderr == ""


def test_stdio_keeps_protocol_stdout_clean():
    code = "from app.error_logging import configure_logging,log_failure; import logging; configure_logging('INFO', stdio=True); log_failure(logging.getLogger('app.test'), 'test')"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout == ""
    assert "ERROR" in result.stderr


def test_log_does_not_include_exception_text_or_bad_code(caplog):
    with caplog.at_level(logging.ERROR):
        log_failure(
            logging.getLogger("app.test"),
            "test",
            QueryValidationError("private row", "private\nSQL"),
        )
    assert "code=INTERNAL_ERROR" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("known", [True, False])
def test_mcp_tool_errors_logged_even_when_handled(caplog, known):
    from app.mcp_server import _domain_to_tool_error

    exc = (
        QueryValidationError("private SQL", "TRINO_RESULT_LIMIT")
        if known
        else RuntimeError("private SQL")
    )
    with caplog.at_level(logging.ERROR):
        _domain_to_tool_error(exc)
    assert "operation=mcp.tool" in caplog.text
    assert "private SQL" not in caplog.text


def test_rest_converted_http_error_logged_preserving_response(caplog):
    from app.main import logged_http_error

    req = Request({"type": "http", "method": "GET", "path": "/query", "headers": []})
    exc = HTTPException(
        401,
        {"code": "AUTH_REQUIRED", "error": "private token"},
        headers={"WWW-Authenticate": "Bearer"},
    )
    with caplog.at_level(logging.ERROR):
        response = anyio.run(logged_http_error, req, exc)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "operation=rest.http code=AUTH_REQUIRED status=401" in caplog.text
    assert "private token" not in caplog.text


def test_rest_domain_validation_logged(caplog):
    from app.main import domain_error_handler

    req = Request({"type": "http", "method": "POST", "path": "/query", "headers": []})
    with caplog.at_level(logging.ERROR):
        response = anyio.run(
            domain_error_handler,
            req,
            QueryValidationError("private SQL", "TRINO_UNSUPPORTED_SQL"),
        )
    assert response.status_code == 400
    assert "operation=rest.domain code=TRINO_UNSUPPORTED_SQL" in caplog.text
    assert "private SQL" not in caplog.text


def test_rest_request_validation_is_loud(caplog):
    from fastapi.exceptions import RequestValidationError

    from app.main import logged_validation_error

    req = Request({"type": "http", "method": "POST", "path": "/query", "headers": []})
    with caplog.at_level(logging.ERROR):
        response = anyio.run(logged_validation_error, req, RequestValidationError([]))
    assert response.status_code == 422
    assert "code=REQUEST_VALIDATION_ERROR status=422" in caplog.text


def test_mcp_auth_response_is_loud(caplog):
    from app.mcp_server import _send_json_response

    messages = []

    async def send(message):
        messages.append(message)

    with caplog.at_level(logging.ERROR):
        anyio.run(
            _send_json_response,
            send,
            403,
            {"code": "ACCESS_DENIED", "error": "private token"},
        )
    assert messages[0]["status"] == 403
    assert "operation=mcp.http code=ACCESS_DENIED status=403" in caplog.text
    assert "private token" not in caplog.text


def test_framework_traceback_does_not_echo_cause_or_sql():
    import textwrap

    script = textwrap.dedent("""
        import logging
        from app.error_logging import configure_logging
        configure_logging('INFO')
        try:
            try:
                raise ValueError('secret-native-sql password=private')
            except ValueError as cause:
                raise RuntimeError('private-row') from cause
        except RuntimeError:
            logging.getLogger('uvicorn.error').exception('ASGI request failed')
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert "failure_class=RuntimeError" in result.stdout
    assert "secret-native-sql" not in result.stdout
    assert "private" not in result.stdout
    assert result.stderr == ""
