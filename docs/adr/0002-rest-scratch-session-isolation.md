# ADR 0002 — Close REST scratch-session isolation gap (H3): scratch-triggered fail-closed on every transport

Status: Accepted (implemented)
Date: 2026-08-06
Supersedes/relates: extends the D64 scratch-isolation gate; keeps ADR-0001 (per-tenant JWT row isolation) untouched.

> **Supersession note.** An earlier revision of this ADR proposed a
> *mandatory `X-Session-Id` header* design (bind the session on REST via
> `require_principal`, reject session-less tool requests with
> `400 SESSION_ID_REQUIRED`, and re-trigger enforcement on
> `scope is not None or session_id is not None`). **That design is superseded by
> the one below.** The implemented design does NOT bind a session on REST, does
> NOT add a mandatory-header check, and does NOT touch `app/auth.py`. Instead it
> triggers the existing fail-closed scratch gate whenever a query *references the
> scratch database*, on every transport. The original recon (Context) and the
> error-map landmine note are preserved because they remain accurate.

---

## Context

Scratch tables are session-scoped, named `scratch.s_<session_id>_bp_<uuid4hex>`. The
owning session is recovered from the table name and compared against the bound
`current_session_id` by `_validate_scratch_name` (app/sqlparse/provenance.py:288-341),
which fails closed on a session mismatch OR a None/empty session id.

**H3 (REST cross-session scratch read).** `require_principal` (app/auth.py) binds
ONLY `current_principal`; it sets neither `current_scope` nor
`current_session_id`. In app/service.py all scratch/scope enforcement lived inside
`if scope is not None:` — in `_enforce_query_guardrails` (shared by `run_query` and
`explain_query`) and in the inline copy in `sample_rows`. On REST,
`get_current_scope()` returns None, so that block never ran. A REST caller with ANY
valid JWT could therefore `SELECT * FROM scratch.s_<victim>_bp_...` and read another
session's scratch table. REST is a live, network-exposed surface (docker-compose
`ch-api`, published `18080:8000`, MODE=api → `uvicorn app.main:app`) and is the
documented ChatGPT Custom GPT Action (app/main.py:113).

**Why REST must simply be DENIED scratch, not "carry a session".** REST has no
session concept and does NOT create scratch tables — creation requires a
server-minted session via `/scratch/v1/*`, which only the BFF holds. A REST caller
can never legitimately own a scratch table, so the correct posture is: any
scratch reference on a scope-less/session-less transport fails closed. There is no
value in binding a forgeable session id onto REST.

---

## Decision

Trigger the existing fail-closed scratch gate on the presence of a **scratch
reference**, independent of scope — on every transport.

1. **Scratch-triggered, unconditional enforcement (app/service.py).** Both
   `_enforce_query_guardrails` and the `sample_rows` inline block compute:

   ```
   reject_cartesian_joins(clean_sql)            # unchanged, UNCONDITIONAL
   scope        = get_current_scope()
   session_id   = get_current_session_id()
   references_scratch = _references_scratch_db(clean_sql, settings.scratch_database)
   need_provenance = (scope is not None) or (require_session_scratch_gate and references_scratch)

   if need_provenance:
       catalog = get_catalog_schema()
       try:
           uses = extract_column_provenance(clean_sql, catalog, session_id=session_id)
       except ScratchSessionError:      -> ColumnScopeError(SCRATCH_SESSION_VIOLATION)
       except ProvenanceExtractionError -> ParseFailedError(PARSE_FAILED_CLOSED)
       if scope:                        # non-empty frozenset ONLY — column allowlist, unchanged
           forbidden = _forbidden_out_of_scope_columns(uses, scope)
           if forbidden: -> ColumnScopeError(COLUMN_SCOPE_VIOLATION)
   ```

   `_enforce_query_guardrails` gained a `settings: Settings` parameter (both callers
   already had it; it is now used to read `scratch_database` and the gate flag).

