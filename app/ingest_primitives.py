"""Ingest primitives — hardened, transport-agnostic CSV/DDL building blocks.

Pure, reusable helpers shared by the MCP scratch/upload write paths
(app.scratch_ingest, app.upload_ingest) and employee-access provisioning.
There is no ClickHouse client or credential handling here — callers own the
connection; this module only validates, infers, coerces, and builds SQL text.

Key design decisions:
- Identifiers (database, table, column names) come from untrusted input and
  are validated against a strict safe-identifier regex before being
  interpolated into DDL.  Any identifier that fails validation causes a
  ValueError.
- ClickHouse type expressions are validated against an explicit whitelist
  regex before use in DDL.
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


