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
from datetime import date, datetime
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

_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
)
_DATE_FORMAT = "%Y-%m-%d"


def _try_parse_datetime(value: str) -> bool:
    """Return True if *value* parses as a datetime but NOT as a bare date."""
    for fmt in _DATETIME_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            pass
    return False


def _try_parse_date(value: str) -> bool:
    """Return True if *value* parses strictly as YYYY-MM-DD (date only)."""
    try:
        datetime.strptime(value, _DATE_FORMAT)
        # Reject strings that also match a datetime format (they won't here
        # because bare dates have no time component, but be explicit).
        return True
    except ValueError:
        return False


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

    # DateTime64 — treat same as DateTime.
    if stripped.startswith("DateTime"):
        for fmt in _DATETIME_FORMATS:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                pass
        # Fallback: try date-only string and return as midnight datetime.
        try:
            return datetime.strptime(value, _DATE_FORMAT)
        except ValueError:
            return None

    # Date.
    if stripped in ("Date", "Date32"):
        try:
            return datetime.strptime(value, _DATE_FORMAT).date()
        except ValueError:
            return None

    # Unknown/fallback type -> str passthrough.
    return value


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------


def build_create_table_sql(
    database: str,
    table: str,
    columns: list[dict[str, str]],
    order_by: list[str],
) -> str:
    """Return a CREATE TABLE ... ENGINE=MergeTree DDL string.

    All identifiers and types are validated before use; raises ValueError on
    any invalid input.

    Args:
        database:  Validated database name.
        table:     Validated table name.
        columns:   List of {"name": col_name, "type": ch_type} dicts.
        order_by:  List of column names for ORDER BY.  Empty list -> tuple().
    """
    if not columns:
        raise ValueError("Column list must not be empty.")

    col_defs = []
    for col in columns:
        col_name = validate_identifier(col["name"])
        col_type = validate_ch_type(col["type"])
        col_defs.append(f"    `{col_name}` {col_type}")

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

        header, sample, total_data_rows = parse_csv_sample(file_content, sample_rows=1000)
        inferred = infer_schema(header, sample)

        existing_schema: list[dict[str, str]] | None = None
        schema_matches: bool | None = None
        schema_diff: dict[str, Any] | None = None

        if tbl_exists:
            existing_schema = _query_existing_schema(client, database, table)
            schema_matches, schema_diff = compare_schemas(header, inferred, existing_schema)

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
        "csv_row_sample_count": len(sample),
        "inferred_columns": inferred,
        "existing_schema": existing_schema,
        "schema_matches": schema_matches,
        "schema_diff": schema_diff,
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

    # Parse columns from JSON if provided.
    columns: list[dict[str, str]] | None = None
    if columns_json:
        try:
            columns = json.loads(columns_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid columns JSON: {exc}") from exc
        for col in columns:
            validate_identifier(col["name"])
            validate_ch_type(col["type"])

    # Parse order_by from JSON if provided.
    order_by: list[str] = []
    if order_by_json:
        try:
            order_by = json.loads(order_by_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid order_by JSON: {exc}") from exc
        for ob in order_by:
            validate_identifier(ob)

    if mode in ("create", "wipe") and not columns:
        raise ValueError(f"columns is required for mode '{mode}'.")

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
    columns: list[dict[str, str]] | None,
    order_by: list[str],
    timestamp_column: str | None,
    batch_size: int,
) -> dict[str, Any]:
    """Execute the ingest logic after a client has been established.

    All CSV-to-DB column mapping is done BY NAME (not by position) so that
    a CSV whose column order differs from the declared/table column order
    still lands values in the correct columns.
    """
    db = validate_identifier(database)
    tbl = validate_identifier(table)

    if mode == "create":
        # Fail if table already exists.
        if _query_table_exists(client, database, table):
            raise ValueError(
                f"Table `{db}`.`{tbl}` already exists. Use mode 'wipe' to recreate "
                "or mode 'append' to add rows."
            )
        client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
        ddl = build_create_table_sql(database, table, columns, order_by)  # type: ignore[arg-type]
        client.command(ddl)

    elif mode == "wipe":
        client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
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

    # For create/wipe: validate that every declared column name exists in the CSV header.
    # For append: every table column must be present in the CSV header.
    csv_name_to_idx = {name: i for i, name in enumerate(header)}
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
    watermark = _query_max_timestamp(client, database, table, timestamp_column)
    watermark_str: str | None = str(watermark) if watermark is not None else None

    # Find the timestamp column's type in the existing schema.
    ts_col_type = "String"
    for col in columns:
        if col["name"] == timestamp_column:
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

    ts_idx = csv_name_to_idx.get(timestamp_column, -1)
    # ts_idx cannot be -1 here because missing_cols check above would have caught it,
    # but guard defensively.
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
