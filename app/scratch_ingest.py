"""Session-scoped scratch-write side-channel (table-intermediate Slice 1).

This is the WRITE half of the scratch surface.  The READ half (session-gated
`scratch.s_<session_id>_*` isolation) already lives in
``app/sqlparse/provenance.py`` (D64/D69/D92) and is unchanged.  This module lets
the trusted runtime materialize an already-scope-checked node result into a
session-partitioned scratch table so a downstream node can ``JOIN`` it.

It is a PRIVILEGED SIDE-CHANNEL, not an MCP tool (D19):
  - It is never registered with ``@mcp.tool`` — the agent LLM cannot see or call
    it.  It rides as a custom HTTP route on the same MCP app so it inherits the
    ``JWTAuthMiddleware`` session binding (D92).
  - The table name's ``s_<session_id>_`` prefix is derived ONLY from the
    D92-bound ``X-Session-Id`` (via ``current_session_id``), NEVER from the
    request body.  A caller can therefore only ever write to its OWN scratch
    namespace (isolation invariant #2).
  - The rows are DATA: they are loaded via the native driver
    (``client.insert(data=...)``), never string-interpolated into SQL
    (injection invariant #4) — the same hardened primitives ``ingest_primitives`` provides.
  - It executes via a distinct, server-side-only ClickHouse credential GRANTed on
    the ``scratch`` database only, so a total logic bug still cannot touch the
    warehouse (blast-radius invariant #1).

Session-id contract for the runtime caller (Slice 2 — MUST hold):
  The bound ``X-Session-Id`` that names the scratch table MUST be an
  identifier-safe (``^[A-Za-z_][A-Za-z0-9_]*$``) AND **underscore-free** string —
  a hex token is the recommendation.  Underscore-free is load-bearing, not
  cosmetic: the scratch namespace is delimited by ``_`` (``s_<sid>_bp_<hex>``), so
  a session_id that itself contains ``_`` reintroduces a ``_``-boundary ambiguity
  where one session id is a prefix of another (``a`` vs ``a_b``).  The drop
  authorizer here defends against that with an EXACT structural match, and the
  create side rejects any session_id that is not identifier-safe; but the Slice-2
  runtime sanitizer must strip a raw uuid4's hyphens to **hex** (NOT convert them
  to underscores) so the sanitized id stays underscore-free.

Reuse of ``ingest_primitives`` (the proven write primitives, G1):
  - ``validate_identifier`` / ``validate_ch_type`` — the safe-identifier regex
    and the ClickHouse-type whitelist.
  - ``build_scratch_client`` — a one-off, non-singleton client with NO
    readonly_settings, built from *server* config (never request-supplied creds).
  - ``coerce`` — hardened per-type value coercion for string cells.
  - ``client.insert(data=..., column_names=...)`` — native bulk insert.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from app.ingest_primitives import coerce, validate_ch_type, validate_identifier
from app.config import Settings

logger = logging.getLogger(__name__)

# The wall-clock TTL is expressed against a hidden DEFAULT-valued DateTime column,
# because a ClickHouse table TTL expression must reference a Date/DateTime column
# (there is no column-less `TTL now() + INTERVAL ...` form on MergeTree).  The
# column is inserted with a DEFAULT so callers never supply it, and its leading
# underscore keeps it out of the way of user column names.  A user column named
# this is rejected to avoid a collision.
_TTL_COLUMN = "_scratch_created_at"

# The server-generated suffix on every scratch table name: `bp_<uuid4hex>` (32
# lowercase hex chars).  It is the ONLY shape ``materialize`` produces, so the
# drop authorizer can require it verbatim — a drop target that this session's
# materialize could not have created is refused (see ``_bare_scratch_name``).
_BP_SUFFIX_RE = r"bp_[0-9a-f]{32}"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScratchWriteError(Exception):
    """A scratch-write request was rejected.

    Carries a stable *code* and an HTTP *status_code* so the route layer renders a
    consistent error shape without leaking internals.  Messages are safe to return
    (they never echo row data or credentials).
    """

    def __init__(self, message: str, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class ScratchTooLargeError(ScratchWriteError):
    """The row count exceeds SCRATCH_MAX_ROWS — fail closed (invariant #6, OQ-C)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SCRATCH_TOO_LARGE", status_code=413)


# ---------------------------------------------------------------------------
# Privileged, scratch-only client
# ---------------------------------------------------------------------------


def build_scratch_client(settings: Settings) -> Client:
    """Build a one-off ClickHouse client from the SERVER-side scratch credential.

    A non-cached client with NO readonly_settings — it is intentionally used for
    DDL/INSERT — sourcing its credentials from server config
    (``scratch_client_params``), NOT from the request.  The runtime never holds
    these credentials (invariant #8): the
    privilege is the server-side, scratch-only grant.

    The caller MUST close the returned client.
    """
    params = settings.scratch_client_params()
    host = str(params["host"])
    port = int(params["port"])
    user = str(params["user"])
    password = str(params["password"])
    secure = bool(params["secure"])
    logger.info(
        "Building scratch ClickHouse client: host=%s port=%s secure=%s user=%s db=%s",
        host,
        port,
        secure,
        user,
        settings.scratch_database,
    )
    return clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        # Connect at the server level (no default database) so the scratch DB can
        # be bootstrapped and every statement is explicitly `scratch`-qualified.
        secure=secure,
        verify=secure,
    )