2. **Fail-closed-safe scratch pre-check `_references_scratch_db(sql, scratch_db)`.**
   A case-insensitive `\bscratch\b` (built from `re.escape(scratch_db)`) over a
   copy of the SQL in which identifier quote characters (`` ` `` and `"`) are
   replaced with **whitespace**. It is an intentional OVER-APPROXIMATION: it may
   return True spuriously (a column/alias literally named `scratch`), which only
   forces a safe provenance parse; it MUST never return False for a query that
   truly references the scratch DB. A false negative re-opens H3.

   > **Security-review finding (fixed).** The first implementation *deleted* the
   > quote characters (`replace("`", "")`) before the `\bscratch\b` search. That
   > FUSED the preceding keyword onto the identifier — `FROM"scratch"."s_victim…"`
   > collapsed to `FROMscratch…`, destroying the left word boundary, so
   > `\bscratch\b` did NOT match → the pre-check returned **False** → on the REST
   > path (`scope is None`) provenance never ran → the foreign scratch read
   > **EXECUTED**. Verified exploitable through both `run_query` and
   > `explain_query` via the no-space `` FROM`scratch`.`…` `` and `FROM"scratch"."…"`
   > forms. `sample_rows` builds its own leading-space SQL so was not exploitable,
   > but the check is fixed at the source rather than relying on that.
   >
   > **Fix:** replace each quote with a **space**, not empty string, so a left word
   > boundary is always preserved. A real scratch reference always has whitespace
   > OR a quote between `FROM`/`JOIN` and the identifier (`FROMscratch` with neither
   > is a single invalid identifier that cannot reference the DB), so substituting a
   > space guarantees the boundary and eliminates this entire false-negative class.
   >
   > The check is **not self-sufficient**: it relies on upstream
   > `validate_and_sanitize` for comment-stripping, multi-statement rejection, and
   > table-function denial — so comment-split tokens (`scr/**/atch`) or a table
   > function over the scratch DB (`merge('scratch', …)`) are handled by that layer
   > / by the provenance parse, not by this token scan.

3. **Config off-switch `require_session_scratch_gate: bool = True` (app/config.py).**
   Gates ONLY the new `gate_on and references_scratch` disjunct. When False,
   behavior reverts to the legacy `scope is not None` trigger exactly (staged
   rollout / emergency off-switch).

3a. **Single source of truth for the scratch DB name (config assertion).** The
   pre-check reads the configurable `settings.scratch_database`, but the D64
   ownership parser (`_validate_scratch_name` / `_build_alias_map` in
   app/sqlparse/provenance.py) uses the hardcoded module constant
   `_SCRATCH_DB = "scratch"`. If an operator set `SCRATCH_DATABASE=sandbox` the two
   would diverge: the pre-check would trip on `sandbox.*` while provenance treated
   `sandbox.*` as an uncatalogued warehouse table → `PARSE_FAILED_CLOSED` even for
   the OWNER's own legitimate scratch access (north-star broken), and the gate
   would stop recognising the real scratch DB. **Chosen fix: a fail-LOUD
   `model_validator` in `Settings`** (`_validate_scratch_db_single_source`) that
   raises at construction if `scratch_database != provenance._SCRATCH_DB`. Threading
   the name through `extract_column_provenance` → `_build_alias_map` →
   `_references_only_scratch_sources` → `_validate_scratch_name` was rejected for
   this slice: it changes 4-5 signatures in the D64-critical ownership chain plus
   every test that calls them positionally (invasive, higher regression risk). The
   assertion makes a divergent config impossible to boot — near-zero blast radius,
   same guarantee.

4. **Error-map landmine fix (app/main.py).** `_DOMAIN_ERROR_STATUS` gained
   `ColumnScopeError: 403` and `ParseFailedError: 400`. These never fired on REST
   before (scope was always None) so they were absent from the map and would have
   surfaced as HTTP 500 via the catch-all handler. The query router does not catch
   these two types, so they propagate to the base-class domain-error handler, which
   consults this map — 403 / 400 respectively. (`CartesianJoinError` was already
   mapped.)

### Invariants (verified by tests)

- **Scoped MCP path unchanged.** When `scope is not None`, `need_provenance` is True
  regardless of `references_scratch`, and the column-allowlist stays gated on the
  non-empty `scope`. No scoped-path test changed.
- **GPT Action unaffected.** `scope is None` + a normal warehouse query (no scratch
  reference) → `need_provenance` is False → NO provenance parse → no new
  PARSE_FAILED_CLOSED regression. The GPT keeps working.
- **H3 closed.** `scope is None` + a scratch reference (REST/stdio) → provenance
  runs; `session_id` is None → `_validate_scratch_name` fails closed →
  SCRATCH_SESSION_VIOLATION.
- No error CODES or messages changed. No session bound on REST. No mandatory-header
  check. `app/auth.py` untouched.

### Interim vs binding — stated honestly

This slice does NOT make session ids unforgeable. On a scoped transport a caller
who *learns* another live session id can still present it (the interim posture
trusts session-id secrecy). The cryptographic fix — flip `require_sid_binding` to
True once every minting path stamps `sid_hash` — stays on the roadmap and is
unchanged here. What this slice buys: REST/stdio can no longer read foreign (or
any) scratch when session-less, on every transport, without binding a session onto
a surface that has no legitimate scratch to own.

---

## Consequences

- **REST blast radius is minimal.** Only queries that *reference the scratch DB*
  newly trigger provenance on REST. A scratch reference by a session-less REST
  caller now returns **403 SCRATCH_SESSION_VIOLATION** instead of rows. Normal
  warehouse queries are untouched — no parse, no PARSE_FAILED_CLOSED. REST is NOT
  newly column-scoped (scope stays None, the `if scope:` allowlist stays off).
- **stdio/local-trust behavior CHANGES for scratch (deliberate, accepted).** The
  legacy stdio path (`scope is None`, `session_id is None`) skipped enforcement
  entirely, so a `scratch.*` reference executed. With the gate on (default), an
  stdio scratch reference now also fails closed (SCRATCH_SESSION_VIOLATION) because
  the gate is transport-agnostic. See "Resolved decision" below.
- **Error-map is load-bearing.** Without the app/main.py map entries a REST
  scratch-violation / parse-fail would 500. Now 403 / 400.

### Resolved decision — option (a): transport-agnostic fail-closed

The stdio/local-trust behavior change was surfaced during implementation (two
pre-existing stdio-parity tests asserted the OLD permissive semantics and failed)
and **resolved in favor of option (a): accept the transport-agnostic fail-closed
tightening.**

Rationale: **no production path uses stdio scratch, and stdio cannot CREATE scratch
tables** (creation requires a server-minted session via `/scratch/v1/*`, held only
by the BFF). A session-less scratch *read* on stdio therefore has no legitimate
use, and denying it matches the audit north-star — *only the creating session ever
sees its scratch table*. The transport-agnostic gate is the correct posture; the
narrower "network-exposed only" alternative was rejected because it would reopen a
session-less scratch read on any future scope-less-but-networked surface.

The two tests were updated to assert the NEW fail-closed behavior (the code was NOT
weakened to keep them green):

- `tests/test_scratch_isolation.py` — `TestD64ScratchNoSessionIdNoCheck` →
  `TestScratchNoSessionFailsClosedEvenScopeless`. The scope-less/session-less
  scratch read now asserts SCRATCH_SESSION_VIOLATION and that the catalog IS
  consulted. Companion tests pin (i) the off-switch reverting to skip-and-execute
  and (ii) the warehouse GPT-unaffected path still skipping provenance.
- `tests/test_explain_query_scope_adversarial.py` — `TestExplainNoScopeStdioParity`
  split into `TestExplainNoScopeWarehouseSkipsProvenance` (warehouse still skips +
  executes — the genuine GPT-unaffected parity) and
  `TestExplainNoScopeScratchFailsClosed` (scratch ref now fails closed, plus the
  off-switch revert).

`require_session_scratch_gate=False` remains the emergency off-switch that restores
the old stdio (and REST) permissive behavior wholesale, and is covered by exactly
one clearly-named test on each path.

### On the roadmap (unchanged by this slice)

- Flip `require_sid_binding` True once the minter stamps `sid_hash` — the real
  anti-spoofing fix for scoped transports.

---

## Files changed

| File | Change |
|---|---|
| `app/config.py` | Added `require_session_scratch_gate: bool = True`; added `_validate_scratch_db_single_source` fail-loud validator (item 3a). |
| `app/service.py` | Added `_references_scratch_db` (quote→space fix); added `settings` param to `_enforce_query_guardrails`; changed both enforcement blocks (guardrails + `sample_rows`) to the scratch-triggered `need_provenance` gate; updated both call sites. |
| `app/main.py` | Imported `ColumnScopeError`/`ParseFailedError`; added `ColumnScopeError: 403`, `ParseFailedError: 400` to `_DOMAIN_ERROR_STATUS`. |
| `app/security.py` | **No change (reverted).** The identical delete-the-quote pattern at the denylist scan (line 384) was tried with the replace-with-space fix, but it BROKE `test_split_backtick_evasion_blocked`: that scan INTENTIONALLY fuses intra-identifier split quotes (`` u`r`l( `` → `url(`) to catch quote-splitting function-name evasion, so replace-with-space (`u r l`) reopens that evasion. The two call sites have OPPOSITE requirements — the scratch pre-check must preserve boundaries, the denylist must collapse them — so the security scan was left as-is. Reported, not pushed through. |
| `tests/test_rest_scratch_gate.py` | pre-check unit tests (incl. no-space quoted forms), REST-shape H3 adversarial (run/sample/explain, incl. no-space double-quote/backtick/JOIN forms), GPT-unaffected, flag-off, error-map. |
| `tests/test_explain_query_scope_adversarial.py` | Added `TestExplainRestScratchNoSpaceBypassFailsClosed` (no-space quoted forms via explain, scope=None). |

## Helper signatures

```python
def _references_scratch_db(sql: str, scratch_db: str) -> bool: ...

def _enforce_query_guardrails(
    clean_sql: str,
    settings: Settings,
    caller: str = "query_guardrails",
) -> None: ...
```
