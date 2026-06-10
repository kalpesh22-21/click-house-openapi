"""Admin CSV ingestion service layer.

Transport-agnostic business logic for the admin ingest feature.
All ClickHouse write operations use user-supplied credentials built into
a one-off client — they NEVER go through the read-only singleton client,
execute_query(), or validate_and_sanitize().

Key design decisions:
- Identifiers (database, table, column names) come from untrusted input and
  are validated against a strict safe-identifier regex before being
  interpolated into DDL.  Any identifier that fails validation causes a
  ValueError (mapped to HTTP 400 by the router).
- ClickHouse type expressions are validated against an explicit whitelist
  regex before use in DDL.
- Parameterized queries (client.query with parameters={}) are used for all
  system.* lookups and the max() watermark query.
- Do NOT import or call app.security.validate_and_sanitize here.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identifier and type safety
# ---------------------------------------------------------------------------

# Safe identifiers: must start with a letter or underscore, contain only
# alphanumeric characters and underscores.  This prevents any SQL injection
# through DDL identifier interpolation.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Used by _sanitize_column_name to replace runs of disallowed characters.
_SANITIZE_COLLAPSE_RE = re.compile(r"[^a-z0-9_]+")

# Whitelist of allowed ClickHouse type expressions (base types and wrappers).
# This regex matches:
#   Int8, Int16, Int32, Int64, UInt8, UInt16, UInt32, UInt64,
#   Float32, Float64, String, Bool, Date, Date32,
#   DateTime, DateTime64(<precision>[, 'tz']),
#   Nullable(<base_type>), LowCardinality(<base_type>)
_ALLOWED_TYPE_RE = re.compile(
    r"^(?:"
    r"Int(?:8|16|32|64)"
    r"|UInt(?:8|16|32|64)"
    r"|Float(?:32|64)"
    r"|String"
    r"|Bool"
    r"|Date(?:32)?"
    r"|DateTime(?:64\(\d+(?:,\s*'[A-Za-z/_]+')?\))?"
    r"|Nullable\((?:Int(?:8|16|32|64)|UInt(?:8|16|32|64)|Float(?:32|64)|String|Bool|Date(?:32)?|DateTime(?:64\(\d+(?:,\s*'[A-Za-z/_]+')?\))?)\)"
    r"|LowCardinality\((?:String|Nullable\(String\))\)"
    r")$"
)

# Pattern used to redact host:port from ClickHouse error messages before they
# reach the caller.  Replicates _HOST_PORT_RE from app/clickhouse_client.py.
_HOST_PORT_RE = re.compile(r"\b[\w\-.]+:\d{2,5}\b")


def validate_identifier(name: str) -> str:
    """Return *name* unchanged if it is a safe SQL identifier.

    Raises ValueError if the identifier contains characters that could
    enable SQL injection through DDL string interpolation.
    """
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid identifier {name!r}: must match ^[A-Za-z_][A-Za-z0-9_]*$"
        )
    return name


def validate_ch_type(type_str: str) -> str:
    """Return *type_str* unchanged if it is a whitelisted ClickHouse type.

    Raises ValueError for any type expression not on the whitelist, preventing
    injection through DDL column type definitions.
    """
    if not _ALLOWED_TYPE_RE.match(type_str.strip()):
        raise ValueError(
            f"Disallowed ClickHouse type {type_str!r}. "
            "Must be one of: Int8/16/32/64, UInt8/16/32/64, Float32/64, "
            "String, Bool, Date, Date32, DateTime, DateTime64(...), "
            "Nullable(<type>), LowCardinality(String)."
        )
    return type_str.strip()


# ---------------------------------------------------------------------------
# Column name sanitization (CSV → valid ClickHouse identifiers)
# ---------------------------------------------------------------------------


def _sanitize_column_name(name: str) -> str:
    """Convert an arbitrary CSV header cell into a valid ClickHouse identifier.

    Steps (applied in order):
    1. Strip leading/trailing whitespace and convert to lowercase.
    2. Replace every run of characters NOT in [a-z0-9_] with a single "_".
    3. Collapse consecutive underscores into one.
    4. Strip leading and trailing underscores.
    5. If the result is empty, return "column".
    6. If the first character is a digit, prepend "_".

    The result always matches ^[A-Za-z_][A-Za-z0-9_]*$ (i.e. _SAFE_IDENTIFIER_RE).

    Examples::

        "Order Date"    → "order_date"
        "Total (USD)"   → "total_usd"
        "2024 Sales"    → "_2024_sales"
        "  --  "        → "column"
    """
    s = name.strip().lower()
    # Replace runs of characters outside [a-z0-9_] with a single underscore.
    s = _SANITIZE_COLLAPSE_RE.sub("_", s)
    # Collapse repeated underscores.
    s = re.sub(r"_+", "_", s)
    # Strip leading/trailing underscores.
    s = s.strip("_")
    # Empty result → fallback.
    if not s:
        return "column"
    # Leading digit → prepend underscore.
    if s[0].isdigit():
        s = "_" + s
    return s


def _sanitize_columns(header: list[str]) -> list[str]:
    """Sanitize a list of CSV header names into unique ClickHouse identifiers.

    Each name is sanitized with _sanitize_column_name.  If two or more sanitized
    names collide, the first occurrence keeps the base name and each subsequent
    collision is suffixed _2, _3, … (re-checking against all already-taken names
    to avoid introducing new collisions).

    Preserves order and length: one output name per input name.
    """
    taken: set[str] = set()
    result: list[str] = []

    for raw in header:
        base = _sanitize_column_name(raw)
        if base not in taken:
            taken.add(base)
            result.append(base)
        else:
            counter = 2
            candidate = f"{base}_{counter}"
            while candidate in taken:
                counter += 1
                candidate = f"{base}_{counter}"
            taken.add(candidate)
            result.append(candidate)

    return result


def _redact_ch_error(msg: str, host: str, password: str) -> str:
    """Remove host:port, user-supplied host string, and password from *msg*.

    Used to sanitize raw ClickHouse error messages before they are surfaced
    to API callers.

    Order of operations:
    1. Broad regex sweep first (catches host:port as a combined token before
       the literal host replacement would break the pattern).
    2. Then replace the literal host string (in case it appeared without a port).
    3. Finally replace the password.
    """
    # Broad sweep first so host:port is replaced as a combined token.
    msg = _HOST_PORT_RE.sub("<host>:<port>", msg)
    if host:
        msg = msg.replace(host, "<host>")
    if password:
        msg = msg.replace(password, "<redacted>")
    return msg


# ---------------------------------------------------------------------------
# One-off admin client builder
# ---------------------------------------------------------------------------


def build_admin_client(
    host: str,
    port: int,
    user: str,
    password: str,
    secure: bool,
    database: str,
) -> Client:
    """Build a one-off clickhouse-connect Client from user-supplied credentials.

    This client is NOT cached and must be closed after use.  It mirrors how
    _build_client works in app/clickhouse_client.py but uses request-provided
    credentials rather than the application settings singleton.

    No readonly_settings are applied — this client is intentionally used for
    DDL and INSERT operations.
    """
    logger.info(
        "Building admin ClickHouse client: host=%s port=%s secure=%s user=%s db=%s",
        host,
        port,
        secure,
        user,
        database,
    )
    return clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        database=database,
        secure=secure,
        verify=secure,
    )


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------

_DATE_FORMAT = "%Y-%m-%d"

# Non-ISO datetime layouts that datetime.fromisoformat() does not accept.
# fromisoformat already covers the common ISO 8601 cases: "T" or space
# separator, optional seconds, fractional seconds, and "+HH:MM" / "Z" offsets.
# Slash formats use US month/day/year ordering (e.g. 03/04/2024 = March 4).
# The year-first (%Y/...) layouts are tried before the US (%m/...) ones; they
# never collide because a 4-digit trailing field can't be a valid day.
_DATETIME_STRPTIME_FALLBACKS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%dT%H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%YT%H:%M:%S",
)

# Bare date layouts, tried after ISO (date.fromisoformat). US month/day/year.
_DATE_STRPTIME_FALLBACKS = (
    "%Y/%m/%d",
    "%m/%d/%Y",
)


def _parse_datetime(value: str) -> datetime | None:
    """Parse *value* into a naive (UTC) datetime, or return None.

    Accepts the broad set of ISO 8601 layouts understood by
    datetime.fromisoformat() — space or "T" separator, optional seconds,
    fractional seconds, and timezone offsets including a trailing "Z" — plus a
    few common non-ISO fallbacks.  A bare date (no time component) parses to
    midnight.  Timezone-aware values are converted to UTC and returned naive so
    they compare cleanly against ClickHouse-sourced (naive) watermarks.
    """
    s = value.strip()
    if not s:
        return None

    # fromisoformat accepts "Z" only on 3.11+; normalize defensively.
    iso = s
    if iso[-1:] in ("Z", "z"):
        iso = iso[:-1] + "+00:00"

    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        for fmt in _DATETIME_STRPTIME_FALLBACKS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue

    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_date(value: str) -> date | None:
    """Parse *value* strictly as a calendar date (YYYY-MM-DD), or return None."""
    s = value.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        for fmt in _DATE_STRPTIME_FALLBACKS:
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None


def _try_parse_datetime(value: str) -> bool:
    """Return True if *value* parses as a datetime WITH a time component.

    A bare date (e.g. "2024-01-01") returns False so it is classified as Date,
    not DateTime — the time component is detected by the presence of ":".
    """
    if ":" not in value:
        return False
    return _parse_datetime(value) is not None


def _try_parse_date(value: str) -> bool:
    """Return True if *value* parses strictly as a bare calendar date."""
    return _parse_date(value) is not None


def infer_column_type(values: list[str]) -> str:
    """Infer a suggested ClickHouse column type from a list of string samples.

    Rules (applied in priority order):
    1. All non-empty values parse as int  -> Int64
    2. All non-empty values parse as float (int or float) -> Float64
    3. All non-empty values parse as datetime (ISO 8601 with time) -> DateTime
    4. All non-empty values parse as date (YYYY-MM-DD) -> Date
    5. Otherwise -> String

    Nullability wrapping:
    - If ANY value in the sample is an empty string, the inferred base type is
      wrapped as Nullable(<type>), EXCEPT String (empty strings are valid
      String values and are not wrapped).
    """
    has_empty = any(v == "" for v in values)
    non_empty = [v for v in values if v != ""]

    if not non_empty:
        # All empty — default to String (can't infer anything useful).
        return "String"

    # --- Int64 ---
    all_int = True
    for v in non_empty:
        try:
            int(v)
        except ValueError:
            all_int = False
            break

    if all_int:
        base = "Int64"
        return f"Nullable({base})" if has_empty else base

    # --- Float64 ---
    all_float = True
    for v in non_empty:
        try:
            float(v)
        except ValueError:
            all_float = False
            break

    if all_float:
        base = "Float64"
        return f"Nullable({base})" if has_empty else base

    # --- DateTime (must test before Date because dates pass date-only check) ---
    all_datetime = all(_try_parse_datetime(v) for v in non_empty)
    if all_datetime:
        base = "DateTime"
        return f"Nullable({base})" if has_empty else base

    # --- Date ---
    all_date = all(_try_parse_date(v) for v in non_empty)
    if all_date:
        base = "Date"
        return f"Nullable({base})" if has_empty else base

    # --- String (no Nullable wrapping — empty string is a valid String value) ---
    return "String"


def infer_schema_streaming(content: bytes) -> tuple[list[str], list[str], int]:
    """Infer a ClickHouse type per column by scanning EVERY data row.

    Returns ``(header, inferred_types, total_data_rows)`` where
    ``inferred_types[i]`` is the suggested type for ``header[i]``.

    Unlike inferring from a leading sample, this examines the whole file so that
    nullability (empty cells) and type widening are detected even when they only
    appear late in the data — e.g. a numeric column whose first empty cell or
    first decimal value is on row 50,000 is still correctly inferred as
    ``Nullable(...)`` / ``Float64`` rather than a non-nullable ``Int64``.

    The decision rules and priority order match infer_column_type().

    Raises ValueError if the content is not valid UTF-8.
    """
    try:
        text = content.decode("utf-8-sig")  # strip BOM if present
    except UnicodeDecodeError as exc:
        raise ValueError("CSV file is not valid UTF-8.") from exc

    reader = csv.reader(io.StringIO(text))
    rows_iter = iter(reader)

    try:
        header = next(rows_iter)
    except StopIteration:
        return [], [], 0

    header = [h.strip() for h in header]
    n = len(header)

    # Per-column accumulators (index-aligned with header).  Each "still_*" flag
    # starts True and is cleared the first time a non-empty value fails to parse
    # as that type; "has_empty" records whether any cell was blank.
    saw_value = [False] * n
    has_empty = [False] * n
    still_int = [True] * n
    still_float = [True] * n
    still_datetime = [True] * n
    still_date = [True] * n

    total = 0
    for row in rows_iter:
        total += 1
        for i in range(n):
            raw = row[i] if i < len(row) else ""
            if raw == "":
                has_empty[i] = True
                continue
            saw_value[i] = True
            if still_int[i]:
                try:
                    int(raw)
                except ValueError:
                    still_int[i] = False
            if still_float[i]:
                try:
                    float(raw)
                except ValueError:
                    still_float[i] = False
            if still_datetime[i] and not _try_parse_datetime(raw):
                still_datetime[i] = False
            if still_date[i] and not _try_parse_date(raw):
                still_date[i] = False

    inferred: list[str] = []
    for i in range(n):
        if not saw_value[i]:
            # All cells empty (or no data rows) — can't infer anything useful.
            inferred.append("String")
            continue
        if still_int[i]:
            base = "Int64"
        elif still_float[i]:
            base = "Float64"
        elif still_datetime[i]:
            base = "DateTime"
        elif still_date[i]:
            base = "Date"
        else:
            # String columns are never wrapped — empty is a valid String value.
            inferred.append("String")
            continue
        inferred.append(f"Nullable({base})" if has_empty[i] else base)

    return header, inferred, total


def parse_csv_sample(
    content: bytes,
    sample_rows: int = 1000,
) -> tuple[list[str], list[list[str]], int]:
    """Parse CSV bytes; return (header, data_rows_sample, total_data_rows).

    Reads up to *sample_rows* data rows for type inference; returns total count
    of all data rows (past the header).

    Raises ValueError if the content is not valid UTF-8.
    """
    try:
        text = content.decode("utf-8-sig")  # strip BOM if present
    except UnicodeDecodeError as exc:
        raise ValueError("CSV file is not valid UTF-8.") from exc

    reader = csv.reader(io.StringIO(text))
    rows_iter = iter(reader)

    try:
        header = next(rows_iter)
    except StopIteration:
        return [], [], 0

    # Strip whitespace from header names.
    header = [h.strip() for h in header]

    sample: list[list[str]] = []
    total = 0
    for row in rows_iter:
        total += 1
        if len(sample) < sample_rows:
            sample.append(row)

    return header, sample, total


def infer_schema(columns: list[str], rows: list[list[str]]) -> list[dict[str, str]]:
    """Return inferred ClickHouse types for each column in *columns*.

    Each column's values are extracted from *rows* and passed to
    infer_column_type().  Missing cells (short rows) are treated as empty.

    Returns a list of {"name": col_name, "suggested_type": ch_type} dicts.
    """
    result = []
    for idx, col in enumerate(columns):
        col_values = [
            row[idx] if idx < len(row) else ""
            for row in rows
        ]
        suggested = infer_column_type(col_values)
        result.append({"name": col, "suggested_type": suggested})
    return result


# ---------------------------------------------------------------------------
# Schema comparison
# ---------------------------------------------------------------------------


def _strip_nullable(type_str: str) -> str:
    """Return the inner type of Nullable(<type>), or *type_str* unchanged."""
    m = re.match(r"^Nullable\((.+)\)$", type_str.strip())
    return m.group(1) if m else type_str.strip()


def compare_schemas(
    csv_columns: list[str],
    inferred: list[dict[str, str]],
    existing: list[dict[str, str]],
) -> tuple[bool, dict[str, Any]]:
    """Compare inferred CSV schema against the existing ClickHouse table schema.

    Matching rule:
    - schema_matches is True iff the set of column names is identical AND there
      are no type mismatches.
    - Type comparison: strip Nullable() wrappers from both sides and compare the
      base type strings.  This is lenient — a CSV column inferred as Int64 is
      considered matching an existing Nullable(Int64) column.

    Returns:
        (schema_matches, schema_diff) where schema_diff contains:
            missing_in_csv: column names in the table but not in the CSV header
            extra_in_csv:   column names in the CSV header but not in the table
            type_mismatches: list of {name, csv_type, table_type} for columns
                             present in both but with differing base types
    """
    csv_name_set = set(csv_columns)
    existing_by_name = {col["name"]: col["type"] for col in existing}
    existing_name_set = set(existing_by_name.keys())

    inferred_by_name = {col["name"]: col["suggested_type"] for col in inferred}

    missing_in_csv = sorted(existing_name_set - csv_name_set)
    extra_in_csv = sorted(csv_name_set - existing_name_set)

    type_mismatches = []
    for col_name in csv_name_set & existing_name_set:
        csv_base = _strip_nullable(inferred_by_name.get(col_name, "String"))
        tbl_base = _strip_nullable(existing_by_name[col_name])
        if csv_base != tbl_base:
            type_mismatches.append(
                {
                    "name": col_name,
                    "csv_type": inferred_by_name.get(col_name, "String"),
                    "table_type": existing_by_name[col_name],
                }
            )

    schema_matches = (not missing_in_csv) and (not extra_in_csv) and (not type_mismatches)

    diff: dict[str, Any] = {
        "missing_in_csv": missing_in_csv,
        "extra_in_csv": extra_in_csv,
        "type_mismatches": type_mismatches,
    }
    return schema_matches, diff


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def coerce(value: str, ch_type: str) -> Any:
    """Coerce a CSV string cell to the Python type expected by ClickHouse.

    Rules:
    - Nullable(<inner>) -> if empty, return None; otherwise delegate to
      coerce(value, inner).
    - Empty string + String (non-nullable) -> "" (empty string is a valid
      String value).
    - Empty string + other non-nullable types -> None (cannot represent empty).
    - Int* -> int (signed types accept any int; UInt* rejects negative values).
    - UInt* -> int, but returns None for negative values.
    - Float* -> float
    - Bool -> bool (accepts true/false/1/0, case-insensitive)
    - Date -> datetime.date (YYYY-MM-DD)
    - DateTime -> datetime.datetime (ISO 8601 with or without T separator)
    - DateTime64(...) -> datetime.datetime (same as DateTime)
    - String -> str (empty string preserved as "")
    - LowCardinality(*) -> delegate to coerce(value, inner)
    - Unknown / fallback -> str
    """
    stripped = ch_type.strip()

    # Handle Nullable wrapper: extract inner type, then treat empty as None.
    nullable_match = re.match(r"^Nullable\((.+)\)$", stripped)
    if nullable_match:
        inner = nullable_match.group(1)
        if value == "":
            return None
        return coerce(value, inner)

    # LowCardinality wrapping: extract inner type (handle before empty check
    # so LowCardinality(String) empty -> coerce("", "String") -> "").
    lc_match = re.match(r"^LowCardinality\((.+)\)$", stripped)
    if lc_match:
        return coerce(value, lc_match.group(1))

    # String: preserve empty string as "" (empty is a valid String value).
    if stripped == "String":
        return value

    # For all other non-Nullable types, empty string -> None (can't represent).
    if value == "":
        return None

    # Int types (signed).
    if re.match(r"^Int(?:8|16|32|64)$", stripped):
        try:
            return int(value)
        except ValueError:
            return None

    # UInt types: reject negative values.
    if re.match(r"^UInt(?:8|16|32|64)$", stripped):
        try:
            v = int(value)
            if v < 0:
                return None
            return v
        except ValueError:
            return None

    # Float types.
    if re.match(r"^Float(?:32|64)$", stripped):
        try:
            return float(value)
        except ValueError:
            return None

    # Bool.
    if stripped == "Bool":
        if value.lower() in ("true", "1"):
            return True
        if value.lower() in ("false", "0"):
            return False
        return None

    # DateTime64 — treat same as DateTime.  A bare date coerces to midnight.
    if stripped.startswith("DateTime"):
        return _parse_datetime(value)

    # Date.
    if stripped in ("Date", "Date32"):
        return _parse_date(value)

    # Unknown/fallback type -> str passthrough.
    return value


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------


def _escape_sql_string(s: str) -> str:
    """Escape *s* for use inside a ClickHouse single-quoted string literal.

    Applies escaping in the required order:
    1. Replace backslash with double-backslash.
    2. Replace single-quote with backslash-single-quote.
    3. Strip null bytes (they are not valid inside SQL string literals).
    4. Escape newline and carriage-return as \\n and \\r respectively.

    The caller is responsible for surrounding the result with single quotes.
    """
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    s = s.replace("\x00", "")
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "\\r")
    return s


def build_create_table_sql(
    database: str,
    table: str,
    columns: list[dict],
    order_by: list[str],
) -> str:
    """Return a CREATE TABLE ... ENGINE=MergeTree DDL string.

    All identifiers and types are validated before use; raises ValueError on
    any invalid input.  Column entries may optionally contain a 'description'
    key; when present and non-empty, a COMMENT clause is appended to that
    column's DDL fragment.

    Args:
        database:  Validated database name.
        table:     Validated table name.
        columns:   List of {"name": col_name, "type": ch_type[, "description": str]} dicts.
        order_by:  List of column names for ORDER BY.  Empty list -> tuple().
    """
    if not columns:
        raise ValueError("Column list must not be empty.")

    col_defs = []
    for col in columns:
        col_name = validate_identifier(col["name"])
        col_type = validate_ch_type(col["type"])
        col_def = f"    `{col_name}` {col_type}"
        desc = col.get("description")
        if desc:
            col_def += f" COMMENT '{_escape_sql_string(desc)}'"
        col_defs.append(col_def)

    cols_sql = ",\n".join(col_defs)

    if order_by:
        order_parts = [f"`{validate_identifier(c)}`" for c in order_by]
        order_sql = f"({', '.join(order_parts)})"
    else:
        order_sql = "tuple()"

    db = validate_identifier(database)
    tbl = validate_identifier(table)

    return (
        f"CREATE TABLE `{db}`.`{tbl}`\n"
        f"(\n{cols_sql}\n)\n"
        f"ENGINE = MergeTree\n"
        f"ORDER BY {order_sql}"
    )


# ---------------------------------------------------------------------------
# System query helpers (parameterized)
# ---------------------------------------------------------------------------


def _query_database_exists(client: Client, database: str) -> bool:
    """Return True if *database* exists in system.databases."""
    result = client.query(
        "SELECT 1 FROM system.databases WHERE name = {db:String}",
        parameters={"db": database},
    )
    return len(result.result_rows) > 0


def _query_table_exists(client: Client, database: str, table: str) -> bool:
    """Return True if *table* exists in *database* per system.tables."""
    result = client.query(
        "SELECT 1 FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
        parameters={"db": database, "tbl": table},
    )
    return len(result.result_rows) > 0


def _query_existing_schema(
    client: Client, database: str, table: str
) -> list[dict[str, str]]:
    """Fetch column name and type for all columns in *database*.*table*.

    Returns a list of {"name": ..., "type": ...} dicts ordered by position.
    """
    result = client.query(
        "SELECT name, type FROM system.columns "
        "WHERE database = {db:String} AND table = {tbl:String} "
        "ORDER BY position",
        parameters={"db": database, "tbl": table},
    )
    return [{"name": row[0], "type": row[1]} for row in result.result_rows]


def _query_existing_comments(
    client: Client, database: str, table: str
) -> dict[str, str]:
    """Fetch column comments for all columns in *database*.*table*.

    Returns a dict mapping column name → comment string (empty string when
    no comment is set).  *_query_existing_schema* is left unchanged so that
    its callers (_append, etc.) are not affected.
    """
    result = client.query(
        "SELECT name, comment FROM system.columns "
        "WHERE database = {db:String} AND table = {tbl:String} "
        "ORDER BY position",
        parameters={"db": database, "tbl": table},
    )
    return {row[0]: (row[1] if row[1] is not None else "") for row in result.result_rows}


def _query_max_timestamp(
    client: Client, database: str, table: str, timestamp_column: str
) -> Any:
    """Return max(*timestamp_column*) from *database*.*table*.

    The column name is validated before interpolation.  The value is returned
    as a native Python type (datetime, date, int, float, or str) as resolved
    by clickhouse-connect.  Returns None if the table is empty.
    """
    col = validate_identifier(timestamp_column)
    db = validate_identifier(database)
    tbl = validate_identifier(table)
    result = client.query(f"SELECT max(`{col}`) FROM `{db}`.`{tbl}`")
    if result.result_rows:
        return result.result_rows[0][0]
    return None


# ---------------------------------------------------------------------------
# Row coercion (name-based)
# ---------------------------------------------------------------------------


def _coerce_row_by_name(
    row: list[str],
    csv_name_to_idx: dict[str, int],
    columns: list[dict[str, str]],
) -> list[Any]:
    """Coerce each declared column by pulling the CSV cell by column name.

    Args:
        row:              Raw CSV row (list of string cells).
        csv_name_to_idx:  Mapping from CSV header name to column index.
        columns:          Declared columns in insert order, each {"name", "type"}.

    Returns a list of coerced values aligned to *columns* order.
    """
    coerced = []
    for col in columns:
        idx = csv_name_to_idx.get(col["name"])
        raw = row[idx] if (idx is not None and idx < len(row)) else ""
        coerced.append(coerce(raw, col["type"]))
    return coerced


def _batch_insert(
    client: Client,
    database: str,
    table: str,
    column_names: list[str],
    rows: list[list[Any]],
    batch_size: int,
) -> int:
    """Insert *rows* in batches; return total rows inserted."""
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        client.insert(
            table=table,
            data=batch,
            column_names=column_names,
            database=database,
        )
        total += len(batch)
    return total


# ---------------------------------------------------------------------------
# Column JSON parsing helper
# ---------------------------------------------------------------------------


def _parse_columns_json(columns_json: str) -> list[dict]:
    """Parse and validate a columns JSON string into a list of column dicts.

    Each entry must have at minimum "name" (valid identifier) and "type"
    (whitelisted ClickHouse type).  An optional "description" key is threaded
    through; if present it must be a string (or None / absent).

    Raises ValueError for:
    - Invalid JSON
    - Invalid identifier in "name"
    - Disallowed type in "type"
    - Non-string "description" value (when present and not None)
    """
    try:
        raw = json.loads(columns_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid columns JSON: {exc}") from exc

    if not isinstance(raw, list):
        raise ValueError("columns JSON must be a JSON array.")

    result: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each entry in 'columns' must be a JSON object.")
        if "name" not in item or "type" not in item:
            raise ValueError("Each column entry must have 'name' and 'type'.")
        validate_identifier(item["name"])
        validate_ch_type(item["type"])
        name = item["name"]
        if name in seen:
            raise ValueError(f"Duplicate column name {name!r} in 'columns'.")
        seen.add(name)
        desc = item.get("description")
        if desc is not None and not isinstance(desc, str):
            raise ValueError(
                f"Column 'description' must be a string or null/absent, "
                f"got {type(desc).__name__!r} for column {item['name']!r}."
            )
        result.append({"name": name, "type": item["type"], "description": desc})

    return result


# ---------------------------------------------------------------------------
# Column report builder
# ---------------------------------------------------------------------------


def _build_column_report(
    header: list[str],
    sanitized: list[str],
    inferred: list[dict[str, str]],
    existing: list[dict[str, str]] | None,
    existing_comments: dict[str, str] | None,
) -> list[dict]:
    """Build the per-column analysis report for the analyze() response.

    For each CSV column i, produces::

        {
            "original_name": header[i],
            "name": sanitized[i],
            "inferred_type": <type inferred from CSV values>,
            "existing_type": <type from existing table, or None>,
            "description": <comment from existing table, or "">,
            "in_csv": True,
        }

    Then, for any column in the existing table that is NOT in *sanitized*,
    appends::

        {
            "original_name": colname,
            "name": colname,
            "inferred_type": None,
            "existing_type": existing[colname],
            "description": existing_comments.get(colname, ""),
            "in_csv": False,
        }

    *inferred* is the list returned by infer_schema() keyed by SANITIZED column name
    (not the raw header name).  *existing* is the list returned by
    _query_existing_schema() ({name: type} after conversion).
    """
    # Build lookup from sanitized column name → inferred type.
    inferred_by_raw: dict[str, str] = {col["name"]: col["suggested_type"] for col in inferred}

    # Build existing type lookup (name → type) from existing schema list.
    existing_by_name: dict[str, str] = {}
    if existing:
        for col in existing:
            existing_by_name[col["name"]] = col["type"]

    existing_comments_safe: dict[str, str] = existing_comments or {}

    entries: list[dict] = []
    sanitized_set: set[str] = set(sanitized)

    for i, raw_name in enumerate(header):
        san = sanitized[i]
        entries.append({
            "original_name": raw_name,
            "name": san,
            "inferred_type": inferred_by_raw.get(san),
            "existing_type": existing_by_name.get(san),
            "description": existing_comments_safe.get(san, ""),
            "in_csv": True,
        })

    # Columns that exist in the table but are absent from the CSV.
    for colname, coltype in existing_by_name.items():
        if colname not in sanitized_set:
            entries.append({
                "original_name": colname,
                "name": colname,
                "inferred_type": None,
                "existing_type": coltype,
                "description": existing_comments_safe.get(colname, ""),
                "in_csv": False,
            })

    return entries


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


def analyze(
    host: str,
    port: int,
    user: str,
    password: str,
    secure: bool,
    database: str,
    table: str,
    file_content: bytes,
) -> dict[str, Any]:
    """Analyze a CSV file against a ClickHouse table and return a schema report.

    Steps:
    1. Connect with user-supplied credentials and ping.
    2. Check database and table existence.
    3. Parse CSV header + up to 1000 data rows for type inference.
    4. Fetch existing schema if the table exists.
    5. Compute schema_matches and schema_diff.

    Raises:
        ValueError: On connection failure, identifier validation errors, or
                    ClickHouse errors (with credentials redacted).
    """
    # Validate identifiers early so bad input is rejected before a connection.
    validate_identifier(database)
    validate_identifier(table)

    try:
        client = build_admin_client(host, port, user, password, secure, database)
        alive = client.ping()
    except ClickHouseError as exc:
        redacted = _redact_ch_error(str(exc), host, password)
        raise ValueError(f"ClickHouse connection failed: {redacted}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"ClickHouse connection failed: {exc}") from exc

    if not alive:
        raise ValueError("ClickHouse ping returned False — server may be unreachable.")

    try:
        db_exists = _query_database_exists(client, database)
        tbl_exists = _query_table_exists(client, database, table)

        # Scan ALL data rows for type inference (not just a leading sample) so
        # nullability and type widening reflect the entire file.
        header, inferred_types, total_data_rows = infer_schema_streaming(file_content)
        sanitized = _sanitize_columns(header)
        # Key inferred entries by sanitized name so they match what is stored in
        # the existing table (also sanitized).
        inferred = [
            {"name": sanitized[i], "suggested_type": inferred_types[i]}
            for i in range(len(header))
        ]

        existing_schema: list[dict[str, str]] | None = None
        existing_comments: dict[str, str] | None = None
        schema_matches: bool | None = None
        schema_diff: dict[str, Any] | None = None

        if tbl_exists:
            existing_schema = _query_existing_schema(client, database, table)
            existing_comments = _query_existing_comments(client, database, table)
            schema_matches, schema_diff = compare_schemas(sanitized, inferred, existing_schema)

        columns_report = _build_column_report(
            header, sanitized, inferred, existing_schema, existing_comments
        )

    except ClickHouseError as exc:
        redacted = _redact_ch_error(str(exc), host, password)
        raise ValueError(f"ClickHouse error: {redacted}") from exc
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    return {
        "connection_ok": True,
        "database_exists": db_exists,
        "table_exists": tbl_exists,
        "csv_columns": header,
        "csv_row_sample_count": total_data_rows,
        "inferred_columns": inferred,
        "existing_schema": existing_schema,
        "schema_matches": schema_matches,
        "schema_diff": schema_diff,
        "columns": columns_report,
    }


def ingest(
    host: str,
    port: int,
    user: str,
    password: str,
    secure: bool,
    database: str,
    table: str,
    file_content: bytes,
    mode: str,
    columns_json: str | None,
    order_by_json: str | None,
    timestamp_column: str | None,
    batch_size: int = 50_000,
) -> dict[str, Any]:
    """Ingest a CSV file into a ClickHouse table.

    Modes:
    - "create": CREATE DATABASE IF NOT EXISTS; CREATE TABLE (fails if exists).
    - "wipe":   CREATE DATABASE IF NOT EXISTS; DROP + CREATE TABLE; insert all.
    - "append": table must exist; watermark-filter rows; insert survivors.

    Raises:
        ValueError: On validation errors, mode-specific precondition failures,
                    or ClickHouse errors (with credentials redacted).
    """
    if mode not in ("create", "wipe", "append"):
        raise ValueError(f"Invalid mode {mode!r}. Must be 'create', 'wipe', or 'append'.")

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")

    validate_identifier(database)
    validate_identifier(table)

    # Parse columns from JSON if provided; otherwise auto-build from CSV header.
    columns: list[dict] | None = None
    if columns_json:
        columns = _parse_columns_json(columns_json)
    # columns left as None here if columns_json absent; built from CSV header
    # later (inside _run_ingest after header is parsed) for create/wipe.

    # Parse order_by from JSON if provided.
    order_by: list[str] = []
    if order_by_json:
        try:
            order_by = json.loads(order_by_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid order_by JSON: {exc}") from exc
        for ob in order_by:
            validate_identifier(ob)

    if mode == "append" and not timestamp_column:
        raise ValueError("timestamp_column is required for append mode.")

    if timestamp_column:
        validate_identifier(timestamp_column)

    # Build client.
    try:
        client = build_admin_client(host, port, user, password, secure, database)
    except ClickHouseError as exc:
        redacted = _redact_ch_error(str(exc), host, password)
        raise ValueError(f"ClickHouse connection failed: {redacted}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"ClickHouse connection failed: {exc}") from exc

    try:
        return _run_ingest(
            client=client,
            host=host,
            password=password,
            database=database,
            table=table,
            file_content=file_content,
            mode=mode,
            columns=columns,
            order_by=order_by,
            timestamp_column=timestamp_column,
            batch_size=batch_size,
        )
    except ClickHouseError as exc:
        redacted = _redact_ch_error(str(exc), host, password)
        raise ValueError(f"ClickHouse error: {redacted}") from exc
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _run_ingest(
    client: Client,
    host: str,
    password: str,
    database: str,
    table: str,
    file_content: bytes,
    mode: str,
    columns: list[dict] | None,
    order_by: list[str],
    timestamp_column: str | None,
    batch_size: int,
) -> dict[str, Any]:
    """Execute the ingest logic after a client has been established.

    All CSV-to-DB column mapping is done BY NAME (not by position) so that
    a CSV whose column order differs from the declared/table column order
    still lands values in the correct columns.

    When *columns* is None (no columns_json was provided by the caller) and
    mode is 'create' or 'wipe', column names and types are auto-inferred from
    the CSV header and sample rows using sanitized snake_case names.
    """
    db = validate_identifier(database)
    tbl = validate_identifier(table)

    # For create/wipe without explicit columns, parse CSV now to auto-build schema.
    # We use a sample (up to 1000 rows) for type inference; the full streaming
    # ingest happens later.  Track whether auto-build happened so we can switch
    # from name-based to positional CSV→column mapping below.
    _auto_built_columns = False
    if columns is None and mode in ("create", "wipe"):
        # Scan ALL rows for type inference so nullability/widening is detected
        # across the whole file, not just a leading sample.
        auto_header, auto_types, _ = infer_schema_streaming(file_content)
        if not auto_header:
            raise ValueError("CSV has no header row — cannot auto-infer schema.")
        sanitized_auto = _sanitize_columns(auto_header)
        columns = [
            {
                "name": sanitized_auto[i],
                "type": auto_types[i],
                "description": None,
            }
            for i in range(len(auto_header))
        ]
        _auto_built_columns = True

    if mode == "create":
        # We never create the database — it must already exist. ClickHouse
        # returns a clear "database doesn't exist" error if it is missing.
        # Fail if table already exists.
        if _query_table_exists(client, database, table):
            raise ValueError(
                f"Table `{db}`.`{tbl}` already exists. Use mode 'wipe' to recreate "
                "or mode 'append' to add rows."
            )
        ddl = build_create_table_sql(database, table, columns, order_by)  # type: ignore[arg-type]
        client.command(ddl)

    elif mode == "wipe":
        # We never create the database — it must already exist.
        client.command(f"DROP TABLE IF EXISTS `{db}`.`{tbl}`")
        ddl = build_create_table_sql(database, table, columns, order_by)  # type: ignore[arg-type]
        client.command(ddl)

    elif mode == "append":
        if not _query_table_exists(client, database, table):
            raise ValueError(
                f"Table `{db}`.`{tbl}` does not exist. Use mode 'create' to create it."
            )
        # For append, columns come from the existing table schema.
        columns = _query_existing_schema(client, database, table)
        if not columns:
            raise ValueError(
                f"Table `{db}`.`{tbl}` has no columns — cannot append."
            )

    assert columns is not None  # guaranteed by checks above

    # Parse CSV header (for name-based mapping) and decode content.
    # parse_csv_sample raises ValueError on non-UTF-8 content.
    header, _, _ = parse_csv_sample(file_content, sample_rows=0)
    if not header:
        # Empty CSV — nothing to insert.
        return {
            "mode": mode,
            "table": f"{database}.{table}",
            "rows_in_csv": 0,
            "rows_inserted": 0,
            "rows_skipped": 0,
            "watermark": None,
        }

    col_names = [c["name"] for c in columns]

    # Build the CSV header name → column index mapping.
    # For auto-built columns (sanitized names from CSV header), use positional
    # mapping so that sanitized name i → CSV position i.
    # For explicitly-declared columns (create/wipe with columns_json), use name-based
    # mapping against the raw header (declared names must match CSV header names exactly).
    # For append columns (from existing table schema — sanitized names), sanitize the
    # CSV header names too so they match the table's sanitized column names.
    if _auto_built_columns:
        # Positional mapping: sanitized column name → its CSV position index.
        csv_name_to_idx = {col["name"]: i for i, col in enumerate(columns)}
        missing_cols: list[str] = []  # positions guaranteed to exist
    elif mode == "append":
        # Append: existing table has sanitized column names; CSV header may be messy.
        # Sanitize the CSV header names and map sanitized→index so they align.
        sanitized_header = _sanitize_columns(header)
        csv_name_to_idx = {san: i for i, san in enumerate(sanitized_header)}
        missing_cols = [c["name"] for c in columns if c["name"] not in csv_name_to_idx]
    else:
        # Explicit columns (create/wipe with columns_json). The UI normalizes
        # column names to sanitized snake_case before sending them, while the raw
        # CSV header still carries the original messy names. Sanitize the header
        # so each declared column aligns with its source CSV column by normalized
        # form (matching how the table itself was created).
        sanitized_header = _sanitize_columns(header)
        csv_name_to_idx = {san: i for i, san in enumerate(sanitized_header)}
        missing_cols = [c["name"] for c in columns if c["name"] not in csv_name_to_idx]
    if missing_cols:
        raise ValueError(
            f"CSV is missing columns required by the declared schema: "
            f"{', '.join(missing_cols)}"
        )

    # Streaming ingest: decode CSV once and iterate rows, coercing and batching.
    try:
        text = file_content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV file is not valid UTF-8.") from exc

    reader = csv.reader(io.StringIO(text))
    rows_iter = iter(reader)
    # Skip header row.
    try:
        next(rows_iter)
    except StopIteration:
        return {
            "mode": mode,
            "table": f"{database}.{table}",
            "rows_in_csv": 0,
            "rows_inserted": 0,
            "rows_skipped": 0,
            "watermark": None,
        }

    if mode != "append":
        # create / wipe: stream all rows, coerce by name, batch insert.
        batch: list[list[Any]] = []
        total_data_rows = 0
        inserted = 0

        for row in rows_iter:
            total_data_rows += 1
            coerced = _coerce_row_by_name(row, csv_name_to_idx, columns)
            batch.append(coerced)
            if len(batch) >= batch_size:
                client.insert(
                    table=table,
                    data=batch,
                    column_names=col_names,
                    database=database,
                )
                inserted += len(batch)
                batch = []

        if batch:
            client.insert(
                table=table,
                data=batch,
                column_names=col_names,
                database=database,
            )
            inserted += len(batch)

        return {
            "mode": mode,
            "table": f"{database}.{table}",
            "rows_in_csv": total_data_rows,
            "rows_inserted": inserted,
            "rows_skipped": 0,
            "watermark": None,
        }

    # Append mode: watermark filter applied row-by-row during streaming.
    assert timestamp_column is not None  # validated above

    # Resolve the timestamp column to the sanitized name actually used by the
    # table and the csv_name_to_idx mapping. The UI populates the picker from the
    # raw CSV header, so the incoming value may be an original (un-sanitized)
    # header name (e.g. "OrderDate" → "orderdate").
    if timestamp_column in csv_name_to_idx:
        ts_name = timestamp_column
    elif timestamp_column in header:
        ts_name = sanitized_header[header.index(timestamp_column)]
    else:
        ts_name = _sanitize_column_name(timestamp_column)

    watermark = _query_max_timestamp(client, database, table, ts_name)
    watermark_str: str | None = str(watermark) if watermark is not None else None

    # Find the timestamp column's type in the existing schema.
    ts_col_type = "String"
    for col in columns:
        if col["name"] == ts_name:
            ts_col_type = col["type"]
            break

    # Note: String-typed timestamp columns require zero-padded ISO-8601 strings
    # for correct lexicographic comparison (e.g. "2024-01-01 00:00:00").
    if _strip_nullable(ts_col_type) == "String":
        logger.warning(
            "timestamp_column %r has type %r; watermark comparison is lexicographic. "
            "Ensure timestamps are zero-padded ISO-8601 strings for correct ordering.",
            timestamp_column,
            ts_col_type,
        )

    ts_idx = csv_name_to_idx.get(ts_name, -1)
    if ts_idx == -1:
        raise ValueError(
            f"timestamp_column {timestamp_column!r} not found in CSV header: {header}"
        )

    batch = []
    total_data_rows = 0
    inserted = 0
    rows_skipped = 0

    for row in rows_iter:
        total_data_rows += 1

        if watermark is None:
            # Empty table — keep all rows.
            coerced = _coerce_row_by_name(row, csv_name_to_idx, columns)
            batch.append(coerced)
        else:
            raw_ts = row[ts_idx] if ts_idx < len(row) else ""
            coerced_ts = coerce(raw_ts, ts_col_type)
            try:
                if coerced_ts is not None and coerced_ts > watermark:
                    coerced = _coerce_row_by_name(row, csv_name_to_idx, columns)
                    batch.append(coerced)
                else:
                    rows_skipped += 1
            except TypeError:
                # Incomparable types — log a warning and skip conservatively.
                logger.warning(
                    "TypeError comparing timestamp value %r (type %r) against "
                    "watermark %r — skipping row.",
                    coerced_ts,
                    ts_col_type,
                    watermark,
                )
                rows_skipped += 1

        if len(batch) >= batch_size:
            client.insert(
                table=table,
                data=batch,
                column_names=col_names,
                database=database,
            )
            inserted += len(batch)
            batch = []

    if batch:
        client.insert(
            table=table,
            data=batch,
            column_names=col_names,
            database=database,
        )
        inserted += len(batch)

    return {
        "mode": mode,
        "table": f"{database}.{table}",
        "rows_in_csv": total_data_rows,
        "rows_inserted": inserted,
        "rows_skipped": rows_skipped,
        "watermark": watermark_str,
    }