# ---------------------------------------------------------------------------
# Naming (derived from the bound session_id ONLY, never the body)
# ---------------------------------------------------------------------------


def scratch_table_name(session_id: str) -> str:
    """Return a fresh ``s_<session_id>_bp_<uuid4hex>`` scratch table name.

    The ``s_<session_id>_`` prefix is byte-compatible with the read-side
    ``_validate_scratch_name`` check, so the very table written here is one the
    D64 read gate accepts for THIS session only.  The ``bp_<uuid>`` suffix is
    server-generated (collision-free; two materializations in one DAG never
    clash).

    The full name is re-run through ``validate_identifier`` — this both rejects a
    hostile session_id and enforces the pre-existing scratch contract that a
    session_id must be a safe SQL identifier (the unqualified ``scratch.s_..``
    name must parse without backtick quoting for the read gate to see it).

    UNDERSCORE-FREE is enforced STRUCTURALLY here (HIGH fix): the D64 read gate
    (``_validate_scratch_name``) recovers a table's owning session by extracting the
    run after ``s_`` up to the NEXT ``_`` — which is only unambiguous when the
    session_id itself contains no ``_``.  ``validate_identifier`` PERMITS ``_``, so
    a session_id like ``a_b`` would otherwise mint ``s_a_b_bp_<hex>``, which the
    read gate mis-attributes to session ``a`` (an ownership INVERSION: ``a`` reads
    it, the true owner ``a_b`` is denied).  Rejecting a ``_``-containing session_id
    at THIS write boundary makes the underscore-free invariant a structural
    property of every scratch table that can exist — the read gate never faces an
    ambiguous name — rather than a discipline enforced only at the UI BFF.
    """
    if not session_id:
        raise ScratchWriteError(
            "No bound session; cannot derive a scratch table name.",
            code="SCRATCH_SESSION_MISSING",
        )
    if "_" in session_id:
        # Defense-in-depth for the D64 read gate's exact session extraction: an
        # underscore-containing sid breaks the `_`-delimited ownership recovery.
        raise ScratchWriteError(
            "Session id must be underscore-free (the scratch namespace is "
            "'_'-delimited; a '_' in the session id breaks ownership extraction).",
            code="SCRATCH_MATERIALIZE_REJECTED",
        )
    name = f"s_{session_id}_bp_{uuid.uuid4().hex}"
    try:
        validate_identifier(name)
    except ValueError as exc:
        # A session_id carrying characters outside [A-Za-z0-9_] cannot form a valid
        # unqualified scratch identifier — reject rather than backtick-smuggle it.
        raise ScratchWriteError(
            "Session id does not form a safe scratch identifier.",
            code="SCRATCH_MATERIALIZE_REJECTED",
        ) from exc
    return name


