"""Per-request authenticated principal and its context propagation.

The authenticated caller's identity (derived from a validated JWT) must reach
``app.clickhouse_client.execute_query`` so that per-tenant ClickHouse settings
can be injected — but neither the service layer nor execute_query take a user
argument, and both run on threadpool workers (sync FastAPI routes and the MCP
HTTP tool functions).  A :class:`contextvars.ContextVar` is the right tool: it
is thread/task-safe and propagates into the anyio threadpool, and it keeps the
MCP HTTP transport stateless (no server-side session store).

Set/reset discipline (load-bearing):
  - REST: an async-generator dependency sets the principal before ``yield`` and
    resets it (via the token) in a ``finally`` after the response.
  - MCP HTTP: a pure-ASGI middleware sets it before awaiting the downstream app
    and resets it in a ``finally``.
Both MUST reset with the token returned by ``set()`` so a principal can never
leak onto a pooled worker thread that later serves a different request.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Principal:
    """An authenticated caller derived from a verified JWT.

    Attributes:
        subject: The token 'sub' claim (stable user id), for logging/audit.
        claims:  The full set of *verified* JWT claims.  The per-tenant settings
                 mapping reads arbitrary claim names from here (e.g. 'user_name'),
                 so the raw claims must be carried through — never the raw token.
    """

    subject: str
    claims: Dict[str, Any] = field(default_factory=dict)


# None means "no authenticated principal" — e.g. internal schema/health queries
# that run outside any request, or the local MCP stdio transport.  execute_query
# treats that as "inject no tenant settings".
current_principal: contextvars.ContextVar[Optional[Principal]] = contextvars.ContextVar(
    "current_principal", default=None
)


def get_current_principal() -> Optional[Principal]:
    """Return the principal bound to the current context, or None."""
    return current_principal.get()
