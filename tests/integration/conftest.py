"""Integration-test conftest: gating, seeding, and shared fixtures.

Gating logic
------------
Integration tests are skipped unless BOTH conditions hold:
  1. Environment variable RUN_INTEGRATION is "1" or "true" (case-insensitive).
  2. The REST /health endpoint at IT_API_BASE is reachable (short-timeout GET).

If the gate is closed the entire suite is *skipped* (not failed), so a plain
``pytest tests/`` run stays green and continues to report the 258 unit tests.

Ground-truth dataset
--------------------
A session-scoped ``seed`` fixture connects directly to ClickHouse
(clickhouse-connect, port 8123, default user, no password) and:
  - Creates database ``it_integration`` (drops and recreates for idempotency).
  - Creates table ``it_integration.orders`` with 6 rows whose aggregates are
    exact constants asserted throughout the suite:
        count       = 6
        sum(amount) = "605.50"
        max(amount) = "200.00"
        distinct countries = {"US", "GB", "DE"}
  - Creates table ``it_integration.customers`` (3 rows) for schema/list variety.
  - Yields a dict of ground-truth values.
  - Teardown: DROP DATABASE it_integration SYNC.
"""

from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Generator

import httpx
import pytest

# ---------------------------------------------------------------------------
# Configurable endpoints / credentials (env with verified defaults)
# ---------------------------------------------------------------------------

IT_API_BASE: str = os.environ.get("IT_API_BASE", "http://localhost:18080")
IT_MCP_URL: str = os.environ.get("IT_MCP_URL", "http://localhost:18090/mcp")
IT_MCP_HEALTH: str = os.environ.get("IT_MCP_HEALTH_URL", "http://localhost:18090/health")
IT_API_KEY: str = os.environ.get("IT_API_KEY", "test-key-abc123")
IT_CH_HOST: str = os.environ.get("IT_CH_HOST", "localhost")
IT_CH_PORT: int = int(os.environ.get("IT_CH_PORT", "8123"))

# ---------------------------------------------------------------------------
# Gating — skip integration tests unless env var set AND stack is reachable
# ---------------------------------------------------------------------------

_RUN_INTEGRATION_RAW = os.environ.get("RUN_INTEGRATION", "0").strip().lower()
_RUN_INTEGRATION: bool = _RUN_INTEGRATION_RAW in ("1", "true", "yes")


def _stack_reachable() -> tuple[bool, str]:
    """Return (reachable, reason_string)."""
    if not _RUN_INTEGRATION:
        return False, "RUN_INTEGRATION is not set to 1/true"
    try:
        resp = httpx.get(f"{IT_API_BASE}/health", timeout=3.0)
        if resp.status_code == 200:
            return True, ""
        return False, f"REST /health returned HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"stack not running: {exc}"


_STACK_OK, _SKIP_REASON = _stack_reachable()


def pytest_collection_modifyitems(
    items: list[pytest.Item],
    config: pytest.Config,
) -> None:
    """Add a skip marker to every integration-marked item when the gate is closed."""
    if _STACK_OK:
        return  # gate open — let tests run normally
    skip = pytest.mark.skip(reason=_SKIP_REASON or "integration gate closed")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip, append=False)


# ---------------------------------------------------------------------------
# anyio backend — used by async MCP tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Session-scoped seed fixture
# ---------------------------------------------------------------------------

#: Ground-truth constants for the it_integration.orders table (6 rows).
#:
#: order_id  customer    country  amount   created_at
#:    1       Alice       US       100.00   2026-01-01 10:00:00
#:    2       Bob         GB        50.00   2026-01-02 11:00:00
#:    3       Charlie     DE       200.00   2026-01-03 12:00:00
#:    4       Alice       US        75.50   2026-01-04 13:00:00
#:    5       Bob         GB        30.00   2026-01-05 14:00:00
#:    6       Diana       DE       150.00   2026-01-06 15:00:00
#:
#: Aggregates:
#:   count       = 6
#:   sum(amount) = 605.50
#:   max(amount) = 200.00
#:   distinct countries: US, GB, DE