def _bare_scratch_name(table: str, session_id: str) -> str:
    """Validate a caller-supplied drop target and return its bare table name.

    Accepts either ``scratch.s_<sid>_bp_<hex>`` or a bare ``s_<sid>_bp_<hex>``.
    The name must EXACTLY match the shape THIS session's ``materialize`` could have
    produced — ``^s_<re.escape(session_id)>_bp_[0-9a-f]{32}$`` — else the drop is
    refused 403 SCRATCH_SESSION_VIOLATION.

    Why exact-structure, not a loose ``startswith(f"s_{sid}_")`` prefix (review
    FIX 1): the scratch namespace is ``_``-delimited and a session_id may itself
    contain ``_``, so a loose prefix has a ``_``-boundary ambiguity — a session
    bound to ``a`` would match a table owned by ``a_b`` (``s_a_b_bp_...`` starts
    with ``s_a_``) and could DROP it.  Anchoring the whole name to the exact
    ``s_<sid>_bp_<32hex>`` structure removes that ambiguity without depending on a
    session-id minting discipline this service cannot enforce: ``a`` can only ever
    match ``s_a_bp_<hex>``, never ``s_a_b_bp_<hex>``.
    """
    if not session_id:
        raise ScratchWriteError(
            "No bound session; cannot authorize a scratch drop.",
            code="SCRATCH_SESSION_MISSING",
        )
    if not isinstance(table, str) or not table:
        raise ScratchWriteError(
            "Missing 'table' in drop request.", code="SCRATCH_MATERIALIZE_REJECTED"
        )
    bare = table.split(".", 1)[1] if "." in table else table
    own_pattern = re.compile(r"^s_" + re.escape(session_id) + r"_" + _BP_SUFFIX_RE + r"$")
    if not own_pattern.match(bare):
        # Not a table this session's materialize could have created — a foreign or
        # malformed target.  Fail closed (defense-in-depth mirror of the D64 read
        # gate); we can never prove ownership of a non-conforming name.
        raise ScratchWriteError(
            "Scratch table does not belong to this session.",
            code="SCRATCH_SESSION_VIOLATION",
            status_code=403,
        )
    # Belt-and-suspenders: the regex already constrains bare to identifier-safe
    # characters, but re-validate before interpolating into DDL.
    try:
        validate_identifier(bare)
    except ValueError as exc:
        raise ScratchWriteError(
            "Invalid scratch table name.", code="SCRATCH_MATERIALIZE_REJECTED"
        ) from exc
    return bare


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def _validate_columns(columns: Any) -> list[dict[str, str]]:
    """Validate the request ``columns`` into a clean [{name, type}] list.

    Every name passes ``validate_identifier`` and every type passes
    ``validate_ch_type`` (the existing whitelist) — a hostile name/type is
    rejected here, never interpolated.
    """
    if not isinstance(columns, list) or not columns:
        raise ScratchWriteError(
            "'columns' must be a non-empty array of {name, type}.",
            code="SCRATCH_MATERIALIZE_REJECTED",
        )
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in columns:
        if not isinstance(item, dict) or "name" not in item or "type" not in item:
            raise ScratchWriteError(
                "Each column must be an object with 'name' and 'type'.",
                code="SCRATCH_MATERIALIZE_REJECTED",
            )
        name = item["name"]
        ch_type = item["type"]
        if not isinstance(name, str) or not isinstance(ch_type, str):
            raise ScratchWriteError(
                "Column 'name' and 'type' must be strings.",
                code="SCRATCH_MATERIALIZE_REJECTED",
            )
        try:
            validate_identifier(name)
            validate_ch_type(ch_type)
        except ValueError as exc:
            raise ScratchWriteError(
                "Rejected column name or type (failed identifier/type whitelist).",
                code="SCRATCH_MATERIALIZE_REJECTED",
            ) from exc
        if name == _TTL_COLUMN:
            raise ScratchWriteError(
                f"Column name {name!r} is reserved.",
                code="SCRATCH_MATERIALIZE_REJECTED",
            )
        if name in seen:
            raise ScratchWriteError(
                f"Duplicate column name {name!r}.",
                code="SCRATCH_MATERIALIZE_REJECTED",
            )
        seen.add(name)
        result.append({"name": name, "type": ch_type.strip()})
    return result


def _validate_rows(rows: Any, n_cols: int, max_rows: int) -> list[list[Any]]:
    """Validate ``rows`` shape and enforce the row cap (invariant #6)."""
    if not isinstance(rows, list):
        raise ScratchWriteError(
            "'rows' must be an array of arrays.", code="SCRATCH_MATERIALIZE_REJECTED"
        )
    if len(rows) > max_rows:
        raise ScratchTooLargeError(
            f"Result has {len(rows)} rows, over the {max_rows}-row scratch cap."
        )
    for row in rows:
        if not isinstance(row, list) or len(row) != n_cols:
            raise ScratchWriteError(
                "Every row must be an array whose length matches 'columns'.",
                code="SCRATCH_MATERIALIZE_REJECTED",
            )
    return rows


