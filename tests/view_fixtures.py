"""Synthetic SQL for public provisioning tests; no business view definitions."""

TINY_VIEW = """-- tiny test view header (stripped before splitting)
-- second comment line
CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse.{{SHARD_CATALOG}};

CREATE VIEW dbpcm_datawarehouse.{{SHARD_CATALOG}}.mini AS
SELECT '{{PROC_CENTER}}' AS proc_center
WHERE split_part(CURRENT_USER, '|', 2) = 'c'
  AND EXISTS (SELECT 1 WHERE split_part(CURRENT_USER, '|', 3) = 'u');
"""
