"""SQL guardrails — validate that a query is safe before sending it to ClickHouse.

Defense-in-depth strategy:
1. Strip SQL comments so they cannot smuggle keywords.
   - Strip single-line (--) comments first.
   - Iteratively strip innermost /* */ block comments until stable.
   - Reject if any /* or */ remains (unterminated or nested comments).
2. Strip single-quoted string literals before the semicolon/multi-statement
   scan so a semicolon inside a literal (e.g. 'v1.0; beta') does not cause a
   false rejection.
3. Reject multi-statement input (more than one statement separated by ';').
4. Allowlist: statement must start with SELECT, WITH, EXPLAIN, SHOW, DESCRIBE,
   or DESC.
5. Denylist: reject if any dangerous keyword or table-function name appears at
   a word boundary.
6. Auto-inject LIMIT when the outer SELECT has no LIMIT clause.

These checks are supplemented by ClickHouse-side settings (readonly=1, etc.)
applied on every query inside clickhouse_client.py — neither layer alone is
sufficient.

NOTE: LIMIT detection is global rather than outer-query-aware.  A LIMIT inside
a subquery suppresses injection of an outer LIMIT.  The ClickHouse-side
max_result_rows cap is the authoritative backstop for this edge case; it is
applied on every code path via readonly_settings() in clickhouse_client.py.
Do not rely solely on the injected LIMIT to bound result sizes.
"""

from __future__ import annotations

import re

from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Comment stripping
# ---------------------------------------------------------------------------

_SINGLE_LINE_COMMENT = re.compile(r"--[^\n]*")
# Matches the innermost (non-nested) /* ... */ block — no /* or */ inside.
_INNERMOST_BLOCK_COMMENT = re.compile(r"/\*[^/*]*\*/", re.DOTALL)


def _strip_comments(sql: str) -> str:
    """Remove SQL single-line (--) and multi-line (/* */) comments.

    Algorithm:
      1. Remove all -- … (to end-of-line) comments in one pass.
      2. Iteratively remove the innermost /* … */ block (no nested /* or */
         inside) until the string stabilises.  This correctly handles both
         nested comments and the common obfuscation pattern:
           SELECT 1 /* x /* y */ FROM evil() -- */
      3. If any /* or */ token remains after the loop the input contains
         unterminated or pathologically nested comments — reject with 400.

    This function is called BEFORE both the allowlist and denylist so that
    comment-hidden keywords are always exposed to both checks.
    """
    # Step 1: single-line comments
    sql = _SINGLE_LINE_COMMENT.sub(" ", sql)

    # Step 2: iteratively strip innermost /* */ until stable
    while True:
        stripped = _INNERMOST_BLOCK_COMMENT.sub(" ", sql)
        if stripped == sql:
            break
        sql = stripped

    # Step 3: reject if any /* or */ remains (unterminated / still-nested)
    if "/*" in sql or "*/" in sql:
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    "Query contains an unterminated or nested SQL comment (/* … */). "
                    "Only complete, non-nested comments are allowed."
                ),
                "code": "INVALID_COMMENT",
            },
        )

    return sql


# ---------------------------------------------------------------------------
# String-literal masking for structural checks AND denylist checks
#
# Before performing the semicolon / multi-statement check AND the denylist
# check we replace the *content* of every single-quoted string literal with
# empty quotes so that keywords or function names appearing only inside a
# string value cannot trigger false rejections.
#
# We handle:
#   'normal string'         → ''
#   'escaped '' quote'      → ''  (SQL standard doubling)
#   'backslash\' quote'     → ''  (backslash escaping)
#
# The denylist now operates on the masked form so that, for example:
#   SELECT * FROM logs WHERE message = 'calling url(endpoint)'
# is accepted (url( appears only inside a string literal), while:
#   SELECT * FROM url('http://evil/', 'CSV', 'x String')
# is still rejected (url( appears in executable position, outside any literal).
# ---------------------------------------------------------------------------

# Matches a single-quoted SQL string, handling '' and \' escapes.
_SINGLE_QUOTED_STRING = re.compile(r"'(?:[^'\\]|\\.|\\'|'')*'")