def _coerce_row(row: list[Any], columns: list[dict[str, str]]) -> list[Any]:
    """Coerce native JSON cells to driver-ready values.

    Non-string cells (int/float/bool/None) are passed through as-is — they are
    already native and go to ``client.insert`` as data.  A STRING cell is run
    through the hardened ``ingest_primitives.coerce`` for its declared type (so an ISO
    string lands as a real Date/DateTime, a numeric string as an int/float).  A
    String-typed cell round-trips unchanged — so a cell like ``'; DROP TABLE …``
    is stored as the literal string and, when later JOINed, compared as data,
    never parsed as SQL.
    """
    out: list[Any] = []
    for cell, col in zip(row, columns):
        if isinstance(cell, str):
            out.append(coerce(cell, col["type"]))
        else:
            out.append(cell)
    return out


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

# Characters that would break out of a single-quoted SQL literal (the cluster
# name, Keeper path, and replica name are interpolated into `ON CLUSTER '...'` and
# the ReplicatedMergeTree('...','...') engine args).  These values come from
# SERVER config (never the request), but we still reject quote/backslash/backtick/
# control chars defensively before interpolating — the same fail-closed posture as
# the identifier whitelist.
_UNSAFE_LITERAL_RE = re.compile(r"""['"`\\\x00-\x1f]""")


def _validate_cluster_literal(value: str, what: str) -> str:
    """Return *value* if it is safe to interpolate into a single-quoted DDL literal.

    Cluster names, Keeper paths, and replica names legitimately contain ``/``,
    ``{``/``}`` (ClickHouse macros such as ``{shard}``/``{replica}``), letters,
    digits, ``-`` and ``_`` — which ``validate_identifier`` would reject — so this
    uses a denylist of literal-breaking characters rather than the identifier
    whitelist.  A hostile value is refused, never smuggled into the DDL.
    """
    if not isinstance(value, str) or not value:
        raise ScratchWriteError(
            f"Cluster {what} must be a non-empty string.",
            code="SCRATCH_CLUSTER_MISCONFIGURED",
            status_code=500,
        )
    if _UNSAFE_LITERAL_RE.search(value):
        raise ScratchWriteError(
            f"Cluster {what} contains an unsafe character.",
            code="SCRATCH_CLUSTER_MISCONFIGURED",
            status_code=500,
        )
    return value


def build_scratch_create_sql(
    database: str,
    table: str,
    columns: list[dict[str, str]],
    ttl_seconds: int,
    *,
    cluster: str = "",
    replica_path_prefix: str = "/clickhouse/tables/{shard}",
    replica_name: str = "{replica}",
) -> str:
    """Return the CREATE TABLE DDL for a session-scoped scratch table with a TTL.

    All identifiers/types are re-validated here (belt-and-suspenders — the caller
    already validated).  A hidden ``_scratch_created_at DateTime DEFAULT now()``
    column carries the wall-clock TTL so orphaned tables GC themselves.

    Single-node (default, ``cluster`` empty): a node-local plain ``MergeTree``.
    The create/insert and the later JOIN both hit the same endpoint, so a table
    that lives on one node is sufficient.

    Cluster mode (``cluster`` set): the table is created ``ON CLUSTER`` as a
    ``ReplicatedMergeTree`` so it exists on every node AND its rows replicate,
    letting the main read client resolve the JOIN on whichever node serves it —
    the multi-node fix.  The Keeper path is ``<replica_path_prefix>/<db>/<table>``
    (unique per table via the uuid table name, identical across a table's
    replicas) and the replica is named by *replica_name* (typically the
    ``{replica}`` macro).
    """
    db = validate_identifier(database)
    tbl = validate_identifier(table)
    col_defs = []
    for col in columns:
        col_name = validate_identifier(col["name"])
        col_type = validate_ch_type(col["type"])
        col_defs.append(f"    `{col_name}` {col_type}")
    col_defs.append(f"    `{_TTL_COLUMN}` DateTime DEFAULT now()")
    cols_sql = ",\n".join(col_defs)

    if cluster:
        cluster_lit = _validate_cluster_literal(cluster, "name")
        prefix = _validate_cluster_literal(replica_path_prefix, "replica path prefix")
        replica_lit = _validate_cluster_literal(replica_name, "replica name")
        on_cluster = f" ON CLUSTER '{cluster_lit}'"
        keeper_path = f"{prefix}/{db}/{tbl}"
        engine = f"ReplicatedMergeTree('{keeper_path}', '{replica_lit}')"
    else:
        on_cluster = ""
        engine = "MergeTree"

    return (
        f"CREATE TABLE `{db}`.`{tbl}`{on_cluster}\n"
        f"(\n{cols_sql}\n)\n"
        f"ENGINE = {engine}\n"
        f"ORDER BY tuple()\n"
        f"TTL `{_TTL_COLUMN}` + INTERVAL {int(ttl_seconds)} SECOND"
    )


