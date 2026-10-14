#!/usr/bin/env python3
"""Apply rendered reporting views to the Trino dev coordinator.

Reads the per-shard SQL rendered by scripts/setup_trino_catalogs.py (default
docker/trino/views/rendered/, walked recursively for *.sql; each file's parent
directory is the shard catalog and its stem the view name) and executes every
file's statements IN ORDER against the dev coordinator (default
http://127.0.0.1:18081 as user view_admin, unauthenticated — the compose dev
coordinator has no auth).

Rendered files carry exactly two statements (CREATE SCHEMA IF NOT EXISTS, then
CREATE VIEW); the leading `--` comment block is stripped and statements are
split on `;` at end-of-line. The views live in the memory-connector
`dbpcm_datawarehouse` catalog: RAM-only, so they VANISH on every coordinator
restart — re-run this script after every `docker compose -f
docker-compose.trino.yml up/restart`.
A bounded readiness loop (SELECT 1, --ready-timeout/--ready-interval) absorbs
slow coordinator starts.

Default STRICT CREATE (the rendered templates deliberately keep plain CREATE
VIEW so a pre-existing view is reviewed, not clobbered): a pre-existing view
is reported FAILED for that shard, other shards are still attempted, and the
exit code is non-zero. --or-replace rewrites the view statement to CREATE OR
REPLACE VIEW AT APPLY TIME only — the rendered files on disk, and the strict
template, are untouched. --dry-run prints the planned statements per shard
without connecting at all.

Nothing sensitive is printed: statements surface only as one-line,
width-bounded summaries and errors as exception-type + first line (truncated).
The script never reads env files or credentials and connects without auth.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import trino.dbapi

ROOT = Path(__file__).resolve().parents[1]

# ERROR_MESSAGES_TRUNCATE bounds every error string we print: connector errors
# can echo SQL and server-internal detail the operator does not need.
_ERROR_TRUNCATE = 200
_SUMMARY_TRUNCATE = 100


class ViewApplyError(RuntimeError):
    """Coordinator never became ready / apply-time environment failure."""


def split_statements(sql_text: str) -> list[str]:
    """Strip leading `--`/blank comment block; split on `;` at end-of-line.

    The rendered-view contract is exactly two statements (CREATE SCHEMA,
    CREATE VIEW); inline `--` comments inside a statement are left in place
    (Trino accepts them). A `;` inside a string literal at end-of-line would
    split incorrectly — the templates contain none, and the rendered files are
    generated, never hand-edited.
    """
    lines = sql_text.splitlines()
    start = 0
    while start < len(lines):
        stripped = lines[start].strip()
        if stripped and not stripped.startswith("--"):
            break
        start += 1
    body = "\n".join(lines[start:])
    return [
        chunk.strip()
        for chunk in re.split(r";[ \t]*$", body, flags=re.MULTILINE)
        if chunk.strip()
    ]


def _summarize(statement: str) -> str:
    """One-line, width-bounded statement summary for status output."""
    flat = " ".join(statement.split())
    if len(flat) > _SUMMARY_TRUNCATE:
        return flat[: _SUMMARY_TRUNCATE - 3] + "..."
    return flat


def _error_summary(exc: Exception) -> str:
    """Connector messages may echo proprietary DDL or credentials."""
    return type(exc).__name__


_OR_REPLACE_PATTERN = re.compile(r"\bCREATE\s+VIEW\b")


def apply_or_replace(statement: str) -> str:
    """Rewrite the leading DDL to CREATE OR REPLACE VIEW (apply-time only).

    `count=1` keeps any 'create view' text inside the view body untouched; the
    rendered files on disk and the strict template are never modified.
    """
    return _OR_REPLACE_PATTERN.sub("CREATE OR REPLACE VIEW", statement, count=1)


def collect_plans(rendered_dir: Path) -> dict[str, dict[str, list[str]]]:
    """Map shard -> {view: [statements]} by walking rendered_dir for *.sql."""
    plans: dict[str, dict[str, list[str]]] = {}
    for path in sorted(rendered_dir.rglob("*.sql")):
        shard = path.parent.name
        plans.setdefault(shard, {})[path.stem] = split_statements(
            path.read_text(encoding="utf-8")
        )
    return plans


def wait_ready(
    *,
    host: str,
    port: int,
    user: str,
    http_scheme: str,
    timeout: float,
    interval: float,
):
    """Poll SELECT 1 until the coordinator answers; return the proven connection.

    The successful probe connection is returned and reused for the apply, so a
    coordinator that dies between probe and apply cannot pass the gate. A probe
    whose connect() succeeded but whose execute/fetch/close raised is closed
    before the next retry — no half-open sessions leak across the bounded loop.
    Raises ViewApplyError with a clear remediation hint when the timeout expires.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        conn = None
        try:
            conn = trino.dbapi.connect(
                host=host, port=port, user=user, http_scheme=http_scheme
            )
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            return conn
        except Exception as exc:  # noqa: BLE001 — any probe failure retries
            if conn is not None:
                # connect() succeeded but execute/fetch/close raised: close the
                # half-open connection before retrying, or the loop leaks one
                # coordinator-side session per attempt.
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 — cleanup never wins
                    pass
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ViewApplyError(
                    f"Trino coordinator not ready at {http_scheme}://{host}:{port} "
                    f"after {timeout:g}s "
                    f"(last probe: {type(last_error).__name__}). Is the dev stack "
                    "up? `docker compose -f docker-compose.trino.yml up -d`"
                ) from last_error
            time.sleep(min(interval, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--rendered-dir",
        type=Path,
        default=ROOT / "docker/trino/views/rendered",
        help="Root of rendered view SQL, walked recursively for *.sql; each "
        "file's parent directory is the shard catalog, its stem the view name.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument(
        "--user",
        default="view_admin",
        help="View owner identity; the dev coordinator is unauthenticated.",
    )
    parser.add_argument("--http-scheme", choices=("http", "https"), default="http")
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=60.0,
        help="Seconds to keep polling SELECT 1 before failing (default 60).",
    )
    parser.add_argument(
        "--ready-interval",
        type=float,
        default=2.0,
        help="Seconds between readiness probes (default 2).",
    )
    parser.add_argument(
        "--or-replace",
        action="store_true",
        help="Rewrite CREATE VIEW to CREATE OR REPLACE VIEW at apply time; the "
        "rendered files and the strict template are untouched.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned statements per shard without connecting.",
    )
    args = parser.parse_args(argv)

    if not args.rendered_dir.is_dir():
        print(
            f"error: rendered-view directory not found: {args.rendered_dir}\n"
            "Run scripts/setup_trino_catalogs.py first (it renders "
            "docker/trino/views/rendered/ from the *.txt templates).",
            file=sys.stderr,
        )
        return 1
    plans = collect_plans(args.rendered_dir)
    if not plans:
        print(
            f"error: no rendered views (*.sql) under {args.rendered_dir}\n"
            "Run scripts/setup_trino_catalogs.py first.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        for shard, views in plans.items():
            for view, statements in views.items():
                print(f"skipped {shard}/{view} (dry-run)")
                for statement in statements:
                    if args.or_replace:
                        statement = apply_or_replace(statement)
                    print(f"    would execute: {_summarize(statement)}")
        return 0

    try:
        conn = wait_ready(
            host=args.host,
            port=args.port,
            user=args.user,
            http_scheme=args.http_scheme,
            timeout=args.ready_timeout,
            interval=args.ready_interval,
        )
    except ViewApplyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    failures = 0
    try:
        for shard, views in plans.items():
            for view, statements in views.items():
                failed: str | None = None
                if not statements:
                    failed = "no statements in rendered file"
                for statement in statements:
                    if failed is not None:
                        break
                    if args.or_replace:
                        statement = apply_or_replace(statement)
                    cursor = conn.cursor()
                    try:
                        cursor.execute(statement)
                    except Exception as exc:  # noqa: BLE001 — per-shard status
                        failed = _error_summary(exc)
                    finally:
                        try:
                            cursor.close()
                        except Exception:  # noqa: BLE001 — cleanup never wins
                            pass
                if failed is None:
                    print(f"created {shard}/{view}")
                else:
                    failures += 1
                    print(f"FAILED  {shard}/{view}: {failed}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — cleanup never wins
            pass

    # Count views, not the (shard, views) tuples plans.items() yields —
    # len() over items() returned 2 per shard ("2/2 view(s)" for one view).
    total = sum(len(views) for views in plans.values())
    print(f"done: {total - failures}/{total} view(s) applied", file=sys.stderr)
    if failures:
        print(
            "Strict CREATE is in force: a FAILED shard usually means the view "
            "already exists. Review it; re-run with --or-replace to replace "
            "deliberately.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())