GROUND_TRUTH: dict[str, Any] = {
    "database": "it_integration",
    "orders_table": "orders",
    "customers_table": "customers",
    "orders_count": 6,
    "orders_sum_amount": "605.50",
    "orders_max_amount": "200.00",
    "orders_countries": {"US", "GB", "DE"},
    # Exact column definitions for schema assertions (name, type)
    "orders_columns": [
        {"name": "order_id", "type": "UInt64"},
        {"name": "customer", "type": "LowCardinality(String)"},
        {"name": "country", "type": "LowCardinality(String)"},
        {"name": "amount", "type": "Decimal(10, 2)"},
        {"name": "created_at", "type": "DateTime"},
    ],
    "customers_columns": [
        {"name": "customer_id", "type": "UInt32"},
        {"name": "name", "type": "String"},
        {"name": "region", "type": "LowCardinality(String)"},
    ],
}


@pytest.fixture(scope="session")
def seed() -> Generator[dict[str, Any], None, None]:
    """Create the it_integration database and seed deterministic test data.

    Yields GROUND_TRUTH and tears down the database after the session.
    """
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=IT_CH_HOST,
        port=IT_CH_PORT,
        user="default",
        password="",
        secure=False,
    )

    # --- Setup ---
    # Drop and recreate to guarantee a clean state on every run.
    client.command("DROP DATABASE IF EXISTS it_integration SYNC")
    client.command("CREATE DATABASE it_integration")

    # orders table
    client.command("""
        CREATE TABLE it_integration.orders (
            order_id   UInt64,
            customer   LowCardinality(String),
            country    LowCardinality(String),
            amount     Decimal(10, 2),
            created_at DateTime
        ) ENGINE = MergeTree
        ORDER BY order_id
    """)

    client.command("""
        INSERT INTO it_integration.orders
            (order_id, customer, country, amount, created_at)
        VALUES
            (1, 'Alice',   'US', 100.00, '2026-01-01 10:00:00'),
            (2, 'Bob',     'GB',  50.00, '2026-01-02 11:00:00'),
            (3, 'Charlie', 'DE', 200.00, '2026-01-03 12:00:00'),
            (4, 'Alice',   'US',  75.50, '2026-01-04 13:00:00'),
            (5, 'Bob',     'GB',  30.00, '2026-01-05 14:00:00'),
            (6, 'Diana',   'DE', 150.00, '2026-01-06 15:00:00')
    """)

    # customers table (for listTables / schema variety)
    client.command("""
        CREATE TABLE it_integration.customers (
            customer_id UInt32,
            name        String,
            region      LowCardinality(String)
        ) ENGINE = MergeTree
        ORDER BY customer_id
    """)

    client.command("""
        INSERT INTO it_integration.customers
            (customer_id, name, region)
        VALUES
            (1, 'Alice',   'EMEA'),
            (2, 'Bob',     'APAC'),
            (3, 'Charlie', 'AMER')
    """)

    yield GROUND_TRUTH

    # --- Teardown ---
    try:
        client.command("DROP DATABASE IF EXISTS it_integration SYNC")
    except Exception:  # noqa: BLE001
        pass  # best-effort cleanup


# ---------------------------------------------------------------------------
# REST API client fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def api() -> httpx.Client:
    """An httpx.Client pointed at IT_API_BASE with the Bearer token pre-set."""
    with httpx.Client(
        base_url=IT_API_BASE,
        headers={"Authorization": f"Bearer {IT_API_KEY}"},
        timeout=10.0,
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# MCP session helper (async context manager factory used by individual tests)
# ---------------------------------------------------------------------------


async def mcp_call(tool: str, args: dict[str, Any]) -> Any:
    """Open a fresh MCP session, call one tool, return the CallToolResult.

    Used by individual async test functions so each test is isolated.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        IT_MCP_URL,
        headers={"Authorization": f"Bearer {IT_API_KEY}"},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, args)


def structured(res: Any) -> Any:
    """Extract the useful payload from a CallToolResult.

    - LIST-returning tools return structuredContent = {"result": [...]} — unwrap.
    - OBJECT-returning tools return structuredContent = {columns, rows, ...} directly.
    """
    sc = res.structuredContent
    if isinstance(sc, dict) and list(sc.keys()) == ["result"]:
        return sc["result"]
    return sc