def _ensure_scratch_db(client: Client, database: str) -> None:
    """Bootstrap the scratch database on first use (OQ-G).

    The privileged credential is GRANTed on ``scratch.*`` including CREATE
    DATABASE for the scratch schema; this is idempotent.
    """
    client.command(f"CREATE DATABASE IF NOT EXISTS `{validate_identifier(database)}`")


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def materialize(
    session_id: str,
    columns: Any,
    rows: Any,
    settings: Settings,
) -> dict[str, Any]:
    """Create a session-scoped scratch table, native-bulk-load the rows, return it.

    The table name derives ONLY from *session_id* (the D92-bound header value),
    never from the body.  Returns ``{"table": "scratch.s_<sid>_bp_<uuid>",
    "row_count": <int>}``.
    """
    table = scratch_table_name(session_id)
    validated_cols = _validate_columns(columns)
    validated_rows = _validate_rows(rows, len(validated_cols), settings.scratch_max_rows)

    client = build_scratch_client(settings)
    try:
        _ensure_scratch_db(client, settings.scratch_database)
        ddl = build_scratch_create_sql(
            settings.scratch_database,
            table,
            validated_cols,
            settings.scratch_ttl_seconds,
            cluster=settings.scratch_cluster,
            replica_path_prefix=settings.scratch_replica_path_prefix,
            replica_name=settings.scratch_replica_name,
        )
        client.command(ddl)

        col_names = [c["name"] for c in validated_cols]
        coerced = [_coerce_row(r, validated_cols) for r in validated_rows]
        # In cluster mode, block the INSERT until a majority of replicas have the
        # rows (insert_quorum='auto') so materialize() only returns once the data
        # is durable cluster-wide — this bounds the replication-lag window before
        # the downstream JOIN.  For a HARD read-your-writes guarantee the reader
        # must also run select_sequential_consistency=1 (a deployment-side profile
        # setting; see Settings.scratch_cluster).  Single-node mode passes no
        # settings and keeps the original fast, un-quorumed insert.
        insert_settings = (
            {"insert_quorum": "auto", "insert_quorum_parallel": 0}
            if settings.scratch_cluster
            else None
        )
        # Native bulk insert — rows are DATA, never SQL text.  Only the user
        # columns are named; the DEFAULT-valued TTL column is filled by ClickHouse.
        client.insert(
            table=table,
            data=coerced,
            column_names=col_names,
            database=settings.scratch_database,
            settings=insert_settings,
        )
        return {
            "table": f"{settings.scratch_database}.{table}",
            "row_count": len(validated_rows),
        }
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def drop(session_id: str, table: Any, settings: Settings) -> dict[str, Any]:
    """Best-effort drop of a scratch table the caller's session owns.

    Cross-session drops are rejected 403 SCRATCH_SESSION_VIOLATION.  The TTL is
    the real cleanup guarantee; this is courtesy GC on clean DAG completion.
    """
    bare = _bare_scratch_name(table, session_id)
    client = build_scratch_client(settings)
    try:
        # In cluster mode the table was created ON CLUSTER, so it must be dropped
        # ON CLUSTER too — a node-local DROP would leave the replicas (and their
        # Keeper metadata) behind.  Single-node mode drops the one local table.
        on_cluster = ""
        if settings.scratch_cluster:
            cluster_lit = _validate_cluster_literal(settings.scratch_cluster, "name")
            on_cluster = f" ON CLUSTER '{cluster_lit}'"
        client.command(
            f"DROP TABLE IF EXISTS `{validate_identifier(settings.scratch_database)}`.`{bare}`"
            f"{on_cluster}"
        )
        return {"dropped": True}
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
