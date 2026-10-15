#!/usr/bin/env python3
"""Warm the optional ITDBA routing cache; never create cluster catalogs.

Use --output with TRINO_ITDBA_CACHE_PATH, then restart workers to discard their
current process memo immediately (otherwise it expires at the configured TTL).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.shard_routing import (  # noqa: E402
    _fetch_itdba_rows, atomic_write_snapshot,
    normalize_itdba_rows, snapshot_for, ShardRegistryError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Saved instanceLocation JSON export")
    parser.add_argument("--output", type=Path, required=True, help="Runtime cache path")
    parser.add_argument("--url", default=os.environ.get("TRINO_ITDBA_URL", ""),
                        help="HTTPS endpoint (defaults to TRINO_ITDBA_URL)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--mysqldb", default="dbonline")
    parser.add_argument("--name", default="online")
    args = parser.parse_args()
    try:
        if args.timeout <= 0:
            raise ValueError("timeout must be positive")
        if args.input:
            payload = json.loads(args.input.read_text(encoding="utf-8"))
            rows = payload["data"]
        else:
            rows = _fetch_itdba_rows(args.url, args.timeout)
        entries = normalize_itdba_rows(rows, args.mysqldb, args.name)
        atomic_write_snapshot(args.output, snapshot_for(entries))
    except (OSError, ValueError, KeyError, TypeError, ShardRegistryError) as exc:
        print(f"error: registry refresh failed: failure_class={type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"Wrote {len(entries)} routing entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
