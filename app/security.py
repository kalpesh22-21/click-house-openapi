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
   or DESC.  A caller whose SQL must additionally pass column-provenance
   extraction (any scoped caller) is held to the narrower
   ``_PROVENANCE_ALLOWED_PREFIXES`` — SELECT / WITH only — via
   :func:`statement_supports_provenance`, called from the service layer where
   the scope is known.
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
import unicodedata

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
# The NARROWER allowlist that applies whenever the caller's SQL must pass column
# provenance extraction (any scoped caller, plus any query touching the scratch
# database under the ADR-0002 gate — see app/service.py::_enforce_query_guardrails).
#
# WHY A SECOND LIST rather than shrinking _ALLOWED_PREFIXES: SHOW / DESCRIBE /
# EXPLAIN genuinely work — and are genuinely useful — on the UNSCOPED paths (REST
# admin/GPT Action and MCP stdio local-trust), where `current_scope is None` means
# the provenance extractor never runs and the statement goes straight to the
# executor.  Removing the prefixes outright would break those callers.
#
# WHY THEY CANNOT BE ALLOWED ONCE PROVENANCE RUNS: sqlglot parses SHOW and EXPLAIN
# as exp.Command and DESCRIBE / DESC as exp.Describe.  extract_column_provenance
# accepts only Select / Union / With / Subquery, so these die downstream as
# PARSE_FAILED_CLOSED whose message asks the caller to "simplify the SQL" — advice
# no rewrite can satisfy, because nothing turns SHOW TABLES into a SELECT.  The
# honest rejection is this one: immediate, and it names the allowed set.
#
# NOT FIXABLE by routing them past provenance: SHOW TABLES reveals table names and
# DESCRIBE reveals column names, which is exactly what column scope governs, so an
# exemption would be a scope hole rather than a feature.  Scoped callers get that
# metadata from the listTables / getTableSchema tools, which apply scope filtering.
# ---------------------------------------------------------------------------

_PROVENANCE_ALLOWED_PREFIXES = ("select", "with")


def statement_supports_provenance(sql: str) -> bool:
    """Return True if *sql* is a statement kind the provenance extractor can process.

    Expects SQL that has already been through :func:`validate_and_sanitize` (comments
    stripped, trailing semicolon removed), so the leading keyword is the first token.

    Non-raising by design: the caller decides the error shape, because only the
    caller knows whether provenance is actually about to run for this request.
    """
    return sql.strip().lower().startswith(_PROVENANCE_ALLOWED_PREFIXES)


# ---------------------------------------------------------------------------
# Denylist — dangerous keywords that must never appear in user-supplied SQL.
# We use \b word boundaries so "INSERT" is blocked but a column named
# "inserted_at" is not.
#
# TABLE FUNCTIONS (CRITICAL — data exfiltration / SSRF vectors):
#   Each entry uses the pattern  \b<root>\w*\s*\(  so that a denied ROOT
#   matches any identifier that starts with it and is immediately followed by
#   an optional run of word-characters and then an open parenthesis.  This
#   means a single root blocks all *Cluster / *S3 / *Secure variants without
#   requiring each variant to be listed explicitly.  Examples:
#     - "url"     blocks  url(  AND  urlCluster(
#     - "s3"      blocks  s3(   AND  s3Cluster(
#     - "hdfs"    blocks  hdfs( AND  hdfsCluster(
#     - "iceberg" blocks  icebergS3(  icebergLocal(  etc.
#     - "cluster" blocks  cluster(  AND  clusterAllReplicas(
#     - "remote"  blocks  remote(   AND  remoteSecure(
#   The \w* suffix does NOT match across a word boundary, so:
#     - "url_slug"  is still safe (followed by '_', not '(')
#     - "file_size" is still safe (followed by '_', not '(')
#   This replaces the previous per-name workaround where "clusterAllReplicas"
#   and "remoteSecure" had to be listed separately because \b blocked the
#   exact-name pattern from matching mid-identifier extensions.
#   Those names are retained in the list below as no-ops (harmless) rather
#   than removed, to keep the intent explicit.
#
# NOTE ON DoS GENERATORS (numbers, range, arrayJoin, generateRandom, etc.):
#   These are intentionally NOT blocked here to avoid false positives on
#   legitimate analytics queries.  They are handled by server-side execution
#   caps (max_rows_to_read, max_result_rows) applied in clickhouse_client.py.
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

