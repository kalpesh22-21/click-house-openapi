#!/usr/bin/env python3
"""Apply the native MySQL reporting views to a QA2 shard (dbonline).

Reads the concrete DDL assets under --views-dir (default
docker/trino/mysql/views/, top-level *.sql only) and executes each file's
statements IN ORDER against the shard given by --host/--port. The DDL inside
each file names its own qualified view (dbonline.iwant_reporting_*); the file
stem is used for status display only. The files are template-free (no
placeholders) and shared verbatim across shards — run once per shard with
different --host/--port.

Credentials come from --env-file (default the repo-root .env) keys
MYSQL_RW_USER / MYSQL_RW_PASSWORD, read by DIRECT FILE PARSE inside this
process. They are never printed, logged, placed on a command line or written
anywhere outside the env file. Connection discipline:
  * TLS is REQUIRED whenever the account's server-side auth plugin switches to
    mysql_clear_password (LDAP-style accounts): the first connect attempt uses
    default auth; on the driver's plugin-switch rejection the retry uses
    auth_plugin=mysql_clear_password with certificate verification against the
    system trust store (or --ssl-ca). Cleartext credentials are NEVER sent
    over an unverified or unencrypted channel — there is deliberately no
    skip-verify flag.
  * The session default schema is ALWAYS --database (default dbonline): the
    view bodies intentionally reference unqualified table names, which MySQL
    resolves against the session default schema AT CREATE time.

Default STRICT CREATE (the committed assets deliberately keep plain CREATE
VIEW so a pre-existing view is reviewed, not clobbered): a pre-existing view
is reported FAILED for that file, other files still run, and the exit code is
non-zero. --or-replace rewrites the leading DDL to CREATE OR REPLACE VIEW AT
APPLY TIME only — the files on disk are untouched. --dry-run prints the planned
statements per file without connecting at all.

Dependency: mysql-connector-python (pip install --user mysql-connector-python)
— chosen over pymysql because its auth error reporting distinguishes the
mysql_clear_password plugin switch; nothing sensitive is printed: statements
surface only as one-line, width-bounded summaries and errors as exception-type
+ first line (truncated).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every surface we print is bounded: connector errors can echo SQL and
# server-internal detail the operator does not need.
_ERROR_TRUNCATE = 200
_SUMMARY_TRUNCATE = 100

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


class ViewApplyError(RuntimeError):
    """Shard never became reachable / apply-time environment failure."""


def load_mysql_credentials(env_file: Path) -> dict[str, str]:
    """Parse MYSQL_RW_USER / MYSQL_RW_PASSWORD from the env file directly.

    The pair is returned to the caller's connect() only; it is never printed,
    logged or persisted. A missing key is a hard failure naming the file, not
    the value.
    """
    if not env_file.is_file():
        raise SystemExit(f"error: env file not found: {env_file}")
    found: dict[str, str] = {}
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in ("MYSQL_RW_USER", "MYSQL_RW_PASSWORD"):
            found[key] = value.strip().strip('"').strip("'")
    missing = {"MYSQL_RW_USER", "MYSQL_RW_PASSWORD"} - found.keys()
    if missing:
        raise SystemExit(
            f"error: {sorted(missing)} not found in {env_file} (key names only; "
            "nothing echoed)"
        )
    return found


def split_statements(sql_text: str) -> list[str]:
    """Strip the leading `--`/blank comment block; split on `;` at end-of-line.

    The committed assets carry exactly one statement (CREATE VIEW) after the
    header comment block; inline `--` comments inside the statement are left
    in place (MySQL accepts them). A `;` inside a string literal at
    end-of-line would split incorrectly — the assets contain none.
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
    files on disk stay strict.
    """
    return _OR_REPLACE_PATTERN.sub("CREATE OR REPLACE VIEW", statement, count=1)


def collect_plans(views_dir: Path) -> dict[str, list[str]]:
    """Map view-display-name -> [statements] for each *.sql directly under
    views_dir. The stem must be a plain identifier; the DDL itself names the
    qualified view."""
    plans: dict[str, list[str]] = {}
    for path in sorted(views_dir.glob("*.sql")):
        if not _IDENTIFIER.match(path.stem):
            raise SystemExit(
                f"error: view file stem is not a plain identifier: {path.name}"
            )
        statements = split_statements(path.read_text(encoding="utf-8"))
        for stmt in statements:
            if not re.match(r"^CREATE\s+VIEW\s", stmt, flags=re.IGNORECASE):
                raise SystemExit(
                    f"error: {path.name}: only CREATE VIEW statements are "
                    "permitted by this script"
                )
        plans[path.stem] = statements
    return plans


def _connect_once(args, credentials):
    """Single connect attempt, default auth first, cleartext-over-TLS retry."""
    import mysql.connector

    common = dict(
        host=args.host,
        port=args.port,
        user=credentials["MYSQL_RW_USER"],
        password=credentials["MYSQL_RW_PASSWORD"],
        database=args.database,
        autocommit=True,
        connect_timeout=15,
    )
    try:
        return mysql.connector.connect(**common)
    except mysql.connector.Error as exc:
        # 2059 = server switched to an auth plugin not enabled client-side
        # (mysql_clear_password for LDAP-style accounts). Retry ONLY over an
        # encrypted, certificate-verified channel.
        if getattr(exc, "errno", None) != 2059:
            raise
        tls = dict(auth_plugin="mysql_clear_password")
        if args.ssl_ca:
            tls["ssl_ca"] = str(args.ssl_ca)
            tls["ssl_verify_cert"] = True
        return mysql.connector.connect(**common, **tls)


def wait_ready(args, credentials):
    """Poll SELECT 1 until the shard answers; return the proven connection.

    Mirrors scripts/apply_trino_views.py: bounded loop, half-open probes are
    closed before retry, and the successful probe connection is reused for the
    apply. Raises ViewApplyError with a remediation hint when the timeout
    expires.
    """
    deadline = time.monotonic() + args.ready_timeout
    last_error: Exception | None = None
    while True:
        conn = None
        try:
            conn = _connect_once(args, credentials)
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            return conn
        except Exception as exc:  # noqa: BLE001 — any probe failure retries
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 — cleanup never wins
                    pass
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ViewApplyError(
                    f"MySQL shard not ready at {args.host}:{args.port} after "
                    f"{args.ready_timeout:g}s (last probe: "
                    f"{type(last_error).__name__}: "
                    f"{str(last_error).splitlines()[0][:120]}). Check network "
                    "reachability and the MYSQL_RW_* credentials in the env "
                    "file (values are never echoed by this script)."
                ) from last_error
            time.sleep(min(args.ready_interval, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--views-dir",
        type=Path,
        default=ROOT / "docker/trino/mysql/views",
        help="Directory of concrete *.sql view DDL (one CREATE VIEW per file).",
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Shard host (e.g. HQ-QA2-LDB10). Required: no silent default shard.",
    )
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="Shard port (e.g. 33205). Required: no silent default shard.",
    )
    parser.add_argument(
        "--database",
        default="dbonline",
        help="Session default schema the views are created in and their "
        "unqualified table names resolve against (default dbonline).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
        help="Env file holding MYSQL_RW_USER / MYSQL_RW_PASSWORD (default: "
        "repo-root .env). Values are parsed in-process and never echoed.",
    )
    parser.add_argument(
        "--ssl-ca",
        type=Path,
        default=None,
        help="Optional CA bundle for the TLS-verified cleartext-plugin retry "
        "(default: system trust store).",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=15.0,
        help="Seconds to keep polling SELECT 1 before failing (default 15).",
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
        "files on disk stay strict.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned statements per file without connecting.",
    )
    args = parser.parse_args(argv)

    if not _IDENTIFIER.match(args.database):
        print(f"error: --database is not a plain identifier: {args.database!r}",
              file=sys.stderr)
        return 1
    if not args.views_dir.is_dir():
        print(
            f"error: view-DDL directory not found: {args.views_dir}",
            file=sys.stderr,
        )
        return 1
    plans = collect_plans(args.views_dir)
    if not plans:
        print(f"error: no view DDL (*.sql) under {args.views_dir}", file=sys.stderr)
        return 1

    if args.dry_run:
        for view, statements in plans.items():
            print(f"skipped {args.database}/{view} on {args.host}:{args.port} (dry-run)")
            for statement in statements:
                if args.or_replace:
                    statement = apply_or_replace(statement)
                print(f"    would execute: {_summarize(statement)}")
        return 0

    credentials = load_mysql_credentials(args.env_file)
    try:
        conn = wait_ready(args, credentials)
    except ViewApplyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    failures = 0
    try:
        for view, statements in plans.items():
            failed: str | None = None
            if not statements:
                failed = "no statements in DDL file"
            for statement in statements:
                if failed is not None:
                    break
                if args.or_replace:
                    statement = apply_or_replace(statement)
                cursor = conn.cursor()
                try:
                    cursor.execute(statement)
                except Exception as exc:  # noqa: BLE001 — per-view status line
                    failed = _error_summary(exc)
                finally:
                    try:
                        cursor.close()
                    except Exception:  # noqa: BLE001 — cleanup never wins
                        pass
            if failed is None:
                print(f"created {args.database}/{view}")
            else:
                failures += 1
                print(f"FAILED  {args.database}/{view}: {failed}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — cleanup never wins
            pass

    total = len(plans)
    print(f"done: {total - failures}/{total} view(s) applied", file=sys.stderr)
    if failures:
        print(
            "Strict CREATE is in force: a FAILED view usually means it already "
            "exists. Review it; re-run with --or-replace to replace "
            "deliberately.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())