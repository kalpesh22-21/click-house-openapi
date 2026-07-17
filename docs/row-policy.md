CREATE TABLE security.user_employee_access (
    JTI String, EmployeeCode String, pull_id UInt64,
    UpdatedAt DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(pull_id)
ORDER BY (JTI, EmployeeCode)
TTL UpdatedAt + INTERVAL 1 DAY;

CREATE ROW POLICY employee_rls ON dbpcm_warehouse.employee
USING ClientCode = getSetting('SQL_CLIENTCODE')
  AND ProcCenter = getSetting('SQL_PROCCENTER')
  AND EmployeeCode IN (
      SELECT EmployeeCode FROM security.user_employee_access
      WHERE JTI = getSetting('SQL_TENANT')
        AND pull_id = (SELECT max(pull_id) FROM security.user_employee_access
                       WHERE JTI = getSetting('SQL_TENANT')))
TO mcp_user;