# Table-function ROOT names that must be blocked when used as function calls.
# Pattern: \b<root>\w*\s*\( — the \w* suffix catches *Cluster/*S3/*Secure
# variants automatically without listing each one.  See the comment block
# above for a full explanation.
_DENIED_TABLE_FUNCTIONS: list[str] = [
    "url",
    "file",
    "remote",
    "remoteSecure",   # kept explicit; also covered by "remote\w*\("
    "s3",
    "s3Cluster",      # kept explicit; also covered by "s3\w*\("
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
    "clusterAllReplicas",  # kept explicit; also covered by "cluster\w*\("
    "gcs",
    "azureBlobStorage",
    "mongodb",
    "redis",
    # Additional dangerous table functions:
    # "merge" is critical — merge('system','.*') dumps system catalog
    # by hiding the 'system' database name inside a string literal, bypassing
    # the \bSYSTEM\b denylist entry (which only fires outside literals).
    # NOTE: aggregate combinators like quantileMerge( do NOT match because
    # "merge" there has no \b word boundary before it (it is mid-identifier).
    "merge",
    # fuzzJSON and fuzzQuery are listed by full name rather than a broad "fuzz"
    # root to avoid false positives on hypothetical scalar fuzzBits( etc.
    # The \w* suffix in \bfuzzJSON\w*\( still auto-covers any future fuzzJSONCluster(
    # variants; the explicit name is only to avoid matching a broad root.
    "fuzzJSON",
    "fuzzQuery",
    "view",
    "loop",
    # Cloud object-storage table functions — same SSRF/exfil class as s3/gcs.
    # The \w* suffix auto-covers ossCluster(, cosnCluster(, obsCluster( variants.
    "oss",   # Alibaba Cloud OSS
    "cosn",  # Tencent Cloud COS
    "obs",   # Huawei Cloud OBS
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

# Table-function patterns: \b<root>\w*\s*\(
# The \w* between the root name and \s*\( ensures that any identifier that
# STARTS WITH the root name (e.g. urlCluster, s3Cluster, hdfsCluster,
# icebergS3, deltaLakeCluster, azureBlobStorageCluster, clusterAllReplicas,
# remoteSecure, mergeTreeIndex) is caught by the same pattern as the base
# name (url, s3, hdfs, iceberg, deltaLake, azureBlobStorage, cluster,
# remote, merge).
# re.IGNORECASE is used so that URL(, Url(, etc. are also caught.
for _fn in _DENIED_TABLE_FUNCTIONS:
    _pat = re.compile(r"\b" + re.escape(_fn) + r"\w*\s*\(", re.IGNORECASE)
    _DENIED_PATTERNS.append(_pat)

# Defense-in-depth: block any user-supplied SETTINGS clause.  This pattern
# intentionally blocks the ENTIRE `SETTINGS key=value` clause regardless of
# which setting name appears — any user-controlled setting override is
# disallowed because it could bypass the readonly/row-limit caps set in
# clickhouse_client.py.  We match SETTINGS followed by the first identifier and
# '=' (whichever key appears first) rather than the bare word "settings" so
# that a column named "settings" in a normal query is NOT falsely rejected.
# Example allowed:  SELECT settings FROM t WHERE settings = 'x'
# Example blocked:  SELECT 1 SETTINGS readonly=0
# Example blocked:  SELECT 1 SETTINGS a=1, readonly=0  (first key triggers it)
_DENIED_PATTERNS.append(re.compile(r"\bSETTINGS\s+\w+\s*=", re.IGNORECASE))

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
    #
    #    Additionally, build a dedicated scan copy that collapses quoted
    #    identifiers: ClickHouse accepts backtick- and double-quote-quoted
    #    identifiers as function names (e.g. `url`( or "url"(), and the
    #    \b<root>\w*\s*\( patterns cannot cross a closing quote to reach the '('.
    #    By stripping backtick and double-quote characters from the scan copy,
    #    a quoted function name collapses to its bare form so the existing
    #    patterns catch it.  _mask_string_literals (single-quote literals) runs
    #    first above, so only identifier quotes remain here; we do NOT strip
    #    single quotes.
    #
    #    CRITICAL: denylist_scan is a throwaway copy used ONLY for matching.
    #    The SQL returned from this function is `stripped` (quotes intact) so
    #    legitimate backtick-quoted column identifiers execute correctly.
    # NFKC folds Unicode COMPATIBILITY forms (e.g. full-width 'ｕrl' U+FF55,
    # superscripts) to their ASCII equivalents so a denied function name cannot be
    # disguised in identifier position. NOTE: NFKC does NOT transliterate across
    # scripts, so a cross-script homoglyph such as Cyrillic 'у' (U+0443) is NOT
    # folded and still passes here — this is a known, accepted gap: ClickHouse's
    # function registry is ASCII-only, so such a name is an unknown function the
    # server rejects, and readonly=1 is the backstop (see the strict-xfail test).
    # Applied only to the throwaway scan copy; the returned SQL is unaffected.
    denylist_scan = unicodedata.normalize("NFKC", stripped_masked).replace("`", "").replace('"', "")
    for pattern in _DENIED_PATTERNS:
        m = pattern.search(denylist_scan)
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
