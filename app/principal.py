"""Per-request authenticated principal and its context propagation.

The authenticated caller's identity (derived from a validated JWT) must reach
``app.clickhouse_client.execute_query`` so that per-tenant ClickHouse settings
can be injected — but neither the service layer nor execute_query take a user
argument.  A :class:`contextvars.ContextVar` is the right tool: it is
thread/task-safe and keeps the MCP HTTP transport stateless (no server-side
session store).

How the ContextVar reaches each call site:
  - REST routes (sync FastAPI handlers): the async-generator dependency sets the
    principal before ``yield``; anyio copies the context into the threadpool
    worker, so ``execute_query`` sees the value.
  - MCP HTTP tools (FastMCP sync tools): FastMCP calls sync tools **inline** in
    the async event-loop task — NOT via a threadpool — so the ContextVar set
    by the pure-ASGI middleware in the same task is directly visible.  No
    threadpool copy step is involved.

Set/reset discipline (load-bearing):
  - REST: an async-generator dependency sets the principal before ``yield`` and
    resets it (via the token) in a ``finally`` after the response.
  - MCP HTTP: a pure-ASGI middleware sets it before awaiting the downstream app
    and resets it in a ``finally``.
Both MUST reset with the token returned by ``set()`` so a principal can never
bleed across requests.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Principal:
    """An authenticated caller derived from a verified JWT.

    Attributes:
        subject:      The token 'sub' claim (stable user id), for logging/audit.
        claims:       The full set of *verified* JWT claims.  The per-tenant settings
                      mapping reads arbitrary claim names from here (e.g. 'user_name'),
                      so the raw claims must be carried through — never the raw token.
        column_scope: Frozenset of "database.table.column" strings the caller is
                      permitted to access.  Empty frozenset = ALLOW-ALL (unrestricted);
                      the caller may read any warehouse column.  The only signal that
                      enforcement is disabled entirely is ``current_scope is None``
                      (stdio/local-trust path).
        token:        The raw bearer JWT this principal was validated from.  Carried
                      ONLY so a first-party downstream call that must act AS the user
                      (the employee-access ``/cl/eeaccess`` fetch) can forward the very
                      same token.  SENSITIVE: never log it, never return it to a client,
                      never persist it.  Empty on the stdio/local-trust path.
    """

    subject: str
    claims: Dict[str, Any] = field(default_factory=dict)
    column_scope: frozenset = field(default_factory=frozenset)
    token: str = ""


# None means "no authenticated principal" — e.g. internal schema/health queries
# that run outside any request, or the local MCP stdio transport.  execute_query
# treats that as "inject no tenant settings".
current_principal: contextvars.ContextVar[Optional[Principal]] = contextvars.ContextVar(
    "current_principal", default=None
)

# None means "no scope enforcement" — e.g. stdio/local-trust transport.
# A non-None frozenset is the set of permitted "database.table.column" strings.
current_scope: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "current_scope", default=None
)

# None means "no session context" — e.g. stdio/local-trust transport.
# A non-None str is the caller's session identifier for scratch-table isolation.
current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_session_id", default=None
)


def get_current_principal() -> Optional[Principal]:
    """Return the principal bound to the current context, or None."""
    return current_principal.get()


def get_current_scope() -> Optional[frozenset]:
    """Return the column scope frozenset bound to the current context, or None.

    None means scope enforcement is disabled (stdio/local-trust path).
    A non-None frozenset is the set of permitted 'database.table.column' strings.
    """
    return current_scope.get()


def get_current_session_id() -> Optional[str]:
    """Return the session_id bound to the current context, or None.

    None means no session context (stdio/local-trust path).
    """
    return current_session_id.get()