def _mask_string_literals(sql: str) -> str:
    """Replace the content of every single-quoted string with empty quotes.

    Returns a structurally equivalent string safe for semicolon scanning.
    Example: "WHERE note = 'v1.0; beta'" → "WHERE note = ''"
    """
    return _SINGLE_QUOTED_STRING.sub("''", sql)


# ---------------------------------------------------------------------------
# Allowlist of statement-opening keywords
# ---------------------------------------------------------------------------

_ALLOWED_PREFIXES = ("select", "with", "explain", "show", "describe", "desc")

# ---------------------------------------------------------------------------
# Denylist — dangerous keywords that must never appear in user-supplied SQL.
# We use \b word boundaries so "INSERT" is blocked but a column named
# "inserted_at" is not.
#
# TABLE FUNCTIONS (CRITICAL — data exfiltration / SSRF vectors):
#   Each entry uses the pattern  \b<name>\s*\(  so that:
#     - "url('...')"  is blocked
#     - "url_slug"    is NOT blocked (no following parenthesis)
#     - "file_size"   is NOT blocked (no following parenthesis)
#   The function call pattern inherently avoids false positives on column/table
#   names that contain the function name as a substring with a non-'(' suffix.
# ---------------------------------------------------------------------------

_DENIED_KEYWORDS: list[str] = [
    "INSERT",
    "ALTER",
    "DROP",
    "CREATE",
    "TRUNCATE",
    "RENAME",
    "ATTACH",
    "DETACH",
    "OPTIMIZE",
    "GRANT",
    "REVOKE",
    "KILL",
    "SYSTEM",
    "DELETE",
    "UPDATE",
    # Block client-controlled FORMAT (could exfiltrate data in unexpected shapes)
    "INTO OUTFILE",
    "FORMAT",
    # SET could change session settings and bypass readonly
    r"\bSET\b",
]

# Table-function names that must be blocked when used as function calls.
# Pattern: \b<name>\s*\( — matches the call syntax but not identifiers like
# url_slug (no parenthesis) or file_size (no parenthesis).
_DENIED_TABLE_FUNCTIONS: list[str] = [
    "url",
    "file",
    "remote",
    "remoteSecure",
    "s3",
    "s3Cluster",
    "mysql",
    "postgresql",
    "sqlite",
    "jdbc",
    "odbc",
    "hdfs",
    "hive",
    "deltaLake",
    "iceberg",
    "hudi",
    "input",
    "executable",
    "cluster",
    # Additional external-network / external-DB table functions.
    # Note: "clusterAllReplicas" must be listed explicitly — the existing
    # "cluster" entry uses \b word-boundary matching which does NOT fire
    # mid-identifier, so "clusterAllReplicas(" would slip past it.
    "gcs",
    "azureBlobStorage",
    "clusterAllReplicas",
    "mongodb",
    "redis",
]

# Pre-compile each keyword pattern once.  We include \b on both sides unless
# the keyword phrase already contains spaces (multi-word phrases use the phrase
# itself as a natural boundary).
_DENIED_PATTERNS: list[re.Pattern[str]] = []
for _kw in _DENIED_KEYWORDS:
    if " " in _kw:
        # Multi-word: treat spaces as \s+ for robustness
        _pat = re.compile(r"\b" + r"\s+".join(_kw.split()) + r"\b", re.IGNORECASE)
    elif _kw.startswith(r"\b"):
        # Already has boundary markers (SET special case)
        _pat = re.compile(_kw, re.IGNORECASE)
    else:
        _pat = re.compile(r"\b" + re.escape(_kw) + r"\b", re.IGNORECASE)
    _DENIED_PATTERNS.append(_pat)

# Table-function patterns: \b<name>\s*\(
# re.IGNORECASE is intentionally NOT set here for case-sensitive function names
# like remoteSecure, s3Cluster, deltaLake, etc.  We add both the canonical and
# lowercase variants explicitly via re.IGNORECASE to catch all casings of the
# common ASCII-only names while still matching mixed-case ClickHouse builtins.
for _fn in _DENIED_TABLE_FUNCTIONS:
    _pat = re.compile(r"\b" + re.escape(_fn) + r"\s*\(", re.IGNORECASE)
    _DENIED_PATTERNS.append(_pat)

