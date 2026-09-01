"""Rewrite raw ClickHouse error text into a short, actionable message for an LLM.

WHY: clickhouse-connect surfaces server errors as one long string such as::

    Received ClickHouse exception, code: 215, server response: Code: 215.
    DB::Exception: Column t.hire_date is not under aggregate function and not in
    GROUP BY keys. In query SELECT <the entire query, re-printed> ...
    (NOT_AN_AGGREGATE) (version 24.8.14.39 (official build)) (for url http://...)

Forwarded verbatim, that costs the calling model hundreds of tokens re-reading
its own SQL, buries the one sentence that matters, and carries no guidance on
*how* to fix it.  This module:

  1. keeps the ``DB::Exception`` sentence and the symbolic error name,
  2. drops the echoed query (``In query ...`` / ``in scope ...``), the server
     version, and the URL suffix — the model already has its query, and the URL
     is redacted downstream anyway,
  3. appends a one-line, code-specific hint that names the concrete rewrite.

Unknown error codes fall through with only the noise removed, so nothing is lost
for the long tail.  Redaction of host/port/user still runs on the result in
``execute_query`` — this module never adds connection details.
"""

from __future__ import annotations

import re

# "Code: 215. DB::Exception: <body>"  — body runs to end of string.
_BODY_RE = re.compile(r"Code:\s*(\d+)\.\s*DB::Exception:\s*(.*)", re.S)
# Trailing driver/server decorations, in the order clickhouse-connect emits them.
_TAIL_VERSION_RE = re.compile(r"\s*\(version [^)]*\([^)]*\)\)\s*$|\s*\(version [^)]*\)\s*$")
_TAIL_URL_RE = re.compile(r"\s*\(for url [^)]*\)\s*$")
# Symbolic name, e.g. "(NOT_AN_AGGREGATE)", once the tail has been trimmed.
_NAME_RE = re.compile(r"\s*\(([A-Z][A-Z0-9_]+)\)\s*\.?\s*$")
# The analyzer re-prints the whole statement after these markers.
_IN_QUERY_RE = re.compile(r"\s*\bIn query\b.*$", re.S)
_IN_SCOPE_RE = re.compile(r"\s+in scope\s+.*?(?=\.\s+Maybe you meant|\.?\s*$)", re.S | re.I)
# clickhouse-connect appends "\n FORMAT Native" to every query; the parser may echo it.
_FORMAT_NATIVE_RE = re.compile(r"\s*FORMAT Native\b")
# The driver appends "\n FORMAT Native"; a syntax error located there means the
# user's statement ended before the parser was satisfied.
_FAILED_AT_NATIVE_RE = re.compile(
    r"failed at position \d+ \('Native'\) \(line 2, col \d+\): Native\.?"
)
_EXPECTED_RE = re.compile(r"\s*Expected one of:.*$", re.S)
_NOT_AGG_COL_RE = re.compile(r"Column (\S+) is not under aggregate function")

_MAX_BODY = 500

