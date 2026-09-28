"""MCP presentation removes NUL padding without changing service results."""

import copy
import json
from decimal import Decimal
from unittest.mock import patch

import anyio
import pytest

from app.mcp_server import _clean_query_result, mcp


@pytest.mark.parametrize(
    "tool, service_name, arguments",
    [
        ("runQuery", "svc_run_query", {"sql": "SELECT 1"}),
        ("sampleRows", "svc_sample_rows", {"database": "default", "table": "t"}),
    ],
)
def test_padding_removed_from_both_mcp_representations(tool, service_name, arguments):
    payload = {
        "columns": ["earn_description", "nested"],
        "rows": [["Planned PDO" + "\x00" * 19, {"key\x00": ["café 東京 😀\x00", "a\x00b\x00"]}]],
        "row_count": 1,
        "truncated": False,
        "provenance": {"version": 1, "columns": [["default.t", "earn_description"]]},
    }
    original = copy.deepcopy(payload)
    expected = {
        **payload,
        "rows": [["Planned PDO", {"key\x00": ["café 東京 😀", "a\x00b"]}]],
    }

    async def call():
        with patch(f"app.mcp_server.{service_name}", return_value=payload):
            return await mcp.call_tool(tool, arguments)

    content, structured = anyio.run(call)
    assert structured == expected
    assert json.loads(content[0].text) == expected
    assert payload == original


def test_cleanup_preserves_other_values_and_metadata():
    cells = [None, 42, Decimal("8.00"), False, b"binary\x00", " a \t\n", r"\u0000", "\x00leading", "a\x00b"]
    payload = {
        "columns": ["alias\x00"],
        "rows": [[cells, ("padded\x00", "\x00\x00", "")]],
        "row_count": 1,
        "truncated": True,
    }
    result = _clean_query_result(payload)
    assert result == {**payload, "rows": [[cells, ("padded", "", "")]]}


def test_empty_result():
    payload = {"columns": [], "rows": [], "row_count": 0, "truncated": False}
    assert _clean_query_result(payload) == payload