# ---------------------------------------------------------------------------
# LIMIT detection — simple heuristic for the *outermost* query
#
# NOTE: This uses a global search, not an outer-query-aware parse.  A LIMIT
# present only in a subquery will suppress injection of an outer LIMIT.  The
# ClickHouse-side max_result_rows setting (applied in readonly_settings()) is
# the authoritative cap for this edge case.  Do not rely on this heuristic as
# the sole bound on result sizes.
# ---------------------------------------------------------------------------

_LIMIT_RE = re.compile(r"\bLIMIT\b", re.IGNORECASE)


def _has_limit(sql: str) -> bool:
    """Return True if the SQL already contains a LIMIT clause (anywhere)."""
    return bool(_LIMIT_RE.search(sql))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_and_sanitize(sql: str, default_limit: int) -> str:
    """Validate *sql* and return a safe version ready to send to ClickHouse.

    Raises HTTPException(400) with a descriptive message on any violation so
    that the calling LLM can self-correct.

    Steps:
      1. Strip comments (single-line then iterative block stripping; reject if
         unterminated/nested comment markers remain).
      2. Check for multiple statements (after masking string literals so a
         semicolon inside a quoted value is not misread as a separator).
      3. Allowlist check.
      4. Denylist check (including table-function names).
      5. Inject LIMIT if absent.

    Returns the (possibly modified) SQL string.
    """
    if not sql or not sql.strip():
        raise HTTPException(status_code=400, detail={
            "error": "SQL statement is empty.",
            "code": "EMPTY_QUERY",
        })

    # 1. Strip comments before any keyword analysis.
    #    _strip_comments raises HTTPException(400) on unterminated/nested comments.
    clean = _strip_comments(sql)

    # 2. Multi-statement check.
    #    First mask string literals so a semicolon inside 'v1.0; beta' is not
    #    treated as a statement separator.  We work on `clean` (comment-stripped)
    #    so that a comment-hidden semicolon is already gone.
    masked = _mask_string_literals(clean)
    stripped_masked = masked.rstrip()
    if stripped_masked.endswith(";"):
        stripped_masked = stripped_masked[:-1]

    if ";" in stripped_masked:
        raise HTTPException(status_code=400, detail={
            "error": (
                "Only a single SQL statement is allowed. "
                "Remove the semicolon that separates multiple statements."
            ),
            "code": "MULTIPLE_STATEMENTS",
        })

    # Build the de-commented, trailing-semicolon-stripped version of the
    # *original* (unmasked) SQL for allowlist/denylist/LIMIT checks.
    stripped = clean.rstrip()
    if stripped.endswith(";"):
        stripped = stripped[:-1]

    # 3. Allowlist check.
    normalised = stripped.strip().lower()
    if not any(normalised.startswith(prefix) for prefix in _ALLOWED_PREFIXES):
        raise HTTPException(status_code=400, detail={
            "error": (
                f"Statement must begin with one of: "
                f"{', '.join(p.upper() for p in _ALLOWED_PREFIXES)}. "
                f"Received: '{stripped.split()[0] if stripped.split() else ''}'"
            ),
            "code": "DISALLOWED_STATEMENT_TYPE",
        })

    # 4. Denylist check.
    #    Operate on the masked (string-literal-blanked) form so that function
    #    names or keywords appearing only inside a quoted string value do NOT
    #    trigger false rejections.  Real function calls in executable position
    #    (outside any literal) are still caught because only the literal
    #    *contents* are blanked — the function-name token itself remains.
    for pattern in _DENIED_PATTERNS:
        m = pattern.search(stripped_masked)
        if m:
            raise HTTPException(status_code=400, detail={
                "error": (
                    f"Query contains a disallowed keyword or table function: '{m.group()}'. "
                    "Only read-only queries without network/file/external-database access "
                    "are permitted."
                ),
                "code": "DISALLOWED_KEYWORD",
            })

    # 5. Auto-inject LIMIT on SELECT queries that have none.
    #    We only do this for SELECT / WITH — SHOW / DESCRIBE / EXPLAIN manage
    #    their own output sizes.
    if normalised.startswith(("select", "with")) and not _has_limit(stripped):
        stripped = f"{stripped} LIMIT {default_limit}"

    return stripped