# Symbolic error name -> concrete rewrite advice.  Kept to one sentence each; the
# goal is a first-retry fix, not a tutorial.  Names come from
# src/Common/ErrorCodes.cpp in the ClickHouse repo.
_HINTS: dict[str, str] = {
    "NOT_AN_AGGREGATE": (
        "Every non-aggregated SELECT expression must be built from a GROUP BY key: "
        "either GROUP BY the SELECT alias, or apply exactly the same expression in "
        "SELECT as in GROUP BY (e.g. formatDateTime(toStartOfMonth(col), ...) with "
        "GROUP BY toStartOfMonth(col))."
    ),
    "UNKNOWN_IDENTIFIER": (
        "The column or alias does not exist on that table; use the 'Maybe you meant' "
        "suggestion if given, otherwise call getTableSchema for the exact column names."
    ),
    "UNKNOWN_TABLE": "Call listTables for the exact table name; always qualify it as database.table.",
    "UNKNOWN_DATABASE": "Call listDatabases for the databases you can query.",
    "UNKNOWN_FUNCTION": (
        "That function does not exist in ClickHouse; use the 'Maybe you meant' "
        "suggestion if given, otherwise the ClickHouse name (e.g. formatDateTime, "
        "toStartOfMonth, dateDiff, countIf, sumIf)."
    ),
    "SYNTAX_ERROR": (
        "Fix the SQL at the reported position; check for a trailing comma or operator, "
        "unbalanced parentheses/quotes, and ClickHouse-specific syntax."
    ),
    "AMBIGUOUS_IDENTIFIER": "Qualify the column with its table alias (t.col).",
    "AMBIGUOUS_COLUMN_NAME": "Qualify the column with its table alias (t.col).",
    "ILLEGAL_TYPE_OF_ARGUMENT": (
        "An argument has the wrong type; check types with getTableSchema and cast "
        "explicitly (toDate, toDateTime64, toString, toInt64)."
    ),
    "NO_COMMON_TYPE": (
        "The compared or combined values have incompatible types; cast both sides "
        "explicitly (e.g. compare a DateTime64 column to toDateTime64('...', 6))."
    ),
    "TYPE_MISMATCH": "Cast the mismatched side explicitly so both types agree.",
    "CANNOT_PARSE_TEXT": (
        "A string literal could not be parsed as the target type; use the exact "
        "format 'YYYY-MM-DD' or 'YYYY-MM-DD hh:mm:ss' for dates."
    ),
    "CANNOT_PARSE_DATETIME": (
        "Use 'YYYY-MM-DD hh:mm:ss' (or toDate('YYYY-MM-DD')) for date/time literals."
    ),
    "NUMBER_OF_ARGUMENTS_DOESNT_MATCH": "Check the function's arity in ClickHouse and adjust the arguments.",
    "BAD_ARGUMENTS": "One of the function arguments is invalid for ClickHouse; check its documentation.",
    "ILLEGAL_AGGREGATION": (
        "Aggregates cannot be nested or used in WHERE; move the inner aggregate to a "
        "subquery/CTE or use HAVING."
    ),
    "NOT_IMPLEMENTED": "ClickHouse does not support that construct; restructure the query.",
    "TOO_MANY_ROWS": "The query exceeded a server row cap; add tighter WHERE filters or aggregate.",
    "TOO_MANY_ROWS_OR_BYTES": (
        "The query exceeded a server cap on rows read or returned; filter on the "
        "date/partition column, aggregate, or lower the LIMIT."
    ),
    "MEMORY_LIMIT_EXCEEDED": (
        "The query exceeded the memory cap; narrow the date range, avoid huge GROUP BY "
        "cardinality or DISTINCT on wide columns, and aggregate before joining."
    ),
    "TIMEOUT_EXCEEDED": "The query exceeded the time cap; narrow the date range or pre-aggregate.",
    "TOO_SLOW": "The query exceeded the time cap; narrow the date range or pre-aggregate.",
    "READONLY": "Only read-only SELECT/WITH statements can run here.",
    "ACCESS_DENIED": "You do not have access to that object; stay within the tables listed by listTables.",
    "UNSUPPORTED_METHOD": "ClickHouse does not support that operation on this table engine.",
}


def rewrite_clickhouse_error(raw: str) -> str:
    """Return a compact, hint-bearing version of a raw ClickHouse error string.

    Never raises; on an unrecognised shape the input is returned with only the
    trailing version/URL noise stripped.
    """
    text = (raw or "").strip()
    text = _TAIL_URL_RE.sub("", text)
    text = _TAIL_VERSION_RE.sub("", text)

    m = _BODY_RE.search(text)
    if not m:
        return text or "unknown ClickHouse error"
    code, body = m.group(1), m.group(2).strip()

    name = ""
    nm = _NAME_RE.search(body)
    if nm:
        name = nm.group(1)
        body = body[: nm.start()].rstrip()

    # Drop the echoed statement: the caller already has its own SQL.
    body = _IN_QUERY_RE.sub("", body)
    body = _IN_SCOPE_RE.sub("", body)
    body = _FORMAT_NATIVE_RE.sub("", body)

    if name == "SYNTAX_ERROR":
        if _FAILED_AT_NATIVE_RE.search(body):
            body = _FAILED_AT_NATIVE_RE.sub(
                "the statement ended before it was complete (dangling operator, "
                "comma, or unclosed parenthesis at the end).",
                body,
            )
        # The "Expected one of:" list can run to 40+ tokens; keep the first few.
        em = _EXPECTED_RE.search(body)
        if em:
            expected = em.group(0).strip().removeprefix("Expected one of:").strip()
            tokens = [t.strip() for t in expected.rstrip(".").split(",")][:6]
            body = body[: em.start()].rstrip(" .") + ". Expected one of: " + ", ".join(tokens) + " (and more)"

    body = body.rstrip(" .")
    if len(body) > _MAX_BODY:
        body = body[: _MAX_BODY - 1].rstrip() + "…"

    hint = _HINTS.get(name)
    if name == "NOT_AN_AGGREGATE":
        cm = _NOT_AGG_COL_RE.search(body)
        if cm and hint:
            col = cm.group(1).rsplit(".", 1)[-1]
            hint = f"'{col}' appears in SELECT but only a function of it is in GROUP BY. " + hint

    label = f"{name} (code {code})" if name else f"code {code}"
    out = f"{label}: {body}."
    if hint:
        out += f" Hint: {hint}"
    return out
