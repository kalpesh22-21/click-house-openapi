# Clickhouse-API Blueprint Collection

**Total:** 193 blueprints
**Generated:** 2026-10-28

## Table of Contents

- [Accruals (8)](#accruals)
- [Compensation (17)](#compensation)
- [Employee (26)](#employee)
- [Expenses (18)](#expenses)
- [Headcount (23)](#headcount)
- [Payroll (49)](#payroll)
- [Punches (17)](#punches)
- [Time-off (15)](#time-off)
- [Workforce (20)](#workforce)

## Accruals

### Blueprint: bp-accrual-balance-summary

**ID:** `bp-accrual-balance-summary`

**Intent:** Recorded beginning, accrued, awarded and taken plan balances by company, department or plan. Optional accrual plan code, employee status, department and employee. Uses taken_amount, never approved event hours. No implicit PTO filter: resolve plan code for PTO. No event-date restriction, current-balance formula or assumed units. Cross-plan sums require compatible units; otherwise use plan grouping/filter. Preaggregate balance rows by employee/plan before LEFT JOIN; preserve source row count without assuming unique plan rows.

**Slots:**

- `group_by` (enum, required) — values: `company`, `department`, `plan`
- `accrual_code` (entity, optional) — binds: `dbpcm_warehouse.timeoff_balance.accrual_code`
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`; binds: `dbpcm_warehouse.employee.employee_status`
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
WITH b AS (
 SELECT employee_code, accrual_code, sum(beginning_balance) AS beginning,
 sum(accrued_amount) AS accrued, sum(awarded_amount) AS awarded,
 sum(taken_amount) AS taken, count(*) AS balance_rows
 FROM dbpcm_warehouse.timeoff_balance WHERE accrual_code = {accrual_code}
 GROUP BY employee_code, accrual_code
)
SELECT CASE {group_by} WHEN 'department' THEN e.department_name WHEN 'plan' THEN b.accrual_code ELSE 'Company' END AS `Group`, sum(CAST(b.beginning AS Nullable(Decimal(38, 6)))) AS `Beginning Balance`,
 sum(CAST(b.accrued AS Nullable(Decimal(38, 6)))) AS `Accrued Amount`, sum(CAST(b.awarded AS Nullable(Decimal(38, 6)))) AS `Awarded Amount`,
 sum(CAST(b.taken AS Nullable(Decimal(38, 6)))) AS `Taken Amount`, sum(CAST(b.balance_rows AS Nullable(Decimal(20, 0)))) AS `Source Balance Rows`,
 uniqExact(b.employee_code) AS `Employee Count`
FROM b LEFT JOIN dbpcm_warehouse.employee e ON b.employee_code = e.employee_code
WHERE e.employee_status = {employee_status} AND e.department_name = {department} AND e.employee_code = {employee}
GROUP BY CASE {group_by} WHEN 'department' THEN e.department_name WHEN 'plan' THEN b.accrual_code ELSE 'Company' END
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Group` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Group` END ASC
```

**Uses:**

- `dbpcm_warehouse.timeoff_balance.employee_code`
- `dbpcm_warehouse.timeoff_balance.accrual_code`
- `dbpcm_warehouse.timeoff_balance.beginning_balance`
- `dbpcm_warehouse.timeoff_balance.accrued_amount`
- `dbpcm_warehouse.timeoff_balance.awarded_amount`
- `dbpcm_warehouse.timeoff_balance.taken_amount`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-accrual-code-dictionary

**ID:** `bp-accrual-code-dictionary`

**Intent:** Distinct accrual codes with descriptions and counts of employees who hold each bank type

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT b.accrual_code AS "Accrual Code",
       any(b.accrual_description) AS "Accrual Description",
       COUNT(DISTINCT b.employee_code) AS "Holder Count"
FROM dbpcm_warehouse.time_off_balance AS b
WHERE b.accrual_code IS NOT NULL
  AND b.accrual_code != ''
GROUP BY b.accrual_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Holder Count" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Holder Count" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_balance.accrual_code`
- `dbpcm_warehouse.time_off_balance.accrual_description`
- `dbpcm_warehouse.time_off_balance.employee_code`

---

### Blueprint: bp-avg-balance-by-cohort

**ID:** `bp-avg-balance-by-cohort`

**Intent:** Average current accrual balance hours per employee department for active non-archived balances

**Slots:**

- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS "Department",
       round(AVG(b.beginning_balance + b.accrued_amount - b.taken_amount), 2) AS "Average Balance Hours",
       COUNT(DISTINCT b.employee_code) AS "Employees"
FROM dbpcm_warehouse.time_off_balance AS b
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = b.employee_code
WHERE b.accrual_status = 'Active'
  AND b.is_archived = 0
GROUP BY e.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Average Balance Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Average Balance Hours" END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.time_off_balance.beginning_balance`
- `dbpcm_warehouse.time_off_balance.accrued_amount`
- `dbpcm_warehouse.time_off_balance.taken_amount`
- `dbpcm_warehouse.time_off_balance.accrual_status`
- `dbpcm_warehouse.time_off_balance.is_archived`
- `dbpcm_warehouse.time_off_balance.employee_code`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-post-termination-accrual-audit

**ID:** `bp-post-termination-accrual-audit`

**Intent:** Terminated employees with time-off balance accrual activity dated after their termination date, rehire-aware

**Slots:**

- `accrual_window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT b.employee_code AS "Employee Code",
       e.employee_name AS "Employee Name",
       b.accrual_code AS "Accrual Code",
       b.accrual_date AS "Accrual Date",
       e.termination_date AS "Termination Date",
       round(toFloat64(SUM(b.accrued_amount)), 2) AS "Accrual Amount"
FROM dbpcm_warehouse.time_off_balance AS b
JOIN dbpcm_warehouse.employee AS e
  ON e.employee_code = b.employee_code
WHERE e.termination_date IS NOT NULL
  AND toDate32(e.termination_date) > toDate32('1900-01-01')
  AND b.accrual_date IS NOT NULL
  AND b.accrual_date > toDate32(e.termination_date)
  AND (e.most_recent_hire_date IS NULL
       OR toDate32(e.most_recent_hire_date) <= toDate32('1900-01-01')
       OR e.most_recent_hire_date <= e.termination_date)
  AND b.accrued_amount > 0
  AND b.accrual_date >= {accrual_window_start}
  AND b.accrual_date <= {accrual_window_end}
GROUP BY b.employee_code, e.employee_name, b.accrual_code,
         b.accrual_date, e.termination_date
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN b.accrual_date END DESC, CASE WHEN {order_direction} = 'ASC' THEN b.accrual_date END ASC
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.time_off_balance.employee_code`
- `dbpcm_warehouse.time_off_balance.accrual_date`
- `dbpcm_warehouse.time_off_balance.accrued_amount`
- `dbpcm_warehouse.time_off_balance.accrual_code`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`

---

### Blueprint: bp-pto-balances

**ID:** `bp-pto-balances`

**Intent:** Current accrual balances per employee with accrual code and description labels, optionally filtered to a specific employee

**Slots:**

- `ee` (string, optional) — binds: `dbpcm_warehouse.time_off_balance.employee_code`
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT b.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       any(e.department_name) AS "Department",
       b.accrual_code AS "Accrual Code",
       any(b.accrual_description) AS "Accrual Description",
        round(SUM(b.beginning_balance + b.accrued_amount - b.taken_amount), 2) AS "Balance Hours",
       any(b.accrual_status) AS "Accrual Status"
FROM dbpcm_warehouse.time_off_balance AS b
LEFT JOIN dbpcm_warehouse.employee AS e ON e.employee_code = b.employee_code
WHERE b.accrual_status = 'Active'
  AND b.is_archived = 0
  AND b.employee_code = {ee}
GROUP BY b.employee_code, b.accrual_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Balance Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Balance Hours" END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.time_off_balance.employee_code`
- `dbpcm_warehouse.time_off_balance.beginning_balance`
- `dbpcm_warehouse.time_off_balance.accrued_amount`
- `dbpcm_warehouse.time_off_balance.taken_amount`
- `dbpcm_warehouse.time_off_balance.accrual_code`
- `dbpcm_warehouse.time_off_balance.accrual_description`
- `dbpcm_warehouse.time_off_balance.accrual_status`
- `dbpcm_warehouse.time_off_balance.is_archived`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-pto-expiry-risk

**ID:** `bp-pto-expiry-risk`

**Intent:** Accrual balances at risk of expiring within the next N days where reset_date is set and balance is positive

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT b.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       any(e.department_name) AS "Department",
       b.accrual_code AS "Accrual Code",
        round(SUM(b.beginning_balance + b.accrued_amount - b.taken_amount), 2) AS "Balance Hours",
       b.accrual_reset_date AS "Reset Date"
FROM dbpcm_warehouse.time_off_balance AS b
LEFT JOIN dbpcm_warehouse.employee AS e ON e.employee_code = b.employee_code
WHERE b.accrual_status = 'Active'
  AND b.is_archived = 0
  AND b.accrual_reset_date IS NOT NULL
  AND toDate32(b.accrual_reset_date) > toDate32('1900-01-01')
  AND toDate32(b.accrual_reset_date) <= today() + INTERVAL {win} DAY
  AND b.beginning_balance + b.accrued_amount - b.taken_amount > 0
GROUP BY b.employee_code, b.accrual_code, b.accrual_reset_date
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Balance Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Balance Hours" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_balance.employee_code`
- `dbpcm_warehouse.time_off_balance.accrual_reset_date`
- `dbpcm_warehouse.time_off_balance.beginning_balance`
- `dbpcm_warehouse.time_off_balance.accrued_amount`
- `dbpcm_warehouse.time_off_balance.taken_amount`
- `dbpcm_warehouse.time_off_balance.accrual_code`
- `dbpcm_warehouse.time_off_balance.accrual_status`
- `dbpcm_warehouse.time_off_balance.is_archived`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-pto-liability-hours

**ID:** `bp-pto-liability-hours`

**Intent:** Total outstanding PTO liability in hours by department, summing active non-archived accrual balances

**Slots:**

- `dept` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS "Department",
        round(SUM(b.beginning_balance + b.accrued_amount - b.taken_amount), 2) AS "Liability Hours",
       COUNT(DISTINCT b.employee_code) AS "Employees"
FROM dbpcm_warehouse.time_off_balance AS b
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = b.employee_code
WHERE b.accrual_status = 'Active'
  AND b.is_archived = 0
  AND e.department_name = {dept}
GROUP BY e.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Liability Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Liability Hours" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_balance.beginning_balance`
- `dbpcm_warehouse.time_off_balance.accrued_amount`
- `dbpcm_warehouse.time_off_balance.taken_amount`
- `dbpcm_warehouse.time_off_balance.accrual_status`
- `dbpcm_warehouse.time_off_balance.is_archived`
- `dbpcm_warehouse.time_off_balance.employee_code`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-unused-leave-roster

**ID:** `bp-unused-leave-roster`

**Intent:** Employees holding positive accrual balances who have zero approved time-off requests year-to-date

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT b.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       any(e.department_name) AS "Department",
        round(SUM(b.beginning_balance + b.accrued_amount - b.taken_amount), 2) AS "Balance Hours"
FROM dbpcm_warehouse.time_off_balance AS b
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = b.employee_code
 WHERE b.beginning_balance + b.accrued_amount - b.taken_amount > 0
  AND b.employee_code NOT IN (
        SELECT DISTINCT employee_code
        FROM dbpcm_warehouse.time_off_request
        WHERE status = 'Approved'
          AND request_date IS NOT NULL
          AND toDate32(request_date) >= toStartOfYear(today())
      )
GROUP BY b.employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Balance Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Balance Hours" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_balance.employee_code`
- `dbpcm_warehouse.time_off_balance.beginning_balance`
- `dbpcm_warehouse.time_off_balance.accrued_amount`
- `dbpcm_warehouse.time_off_balance.taken_amount`
- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

## Compensation

### Blueprint: bp-average-annual-salary-by-status

**ID:** `bp-average-annual-salary-by-status`

**Intent:** Average annual compensation by pay type, filtered by employee status. Salary uses annual_salary; Hourly uses hourly_salary and estimated annual compensation = hourly_salary * 2080 (40 hours/week, 52 weeks/year; excludes overtime). Keep pay types separate. Defaults to Active + On Leave; employee_status overrides or All removes the filter.

**Slots:**

- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT employee_pay_type AS `Pay Type`, CASE WHEN employee_pay_type = 'Hourly' THEN 'Estimated annual compensation (2080 hours)' WHEN employee_pay_type = 'Salary' THEN 'Recorded annual salary' ELSE 'Unknown compensation basis' END AS `Compensation Basis`, avg(CASE WHEN employee_pay_type = 'Salary' THEN annual_salary WHEN employee_pay_type = 'Hourly' THEN hourly_salary * 2080 ELSE NULL END) AS `Average Annual Compensation`, avg(CASE WHEN employee_pay_type = 'Hourly' THEN hourly_salary ELSE NULL END) AS `Average Hourly Rate` FROM dbpcm_warehouse.employee WHERE (employee_status = {employee_status} OR {employee_status} = 'All') GROUP BY employee_pay_type
```

**Uses:**

- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.hourly_salary`

---

### Blueprint: bp-average-salary-by-department

**ID:** `bp-average-salary-by-department`

**Intent:** Average annual compensation and headcount by department and pay type. Averages exclude unknown compensation; headcount includes it. Salary uses annual_salary; Hourly uses hourly_salary and estimated annual compensation = hourly_salary * 2080 (40 hours/week, 52 weeks/year; excludes overtime). Keep pay types separate. Defaults to Active + On Leave; employee_status overrides or All removes the filter.

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT employee_pay_type AS `Pay Type`, CASE WHEN employee_pay_type = 'Hourly' THEN 'Estimated annual compensation (2080 hours)' WHEN employee_pay_type = 'Salary' THEN 'Recorded annual salary' ELSE 'Unknown compensation basis' END AS `Compensation Basis`, department_name AS department,
       AVG((CASE WHEN employee_pay_type = 'Salary' THEN annual_salary WHEN employee_pay_type = 'Hourly' THEN hourly_salary * 2080 ELSE NULL END)) AS average_annual_compensation,
       COUNT(DISTINCT employee_code) AS headcount, avg(CASE WHEN employee_pay_type = 'Hourly' THEN hourly_salary ELSE NULL END) AS `Average Hourly Rate`
FROM dbpcm_warehouse.employee
WHERE department_name = {department}
  AND (employee_status = {employee_status} OR {employee_status} = 'All')
GROUP BY employee_pay_type, department_name
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.hourly_salary`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-commission-by-employee

**ID:** `bp-commission-by-employee`

**Intent:** Commission earnings per employee for a date period, ranked by total dollars, resolved via the Commission earning category

**Slots:**

- `period` (period_range, required)
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT te.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       round(SUM(te.earn_amount), 2) AS "Total Commission"
FROM (SELECT employee_code, punch_date, earn_amount, earn_code FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
LEFT JOIN dbpcm_warehouse.employee AS e ON e.employee_code = te.employee_code
WHERE ec.category_name = 'Commission'
  AND te.punch_date BETWEEN {period_start} AND {period_end}
GROUP BY te.employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Commission" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Commission" END ASC, "Employee Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-comp-range-exceptions

**ID:** `bp-comp-range-exceptions`

**Intent:** Active employees paid outside their salary range or below a compa-ratio cap

**Slots:**

- `compa_cap_pct` (positive_integer, required)
- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       department_name AS "Department",
       round(annual_salary, 2) AS "Annual Salary",
       round(salary_min, 2) AS "Range Min",
       round(salary_mid, 2) AS "Range Mid",
       round(salary_max, 2) AS "Range Max",
       round(toFloat64(annual_salary) / nullIf(toFloat64(salary_mid), 0), 3) AS "Compa Ratio"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
  AND annual_salary IS NOT NULL
  AND ( toFloat64(annual_salary) < toFloat64(salary_min)
     OR (toFloat64(salary_max) > 0 AND toFloat64(annual_salary) > toFloat64(salary_max))
     OR toFloat64(annual_salary) / nullIf(toFloat64(salary_mid), 0) < {compa_cap_pct} / 100.0 )
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Compa Ratio" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Compa Ratio" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.salary_min`
- `dbpcm_warehouse.employee.salary_mid`
- `dbpcm_warehouse.employee.salary_max`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-compensation-ranking

**ID:** `bp-compensation-ranking`

**Intent:** Top N highest-paid employees separately within Salary and Hourly pay types. Salary uses recorded annual salary; Hourly uses hourly_salary * 2080 estimated annual compensation (40 hours/week, 52 weeks/year, excludes overtime). This is current compensation, not actual payroll earnings. Defaults to Active + On Leave; top_n applies to each pay type.

**Slots:**

- `top_n` (positive_integer, required)
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
WITH compensation AS (
SELECT e.employee_code, e.employee_name, e.department_name, e.employee_pay_type, e.hourly_salary,
CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END AS annual_compensation
FROM dbpcm_warehouse.employee e WHERE (e.employee_status = {employee_status} OR {employee_status} = 'All') AND e.department_name = {department}
), ranked AS (
SELECT employee_code, employee_name, department_name, employee_pay_type, hourly_salary, annual_compensation, row_number() OVER (PARTITION BY employee_pay_type ORDER BY CASE WHEN {order_direction} = 'DESC' THEN annual_compensation END DESC, CASE WHEN {order_direction} = 'ASC' THEN annual_compensation END ASC, employee_code) AS compensation_rank
FROM compensation WHERE annual_compensation IS NOT NULL
)
SELECT employee_pay_type AS `Pay Type`, compensation_rank AS `Rank`, employee_code AS `Employee Code`, employee_name AS `Employee Name`, department_name AS `Department`, annual_compensation AS `Annual Compensation`, hourly_salary AS `Hourly Rate`
FROM ranked WHERE compensation_rank <= {top_n} ORDER BY employee_pay_type, compensation_rank
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.hourly_salary`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-departments-above-company-average-salary

**ID:** `bp-departments-above-company-average-salary`

**Intent:** Departments above the corresponding company average, separately for Salary annual salaries and Hourly annual estimates (hourly rate * 2080, 40 hours/week, 52 weeks/year). Defaults to Active + On Leave; employee_status overrides or All removes the filter.

**Slots:**

- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.

**Status:** validated (drift: clean)

**SQL Template:**

```sql

```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.hourly_salary`

---

### Blueprint: bp-median-annual-salary

**ID:** `bp-median-annual-salary`

**Intent:** True median annual salary of active employees, overall or per department

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT round((quantileExactLow(0.5)(toFloat64(annual_salary)) + quantileExactHigh(0.5)(toFloat64(annual_salary))) / 2, 2) AS "Median Annual Salary",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND annual_salary IS NOT NULL
  AND department_name = {department}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-median-hourly-rate-by-state-union

**ID:** `bp-median-hourly-rate-by-state-union`

**Intent:** True median base hourly rate per works-in state and union flag for active employees

**Slots:**

- `work_state` (string, optional) — binds: `dbpcm_warehouse.employee.works_in_state`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT works_in_state AS "Works In State",
       multiIf(union_code IS NULL OR union_code = '', 'Non-Union', 'Union') AS "Union Flag",
       round((quantileExactLow(0.5)(toFloat64(rate_1)) + quantileExactHigh(0.5)(toFloat64(rate_1))) / 2, 2) AS "Median Hourly Rate",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND rate_1 > 0
  AND works_in_state = {work_state}
GROUP BY works_in_state, "Union Flag"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN works_in_state END DESC, CASE WHEN {order_direction} = 'ASC' THEN works_in_state END ASC, "Union Flag"
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.works_in_state`
- `dbpcm_warehouse.employee.union_code`
- `dbpcm_warehouse.employee.rate_1`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-median-salary-by-position-level

**ID:** `bp-median-salary-by-position-level`

**Intent:** True median annual salary per position level for active employees

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT position_level AS "Position Level",
       round((quantileExactLow(0.5)(toFloat64(annual_salary)) + quantileExactHigh(0.5)(toFloat64(annual_salary))) / 2, 2) AS "Median Annual Salary",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND annual_salary IS NOT NULL
GROUP BY position_level
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN position_level END DESC, CASE WHEN {order_direction} = 'ASC' THEN position_level END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.position_level`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-pay-timing-exceptions

**ID:** `bp-pay-timing-exceptions`

**Intent:** Employees paid after termination or active employees not paid within N days

**Slots:**

- `days_since_pay` (positive_integer, required)
- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       department_name AS "Department",
       employee_status AS "Status",
       termination_date AS "Termination Date",
       last_check_date AS "Last Check Date",
       multiIf(employee_status = 'Terminated', 'Paid After Termination', 'Active Not Paid') AS "Exception Type"
FROM dbpcm_warehouse.employee
WHERE department_name = {department}
  AND ( (employee_status = 'Terminated'
         AND (most_recent_hire_date IS NULL OR most_recent_hire_date < termination_date)
         AND last_check_date IS NOT NULL AND toDate32(last_check_date) > toDate32('1900-01-01')
         AND termination_date IS NOT NULL AND toDate32(termination_date) > toDate32('1900-01-01')
         AND last_check_date > termination_date)
     OR (employee_status = 'Active'
         AND ( NOT (last_check_date IS NOT NULL AND toDate32(last_check_date) > toDate32('1900-01-01'))
            OR last_check_date < now() - INTERVAL {days_since_pay} DAY )) )
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Exception Type" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Exception Type" END ASC, employee_code
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`
- `dbpcm_warehouse.employee.last_check_date`

---

### Blueprint: bp-pay-type-mismatch

**ID:** `bp-pay-type-mismatch`

**Intent:** Active employees whose pay type contradicts their position pay type

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       employee_pay_type AS "Employee Pay Type",
       position_pay_type AS "Position Pay Type"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
  AND employee_pay_type IS NOT NULL
  AND position_pay_type IS NOT NULL
  AND position_pay_type NOT IN ('None', '')
  AND employee_pay_type != position_pay_type
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN employee_code END ASC LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.position_pay_type`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-salary-distribution

**ID:** `bp-salary-distribution`

**Intent:** Annual compensation histogram by pay type with positive whole-currency band_width. Lower inclusive, upper exclusive; percentages use the selected known-compensation population within each pay type. salary_population selects recorded (including zero) or positive values. Salary uses annual_salary; Hourly uses hourly_salary and estimated annual compensation = hourly_salary * 2080 (40 hours/week, 52 weeks/year; excludes overtime). Keep pay types separate. Defaults to Active + On Leave; employee_status overrides or All removes the filter.

**Slots:**

- `band_width` (positive_integer, required)
- `salary_population` (enum, required) — values: `recorded`, `positive`. Use recorded (including zero) unless the user explicitly requests positive/nonzero compensation.
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT e.employee_pay_type AS `Pay Type`, CASE WHEN e.employee_pay_type = 'Hourly' THEN 'Estimated annual compensation (2080 hours)' WHEN e.employee_pay_type = 'Salary' THEN 'Recorded annual salary' ELSE 'Unknown compensation basis' END AS `Compensation Basis`, floor((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) / {band_width}) * {band_width} AS `Lower Bound`,
 (floor((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) / {band_width}) + 1) * {band_width} AS `Upper Bound Exclusive`,
 count(*) AS `Employee Count`, 100.0 * count(*) / sum(count(*)) OVER (PARTITION BY e.employee_pay_type) AS `Percent Within Pay Type`
FROM dbpcm_warehouse.employee e WHERE (e.employee_status = {employee_status} OR {employee_status} = 'All') AND e.department_name = {department} AND e.employee_code = {employee} AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) IS NOT NULL
 AND ({salary_population} = 'recorded' OR (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) > 0)
GROUP BY e.employee_pay_type, floor((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) / {band_width}) ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Lower Bound` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Lower Bound` END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.hourly_salary`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-salary-exact-median

**ID:** `bp-salary-exact-median`

**Intent:** Exact middle-pair median, mean, min, max and known-compensation count by company, department or status, separately by pay type. salary_population selects recorded values (including zero) or positive values; NULLs excluded. Salary uses annual_salary; Hourly uses hourly_salary and estimated annual compensation = hourly_salary * 2080 (40 hours/week, 52 weeks/year; excludes overtime). Keep pay types separate. Defaults to Active + On Leave; employee_status overrides or All removes the filter.

**Slots:**

- `group_by` (enum, required) — values: `company`, `department`, `status`
- `salary_population` (enum, required) — values: `recorded`, `positive`. Use recorded (including zero) unless the user explicitly requests positive/nonzero compensation.
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT e.employee_pay_type AS `Pay Type`, CASE WHEN e.employee_pay_type = 'Hourly' THEN 'Estimated annual compensation (2080 hours)' WHEN e.employee_pay_type = 'Salary' THEN 'Recorded annual salary' ELSE 'Unknown compensation basis' END AS `Compensation Basis`, CASE WHEN {group_by} = 'department' THEN e.department_name WHEN {group_by} = 'status' THEN e.employee_status ELSE 'Company' END AS `Group`, count((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) AS `Known Compensation Count`,
 (quantileExactLow(0.5)((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) + quantileExactHigh(0.5)((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END))) / 2 AS `Exact Median Annual Compensation`,
 avg((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) AS `Average Annual Compensation`, min((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) AS `Minimum Annual Compensation`, max((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) AS `Maximum Annual Compensation`, avg(CASE WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary ELSE NULL END) AS `Average Hourly Rate`
FROM dbpcm_warehouse.employee e WHERE (e.employee_status = {employee_status} OR {employee_status} = 'All') AND e.department_name = {department} AND e.employee_code = {employee}
 AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) IS NOT NULL AND ({salary_population} = 'recorded' OR (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) > 0)
GROUP BY e.employee_pay_type, CASE WHEN {group_by} = 'department' THEN e.department_name WHEN {group_by} = 'status' THEN e.employee_status ELSE 'Company' END ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Group` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Group` END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.hourly_salary`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-salary-histogram

**ID:** `bp-salary-histogram`

**Intent:** Active employee salary distribution histogram in configurable dollar bands, excluding zero or null salary placeholders

**Slots:**

- `band` (positive_integer, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT concat('$', toString(toInt32(floor(toFloat64(annual_salary) / toUInt32({band})) * toUInt32({band}))) ,
              ' - $', toString(toInt32(floor(toFloat64(annual_salary) / toUInt32({band})) * toUInt32({band}) + toUInt32({band}) - 1))) AS "Salary Band",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND annual_salary IS NOT NULL
  AND toFloat64(annual_salary) > 0
GROUP BY floor(toFloat64(annual_salary) / toUInt32({band}))
ORDER BY floor(toFloat64(annual_salary) / toUInt32({band})) ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-salary-threshold-count

**ID:** `bp-salary-threshold-count`

**Intent:** Employee counts by pay type above, below, at least or exactly a whole-currency annual compensation threshold. Empty results mean no matches. Salary uses annual_salary; Hourly uses hourly_salary and estimated annual compensation = hourly_salary * 2080 (40 hours/week, 52 weeks/year; excludes overtime). Keep pay types separate. Defaults to Active + On Leave; employee_status overrides or All removes the filter.

**Slots:**

- `comparison` (enum, required) — values: `above`, `at_least`, `equal`, `below`
- `salary` (nonnegative_integer, required)
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT e.employee_pay_type AS `Pay Type`, CASE WHEN e.employee_pay_type = 'Hourly' THEN 'Estimated annual compensation (2080 hours)' WHEN e.employee_pay_type = 'Salary' THEN 'Recorded annual salary' ELSE 'Unknown compensation basis' END AS `Compensation Basis`, count(DISTINCT e.employee_code) AS `Employee Count` FROM dbpcm_warehouse.employee e WHERE (e.employee_status = {employee_status} OR {employee_status} = 'All') AND e.department_name = {department} AND e.employee_code = {employee} AND (({comparison} = 'above' AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) > {salary}) OR ({comparison} = 'at_least' AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) >= {salary}) OR ({comparison} = 'equal' AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) = {salary}) OR ({comparison} = 'below' AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) < {salary})) GROUP BY e.employee_pay_type
```

**Uses:**

- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.hourly_salary`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-salary-threshold-directory

**ID:** `bp-salary-threshold-directory`

**Intent:** Employee directory filtered by a whole-currency annual compensation threshold, ordered by pay type then annual compensation. row_limit caps the entire directory; this is not a top-N-per-pay-type ranking. Salary uses annual_salary; Hourly uses hourly_salary and estimated annual compensation = hourly_salary * 2080 (40 hours/week, 52 weeks/year; excludes overtime). Keep pay types separate. Defaults to Active + On Leave; employee_status overrides or All removes the filter.

**Slots:**

- `comparison` (enum, required) — values: `above`, `at_least`, `equal`, `below`
- `salary` (nonnegative_integer, required)
- `row_limit` (positive_integer, required)
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT e.employee_pay_type AS `Pay Type`, CASE WHEN e.employee_pay_type = 'Hourly' THEN 'Estimated annual compensation (2080 hours)' WHEN e.employee_pay_type = 'Salary' THEN 'Recorded annual salary' ELSE 'Unknown compensation basis' END AS `Compensation Basis`, e.employee_code AS `Employee Code`, e.department_name AS `Department`, e.employee_status AS `Status`, (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) AS `Annual Compensation`, e.hourly_salary AS `Hourly Rate`
FROM dbpcm_warehouse.employee e WHERE (e.employee_status = {employee_status} OR {employee_status} = 'All') AND e.department_name = {department} AND e.employee_code = {employee} AND (({comparison} = 'above' AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) > {salary}) OR ({comparison} = 'at_least' AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) >= {salary}) OR ({comparison} = 'equal' AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) = {salary}) OR ({comparison} = 'below' AND (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) < {salary}))
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN e.employee_pay_type END DESC, CASE WHEN {order_direction} = 'ASC' THEN e.employee_pay_type END ASC, (CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END) DESC, e.employee_code LIMIT {row_limit}
```

**Uses:**

- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.hourly_salary`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-top-payroll-codes

**ID:** `bp-top-payroll-codes`

**Intent:** Top-N payroll type codes by dollar amount within a chosen register family for a pay period

**Slots:**

- `top_n` (positive_integer, required)
- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `register` (enum, required) — values: `EARN`, `DDUCT`, `EETAX`, `ERTAX`, `EEBEN`, `NETPAYDIST`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT type_code AS "Type Code",
       round(SUM(amount), 2) AS "Total Amount"
FROM dbpcm_warehouse.payroll
WHERE register_type = {register}
  AND pay_period_end_date = {period}
GROUP BY type_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Amount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Amount" END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

## Employee

### Blueprint: bp-change-actor-mix

**ID:** `bp-change-actor-mix`

**Intent:** Monthly counts of employee master-data changes grouped by the actor user type, distinguishing client administrators from employee users and regular users

**Slots:**

- `win` (relative_window, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
# Output aliases are Title-Case display names; consumers keying on column names
# must use the quoted aliases.
SELECT formatDateTime(toStartOfMonth(change_time_added), '%Y-%m') AS "Month",
       change_by_usertype AS "User Type",
       count() AS "Change Events"
FROM dbpcm_warehouse.employee_change
WHERE change_time_added >= addMonths(today(), -{win})
GROUP BY "Month", change_by_usertype
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Change Events" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Change Events" END ASC, "Month", "User Type"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee_change.change_by_usertype`
- `dbpcm_warehouse.employee_change.change_time_added`

---

### Blueprint: bp-change-velocity-spikes

**ID:** `bp-change-velocity-spikes`

**Intent:** Days with unusually high employee master-data change volume within a trailing window, flagged when daily counts exceed one standard deviation above the mean

**Slots:**

- `win` (relative_window, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
# Output aliases are Title-Case display names; consumers keying on column names
# must use the quoted aliases.
WITH daily_counts AS (
  SELECT toDate(change_time_added) AS day,
         count() AS cnt
  FROM dbpcm_warehouse.employee_change
  WHERE change_time_added >= addMonths(today(), -{win})
  GROUP BY toDate(change_time_added)
),
stats AS (
  SELECT avg(cnt) AS avg_cnt,
         stddevPop(cnt) AS sd_cnt
  FROM daily_counts
)
SELECT toString(d.day) AS "Day",
       d.cnt AS "Change Events",
       round(s.avg_cnt, 2) AS "Daily Mean",
       round(s.sd_cnt, 2) AS "Daily Std Dev"
FROM daily_counts AS d
CROSS JOIN stats AS s
WHERE d.cnt > s.avg_cnt + s.sd_cnt
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN d.cnt END DESC, CASE WHEN {order_direction} = 'ASC' THEN d.cnt END ASC, d.day
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee_change.change_time_added`

---

### Blueprint: bp-employee-benefits-summary

**ID:** `bp-employee-benefits-summary`

**Intent:** Total employee benefit contributions and breakdown by benefit type code for a pay period, from payroll register benefit lines

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT type_code AS "Benefit Code",
       round(SUM(amount), 2) AS "Total Benefit Contributions"
FROM dbpcm_warehouse.payroll
WHERE register_type = 'EEBEN'
  AND pay_period_end_date = {period}
GROUP BY type_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Benefit Contributions" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Benefit Contributions" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-employee-change-timeline

**ID:** `bp-employee-change-timeline`

**Intent:** Chronological audit timeline of master-data changes for a single employee, showing changed field, record time, effective date, and change actor

**Slots:**

- `ee` (string, required) — binds: `dbpcm_warehouse.employee_change.employee_code`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
# Output aliases are Title-Case display names; consumers keying on column names
# must use the quoted aliases.
SELECT changed_field AS "Changed Field",
       change_time_added AS "Record Date",
       effective_date AS "Effective Date",
       change_by_username AS "Changed By",
       change_by_usertype AS "User Type",
       change_description AS "Description"
FROM dbpcm_warehouse.employee_change
WHERE employee_code = {ee}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN change_time_added END DESC, CASE WHEN {order_direction} = 'ASC' THEN change_time_added END ASC, changed_field
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee_change.employee_code`
- `dbpcm_warehouse.employee_change.changed_field`
- `dbpcm_warehouse.employee_change.change_description`
- `dbpcm_warehouse.employee_change.change_time_added`
- `dbpcm_warehouse.employee_change.effective_date`
- `dbpcm_warehouse.employee_change.change_by_username`
- `dbpcm_warehouse.employee_change.change_by_usertype`

---

### Blueprint: bp-employee-change-volume

**ID:** `bp-employee-change-volume`

**Intent:** Monthly counts of employee master-data changes by changed field, including both effective and non-effective logged changes

**Slots:**

- `win` (relative_window, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
# Output aliases are Title-Case display names; consumers keying on column names
# must use the quoted aliases.
SELECT formatDateTime(toStartOfMonth(change_time_added), '%Y-%m') AS "Month",
       changed_field AS "Changed Field",
       count() AS "Change Events"
FROM dbpcm_warehouse.employee_change
WHERE change_time_added >= addMonths(today(), -{win})
GROUP BY "Month", changed_field
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Change Events" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Change Events" END ASC, "Month", "Changed Field"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee_change.changed_field`
- `dbpcm_warehouse.employee_change.change_time_added`

---

### Blueprint: bp-employee-check-detail-for-period

**ID:** `bp-employee-check-detail-for-period`

**Intent:** A single employee's paycheck line items for one pay period, with per-item amount and hours

**Slots:**

- `employee` (string, required) — binds: `dbpcm_warehouse.payroll.employee_code`
- `period` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT toString(p.register_type) AS register_type,
       toString(p.type_code) AS type_code,
       any(toString(p.type_code_description)) AS type_code_description,
       e.employee_name AS employee_name,
       e.department_name AS department_name,
       toFloat64(SUM(p.amount)) AS amount,
       toFloat64(SUM(p.type_hours)) AS hours
FROM dbpcm_warehouse.payroll p
LEFT JOIN dbpcm_warehouse.employee e ON e.employee_code = p.employee_code
WHERE p.employee_code = {employee}
  AND p.pay_period_end_date = {period}
GROUP BY p.register_type, p.type_code, e.employee_name, e.department_name
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.type_code_description`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.type_hours`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-employee-coverage-matrix

**ID:** `bp-employee-coverage-matrix`

**Intent:** Multi-source employee coverage matrix showing which employees have roster entries, punch pairs, payroll lines, and timecard earnings, ordered by coverage gap count descending

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH punchers AS (
  SELECT DISTINCT employee_code AS employee_code,
         1 AS has_punches
  FROM dbpcm_warehouse.punch_pair
  WHERE punch_date >= addMonths(today(), -12)
),
payrollers AS (
  SELECT DISTINCT employee_code AS employee_code,
         1 AS has_payroll
  FROM dbpcm_warehouse.payroll
  WHERE pay_period_end_date >= addMonths(today(), -12)
),
earners AS (
  SELECT DISTINCT employee_code AS employee_code,
         1 AS has_earnings
  FROM dbpcm_warehouse.total_earning
  WHERE punch_date >= addMonths(today(), -12)
)
SELECT e.employee_code AS "Employee Code",
       e.employee_name AS "Employee Name",
       e.department_name AS "Department",
       e.employee_status AS "Employee Status",
       1 AS "Has Roster",
       COALESCE(p.has_punches, 0) AS "Has Punches",
       COALESCE(pl.has_payroll, 0) AS "Has Payroll",
       COALESCE(te.has_earnings, 0) AS "Has Earnings",
       (COALESCE(p.has_punches, 0)
        + COALESCE(pl.has_payroll, 0)
        + COALESCE(te.has_earnings, 0)) AS "Coverage Score",
       (3 - COALESCE(p.has_punches, 0)
          - COALESCE(pl.has_payroll, 0)
          - COALESCE(te.has_earnings, 0)) AS "Gap Count"
FROM dbpcm_warehouse.employee e
LEFT JOIN punchers p ON p.employee_code = e.employee_code
LEFT JOIN payrollers pl ON pl.employee_code = e.employee_code
LEFT JOIN earners te ON te.employee_code = e.employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Gap Count" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Gap Count" END ASC, e.employee_code
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`

---

### Blueprint: bp-employee-date-summary

**ID:** `bp-employee-date-summary`

**Intent:** Counts of distinct employees with recorded original hire, most recent hire or termination dates in an explicit half-open period. Group by company, department, current status or event month. These are current employee records, not a complete employment-event history or turnover rate. Current-status filter is optional and may exclude rehired employees. No implicit active/terminated filter; no zero-filled months.

**Slots:**

- `date_basis` (enum, required) — values: `original_hire`, `most_recent_hire`, `termination`
- `group_by` (enum, required) — values: `company`, `department`, `status`, `month`
- `period` (period_range, required)
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`; binds: `dbpcm_warehouse.employee.employee_status`
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT CASE {group_by} WHEN 'month' THEN substring(toString(CASE {date_basis} WHEN 'most_recent_hire' THEN e.most_recent_hire_date WHEN 'termination' THEN e.termination_date ELSE e.hire_date END), 1, 7) WHEN 'department' THEN e.department_name WHEN 'status' THEN e.employee_status ELSE 'Company' END AS `Group`, uniqExact(e.employee_code) AS `Employee Count`
FROM dbpcm_warehouse.employee e WHERE e.employee_status = {employee_status} AND e.department_name = {department} AND e.employee_code = {employee} AND (CASE {date_basis} WHEN 'most_recent_hire' THEN e.most_recent_hire_date WHEN 'termination' THEN e.termination_date ELSE e.hire_date END) >= toDate({period_start}) AND (CASE {date_basis} WHEN 'most_recent_hire' THEN e.most_recent_hire_date WHEN 'termination' THEN e.termination_date ELSE e.hire_date END) < toDate({period_end})
GROUP BY `Group` ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Group` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Group` END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-employee-day-clock-span

**ID:** `bp-employee-day-clock-span`

**Intent:** Employee-day first recorded In time, last recorded Out time and matched-pair count, with paid hours. Clock span may contain unpaid gaps; it is not hours worked, a single shift or a lateness finding. Days are assigned by recorded punch_date; midnight ownership is not inferred. Optional punch-type filters restrict which pairs contribute. Required half-open punch-date period and resolved table_source prevent accidental cross-source double counting. FINAL deduplicates storage versions, not overlapping real-world events. Department means recorded labor-allocation department code, not current home department. No scheduled shift, tardiness or overtime policy is inferred. Observed first/last dates do not prove completeness; missing days/months are not zero-filled. Salaried approved leave and automatic pay-period-end entries can generate these records; this blueprint does not establish physical attendance or current clock-in.

**Slots:**

- `period` (period_range, required)
- `source` (entity, required) — binds: `dbpcm_warehouse.punch_pair.table_source`
- `employee` (entity, optional) — binds: `dbpcm_warehouse.punch_pair.employee_code`
- `department` (entity, optional) — binds: `dbpcm_warehouse.punch_pair.department_code`
- `hours_type` (entity, optional) — binds: `dbpcm_warehouse.punch_pair.hours_type`
- `pay_class` (entity, optional) — binds: `dbpcm_warehouse.punch_pair.pay_class_code`
- `in_type` (enum, optional) — values: `ID`, `IL`, `IB`, `IX`; binds: `dbpcm_warehouse.punch_pair.in_punch_type`
- `out_type` (enum, optional) — values: `OD`, `OL`, `OB`, `OX`; binds: `dbpcm_warehouse.punch_pair.out_punch_type`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT p.employee_code AS `Employee Code`, e.employee_name AS `Employee Name`, e.department_name AS `Department`, p.punch_date AS `Punch Date`, min(p.in_punch_time) AS `First Recorded In`, max(p.out_punch_time) AS `Last Recorded Out`, count(*) AS `Pair Count`, sum(p.earn_hours) AS `Paid Hours`
FROM dbpcm_warehouse.punch_pair AS p LEFT JOIN dbpcm_warehouse.employee e ON e.employee_code = p.employee_code
WHERE p.punch_date >= toDate({period_start}) AND p.punch_date < toDate({period_end}) AND p.table_source = {source} AND p.employee_code = {employee} AND p.department_code = {department} AND p.hours_type = {hours_type} AND p.pay_class_code = {pay_class} AND p.in_punch_type = {in_type} AND p.out_punch_type = {out_type}
GROUP BY p.employee_code, e.employee_name, e.department_name, p.punch_date ORDER BY CASE WHEN {order_direction} = 'DESC' THEN p.employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN p.employee_code END ASC, p.punch_date
```

**Uses:**

- `dbpcm_warehouse.punch_pair.department_code`
- `dbpcm_warehouse.punch_pair.earn_hours`
- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.hours_type`
- `dbpcm_warehouse.punch_pair.in_punch_time`
- `dbpcm_warehouse.punch_pair.in_punch_type`
- `dbpcm_warehouse.punch_pair.out_punch_time`
- `dbpcm_warehouse.punch_pair.out_punch_type`
- `dbpcm_warehouse.punch_pair.pay_class_code`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.punch_pair.table_source`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-employee-directory

**ID:** `bp-employee-directory`

**Intent:** Filtered active-employee directory with identity, contact, title and pay columns

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `pay_basis` (string, optional) — binds: `dbpcm_warehouse.employee.pay_basis`
- `employment_type` (string, optional) — binds: `dbpcm_warehouse.employee.employment_type`
- `supervisor` (string, optional) — binds: `dbpcm_warehouse.employee.primary_supervisor_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       work_email AS "Work Email",
       position_title_position_info AS "Title",
       department_name AS "Department",
       badge_number AS "Badge",
       pay_basis AS "Pay Basis",
       employment_type AS "Employment Type",
       primary_supervisor_name AS "Supervisor"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
  AND pay_basis = {pay_basis}
  AND employment_type = {employment_type}
  AND primary_supervisor_name = {supervisor}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN employee_code END ASC LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.work_email`
- `dbpcm_warehouse.employee.position_title_position_info`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.badge_number`
- `dbpcm_warehouse.employee.pay_basis`
- `dbpcm_warehouse.employee.employment_type`
- `dbpcm_warehouse.employee.primary_supervisor_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-employee-record-dq-flags

**ID:** `bp-employee-record-dq-flags`

**Intent:** Per-employee data-quality flags across SUI state, hire date and date of birth

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT "Employee Code",
       "Name",
       "Department",
       "Works In State",
       "SUI State",
       "Hire Date",
       "Date Of Birth",
       "DQ Flag"
FROM (
       SELECT employee_code AS "Employee Code",
              employee_name AS "Name",
              department_name AS "Department",
              works_in_state AS "Works In State",
              sui_state AS "SUI State",
              hire_date AS "Hire Date",
              date_of_birth AS "Date Of Birth",
              'SUI Mismatch' AS "DQ Flag"
       FROM dbpcm_warehouse.employee
       WHERE employee_status = 'Active'
         AND lives_in_state IS NOT NULL AND lives_in_state != ''
         AND works_in_state IS NOT NULL AND works_in_state != ''
         AND sui_state IS NOT NULL AND sui_state != ''
         AND works_in_state != sui_state
       UNION ALL
       SELECT employee_code AS "Employee Code",
              employee_name AS "Name",
              department_name AS "Department",
              works_in_state AS "Works In State",
              sui_state AS "SUI State",
              hire_date AS "Hire Date",
              date_of_birth AS "Date Of Birth",
              'Future Hire Date' AS "DQ Flag"
       FROM dbpcm_warehouse.employee
       WHERE employee_status = 'Active'
         AND hire_date > now()
       UNION ALL
       SELECT employee_code AS "Employee Code",
              employee_name AS "Name",
              department_name AS "Department",
              works_in_state AS "Works In State",
              sui_state AS "SUI State",
              hire_date AS "Hire Date",
              date_of_birth AS "Date Of Birth",
              'Missing Or Sentinel DOB' AS "DQ Flag"
       FROM dbpcm_warehouse.employee
       WHERE employee_status = 'Active'
         AND NOT (date_of_birth IS NOT NULL AND toDate32(date_of_birth) > toDate32('1900-01-01'))
       UNION ALL
       SELECT employee_code AS "Employee Code",
              employee_name AS "Name",
              department_name AS "Department",
              works_in_state AS "Works In State",
              sui_state AS "SUI State",
              hire_date AS "Hire Date",
              date_of_birth AS "Date Of Birth",
              'Age Under 16' AS "DQ Flag"
       FROM dbpcm_warehouse.employee
       WHERE employee_status = 'Active'
         AND date_of_birth IS NOT NULL AND toDate32(date_of_birth) > toDate32('1900-01-01')
         AND date_diff('year', date_of_birth, now()) < 16
       UNION ALL
       SELECT employee_code AS "Employee Code",
              employee_name AS "Name",
              department_name AS "Department",
              works_in_state AS "Works In State",
              sui_state AS "SUI State",
              hire_date AS "Hire Date",
              date_of_birth AS "Date Of Birth",
              'Age Over 90' AS "DQ Flag"
       FROM dbpcm_warehouse.employee
       WHERE employee_status = 'Active'
         AND date_of_birth IS NOT NULL AND toDate32(date_of_birth) > toDate32('1900-01-01')
         AND date_diff('year', date_of_birth, now()) > 90
     )
WHERE "Department" = {department}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Employee Code" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Employee Code" END ASC, "DQ Flag"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.works_in_state`
- `dbpcm_warehouse.employee.sui_state`
- `dbpcm_warehouse.employee.lives_in_state`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.date_of_birth`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-employee-summary

**ID:** `bp-employee-summary`

**Intent:** Headcount and annual compensation sum, average, minimum, maximum and known-compensation count by company, department or status, separately by pay type. Unknown compensation is NULL; headcount retains other and unknown pay types. Salary uses annual_salary; Hourly uses hourly_salary and estimated annual compensation = hourly_salary * 2080 (40 hours/week, 52 weeks/year; excludes overtime). Keep pay types separate. Defaults to Active + On Leave; employee_status overrides or All removes the filter.

**Slots:**

- `group_by` (enum, required) — values: `company`, `department`, `status`
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT e.employee_pay_type AS `Pay Type`, CASE WHEN e.employee_pay_type = 'Hourly' THEN 'Estimated annual compensation (2080 hours)' WHEN e.employee_pay_type = 'Salary' THEN 'Recorded annual salary' ELSE 'Unknown compensation basis' END AS `Compensation Basis`, CASE WHEN {group_by} = 'department' THEN e.department_name WHEN {group_by} = 'status' THEN e.employee_status ELSE 'Company' END AS `Group`,
 uniqExact(e.employee_code) AS `Employee Count`,
 count((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) AS `Known Compensation Count`,
 sum((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) AS `Annual Compensation Sum`, avg((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) AS `Average Annual Compensation`,
 min((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) AS `Minimum Annual Compensation`, max((CASE WHEN e.employee_pay_type = 'Salary' THEN e.annual_salary WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary * 2080 ELSE NULL END)) AS `Maximum Annual Compensation`, avg(CASE WHEN e.employee_pay_type = 'Hourly' THEN e.hourly_salary ELSE NULL END) AS `Average Hourly Rate`
FROM dbpcm_warehouse.employee e
WHERE (e.employee_status = {employee_status} OR {employee_status} = 'All') AND e.department_name = {department} AND e.employee_code = {employee}
GROUP BY e.employee_pay_type, CASE WHEN {group_by} = 'department' THEN e.department_name WHEN {group_by} = 'status' THEN e.employee_status ELSE 'Company' END
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Group` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Group` END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.employee_pay_type`
- `dbpcm_warehouse.employee.hourly_salary`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-employee-tax-withheld

**ID:** `bp-employee-tax-withheld`

**Intent:** Total employee tax withholding and breakdown by tax type code for a pay period, from payroll register employee-tax lines

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT type_code AS "Tax Code",
       round(SUM(amount), 2) AS "Total Tax Withheld"
FROM dbpcm_warehouse.payroll
WHERE register_type = 'EETAX'
  AND pay_period_end_date = {period}
GROUP BY type_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Tax Withheld" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Tax Withheld" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-employee-tenure-summary

**ID:** `bp-employee-tenure-summary`

**Intent:** Elapsed years from original or most recent hire date to a supplied ISO YYYY-MM-DD reference_date, using calendar days / 365.25. Company/department/current-status groups, average/min/max and valid-date count. Excludes NULL, dates at/before 1900-01-01 and future hire dates. all-status elapsed time does NOT stop at termination and is not historical active headcount. Date is explicit, not inferred from today. Defaults to Active + On Leave; employee_status selects another status or All to remove the filter.

**Slots:**

- `group_by` (enum, required) — values: `company`, `department`, `status`
- `hire_basis` (enum, required) — values: `original_hire`, `most_recent_hire`
- `reference_date` (string, required)
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT CASE {group_by} WHEN 'department' THEN e.department_name WHEN 'status' THEN e.employee_status ELSE 'Company' END AS `Group`, count(*) AS `Valid Hire Date Count`,
 avg(dateDiff('day', CASE {hire_basis} WHEN 'most_recent_hire' THEN e.most_recent_hire_date ELSE e.hire_date END, toDate({reference_date})) / 365.25) AS `Average Elapsed Years`,
 min(dateDiff('day', CASE {hire_basis} WHEN 'most_recent_hire' THEN e.most_recent_hire_date ELSE e.hire_date END, toDate({reference_date})) / 365.25) AS `Minimum Elapsed Years`,
 max(dateDiff('day', CASE {hire_basis} WHEN 'most_recent_hire' THEN e.most_recent_hire_date ELSE e.hire_date END, toDate({reference_date})) / 365.25) AS `Maximum Elapsed Years`
FROM dbpcm_warehouse.employee e WHERE (e.employee_status = {employee_status} OR {employee_status} = 'All') AND e.department_name = {department} AND e.employee_code = {employee} AND (CASE {hire_basis} WHEN 'most_recent_hire' THEN e.most_recent_hire_date ELSE e.hire_date END) > toDate('1900-01-01') AND (CASE {hire_basis} WHEN 'most_recent_hire' THEN e.most_recent_hire_date ELSE e.hire_date END) <= toDate({reference_date})
GROUP BY CASE {group_by} WHEN 'department' THEN e.department_name WHEN 'status' THEN e.employee_status ELSE 'Company' END ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Group` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Group` END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-employees-in-inactive-seats

**ID:** `bp-employees-in-inactive-seats`

**Intent:** Active employees assigned to position seats with inactive or disabled seat status

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       position_code AS "Position Code",
       position_seat_number AS "Seat Number",
       status_position_seat AS "Seat Status"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
  AND status_position_seat IN ('I', 'D')
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN employee_code END ASC LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.position_code`
- `dbpcm_warehouse.employee.position_seat_number`
- `dbpcm_warehouse.employee.status_position_seat`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-inactive-supervisor-audit

**ID:** `bp-inactive-supervisor-audit`

**Intent:** Active employees whose primary supervisor status is not active, supervisor inactivity audit

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.employee_code AS "Employee Code",
       e.employee_name AS "Name",
       e.department_name AS "Department",
       e.primary_supervisor_ee_code AS "Supervisor Code",
       s.employee_name AS "Supervisor Name",
       s.employee_status AS "Supervisor Status"
FROM dbpcm_warehouse.employee AS e
JOIN dbpcm_warehouse.employee AS s ON s.employee_code = e.primary_supervisor_ee_code
WHERE e.employee_status = 'Active'
  AND e.primary_supervisor_ee_code IS NOT NULL
  AND e.primary_supervisor_ee_code != ''
  AND s.employee_status != 'Active'
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN e.employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN e.employee_code END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.primary_supervisor_ee_code`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-inactive-without-leave-start

**ID:** `bp-inactive-without-leave-start`

**Intent:** Inactive employees missing a valid leave-of-absence start date

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       last_worked_date AS "Last Worked",
       leave_absence_start AS "Leave Absence Start"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Inactive'
  AND NOT (leave_absence_start IS NOT NULL AND toDate32(leave_absence_start) > toDate32('1900-01-01'))
  AND department_name = {department}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN employee_code END ASC LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.last_worked_date`
- `dbpcm_warehouse.employee.leave_absence_start`

---

### Blueprint: bp-longest-tenured-employees

**ID:** `bp-longest-tenured-employees`

**Intent:** Top-N longest-tenured active employees by original hire date

**Slots:**

- `top_n` (relative_window, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       hire_date AS "Hire Date",
       round(date_diff('day', hire_date, now()) / 365.25, 1) AS "Tenure Years"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
ORDER BY hire_date ASC, employee_code
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-missing-emergency-contacts

**ID:** `bp-missing-emergency-contacts`

**Intent:** Active employees missing a primary emergency contact phone

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       primary_phone AS "Primary Phone"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
  AND (primary_emergency_contact_phone IS NULL OR primary_emergency_contact_phone = '')
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN employee_code END ASC LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.primary_phone`
- `dbpcm_warehouse.employee.primary_emergency_contact_phone`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-missing-supervisor-exceptions

**ID:** `bp-missing-supervisor-exceptions`

**Intent:** Active employees missing a primary supervisor assignment

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       position_title_position_info AS "Title",
       work_email AS "Work Email",
       primary_supervisor_name AS "Supervisor"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
  AND (primary_supervisor_ee_code IS NULL OR primary_supervisor_ee_code = '')
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN employee_code END ASC LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.position_title_position_info`
- `dbpcm_warehouse.employee.work_email`
- `dbpcm_warehouse.employee.primary_supervisor_ee_code`
- `dbpcm_warehouse.employee.primary_supervisor_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-name-identity-audit

**ID:** `bp-name-identity-audit`

**Intent:** Active employees whose preferred name differs from their legal name

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Legal Name",
       concatWithSeparator(' ', nullIf(preferred_first_name, ''), nullIf(preferred_last_name, '')) AS "Preferred Name",
       department_name AS "Department"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
  AND nullIf(preferred_first_name, '') IS NOT NULL
  AND (preferred_first_name != first_name
       OR nullIf(preferred_last_name, '') != last_name)
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN employee_code END ASC LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.preferred_first_name`
- `dbpcm_warehouse.employee.preferred_last_name`
- `dbpcm_warehouse.employee.first_name`
- `dbpcm_warehouse.employee.last_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-post-termination-changes

**ID:** `bp-post-termination-changes`

**Intent:** Employee master-data changes with an effective date after the employee's termination date, flagged as a post-termination audit anomaly

**Slots:**

- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
# Output aliases are Title-Case display names; consumers keying on column names
# must use the quoted aliases.
SELECT ec.employee_code AS "Employee Code",
       any(e.employee_name) AS "Name",
       ec.changed_field AS "Changed Field",
       count() AS "Post-Termination Changes"
FROM (SELECT employee_code, effective_date, change_time_added, changed_field FROM dbpcm_warehouse.employee_change) AS ec
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = ec.employee_code
WHERE ec.effective_date IS NOT NULL
  AND ec.change_time_added BETWEEN {window_start}
                               AND {window_end}
  AND e.termination_date IS NOT NULL
  AND toDate32(e.termination_date) > toDate32('1900-01-01')
  AND ec.effective_date > e.termination_date
GROUP BY ec.employee_code, ec.changed_field
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Post-Termination Changes" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Post-Termination Changes" END ASC, "Employee Code", "Changed Field"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee_change.employee_code`
- `dbpcm_warehouse.employee_change.effective_date`
- `dbpcm_warehouse.employee_change.change_time_added`
- `dbpcm_warehouse.employee_change.changed_field`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-retroactive-change-share

**ID:** `bp-retroactive-change-share`

**Intent:** Count and share of retroactive employee master-data changes where the effective date precedes the record date by more than a specified number of days, grouped by changed field

**Slots:**

- `lag_days` (positive_integer, required)
- `win` (relative_window, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
# Output aliases are Title-Case display names; consumers keying on column names
# must use the quoted aliases.
SELECT changed_field AS "Changed Field",
       count() AS "Retroactive Changes",
       round(count() * 100.0
               / nullIf(
                   (SELECT count()
                    FROM dbpcm_warehouse.employee_change
                    WHERE change_time_added >= addMonths(today(), -{win})
                      AND effective_date IS NOT NULL),
                   0),
             2) AS "Share Pct"
FROM dbpcm_warehouse.employee_change
WHERE change_time_added >= addMonths(today(), -{win})
  AND effective_date IS NOT NULL
  AND dateDiff('day', effective_date, change_time_added) > {lag_days}
GROUP BY changed_field
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Retroactive Changes" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Retroactive Changes" END ASC, "Changed Field"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee_change.effective_date`
- `dbpcm_warehouse.employee_change.change_time_added`
- `dbpcm_warehouse.employee_change.changed_field`

---

### Blueprint: bp-supervision-anomalies

**ID:** `bp-supervision-anomalies`

**Intent:** Self-supervising and missing-primary-supervisor anomaly audit for active employees

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       primary_supervisor_ee_code AS "Supervisor Code",
       primary_supervisor_name AS "Supervisor Name",
       'Self-Supervising' AS "Anomaly Type"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND primary_supervisor_ee_code IS NOT NULL
  AND primary_supervisor_ee_code != ''
  AND primary_supervisor_ee_code = employee_code
UNION ALL
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       primary_supervisor_ee_code AS "Supervisor Code",
       primary_supervisor_name AS "Supervisor Name",
       'Missing Supervisor' AS "Anomaly Type"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND (primary_supervisor_ee_code IS NULL OR primary_supervisor_ee_code = '')
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Anomaly Type" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Anomaly Type" END ASC, "Employee Code"
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.primary_supervisor_ee_code`
- `dbpcm_warehouse.employee.primary_supervisor_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-top-changed-fields

**ID:** `bp-top-changed-fields`

**Intent:** Top employee master-data changed fields ranked by total change-event count within a trailing month window

**Slots:**

- `win` (relative_window, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
# Output aliases are Title-Case display names; consumers keying on column names
# must use the quoted aliases.
SELECT changed_field AS "Changed Field",
       count() AS "Change Events"
FROM dbpcm_warehouse.employee_change
WHERE change_time_added >= addMonths(today(), -{win})
GROUP BY changed_field
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Change Events" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Change Events" END ASC, "Changed Field"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee_change.changed_field`
- `dbpcm_warehouse.employee_change.change_time_added`

---

### Blueprint: bp-upcoming-anniversaries

**ID:** `bp-upcoming-anniversaries`

**Intent:** Active employees with upcoming work anniversaries based on hire date month and day within the next N days

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       hire_date AS "Hire Date",
       date_diff('year', hire_date, now()) AS "Years of Service"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND hire_date IS NOT NULL
  AND toDate32(hire_date) > toDate32('1900-01-01')
  AND toDayOfYear(hire_date) >= toDayOfYear(now())
  AND toDayOfYear(hire_date) <= toDayOfYear(now()) + {win}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN toDayOfYear(hire_date) END DESC, CASE WHEN {order_direction} = 'ASC' THEN toDayOfYear(hire_date) END ASC, employee_code
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.employee_status`

---


## Expenses

### Blueprint: bp-card-vs-personal-split

**ID:** `bp-card-vs-personal-split`

**Intent:** Corporate card versus personal expense split for a payment period

**Slots:**

- `period` (period, required) — binds: `dbpcm_warehouse.expense.expense_paid_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  expense_type AS "Expense Type",
  round(toFloat64(SUM(amount_approved)), 2) AS "Approved Amount"
FROM dbpcm_warehouse.expense
WHERE expense_type IN ('Personal', 'Corporate Card')
  AND expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND expense_paid_date = {period}
GROUP BY expense_type
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Approved Amount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Approved Amount" END ASC
```

**Uses:**

- `dbpcm_warehouse.expense.expense_type`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-denied-expenses

**ID:** `bp-denied-expenses`

**Intent:** Denied expense total and denial rate by department for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `department` (string, optional) — binds: `dbpcm_warehouse.expense.department_description`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  department_description AS "Department",
  countIf(expense_status = 'Denied') AS "Denied Count",
  round(toFloat64(SUMIf(amount_approved, expense_status = 'Denied')), 2) AS "Denied Amount",
  round(toFloat64(countIf(expense_status = 'Denied')) / nullIf(count(), 0), 4) AS "Denial Rate"
FROM dbpcm_warehouse.expense
WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid', 'Denied')
  AND expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
  AND department_description = {department}
GROUP BY department_description
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Denied Amount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Denied Amount" END ASC, "Department"
```

**Uses:**

- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.department_description`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-duplicate-expense-flag-list

**ID:** `bp-duplicate-expense-flag-list`

**Intent:** Expense lines flagged as potential duplicate for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  employee_code AS "Employee Code",
  round(toFloat64(SUM(amount_approved)), 2) AS "Flagged Amount",
  count() AS "Flagged Lines"
FROM dbpcm_warehouse.expense
WHERE potential_duplicate_expense = 'Yes'
  AND expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
GROUP BY employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Flagged Amount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Flagged Amount" END ASC, "Employee Code"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.expense.potential_duplicate_expense`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.employee_code`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-expense-approval-backlog

**ID:** `bp-expense-approval-backlog`

**Intent:** Expenses awaiting approval longest, by submission age, with optional minimum wait threshold

**Slots:**

- `min_age` (nonnegative_integer, required)
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  employee_code AS "Employee Code",
  count() AS "Pending Lines",
  round(toFloat64(SUM(amount_approved)), 2) AS "Pending Amount",
  round(AVG(dateDiff('day', date_time_submitted, today())), 1) AS "Avg Days Pending"
FROM dbpcm_warehouse.expense
WHERE expense_status = 'Pending Approval'
  AND date_time_submitted IS NOT NULL
  AND toDate32(date_time_submitted) > toDate32('1900-01-01')
  AND dateDiff('day', date_time_submitted, today()) >= {min_age}
GROUP BY employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Avg Days Pending" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Avg Days Pending" END ASC, "Employee Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.date_time_submitted`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.employee_code`

---

### Blueprint: bp-expense-approval-trim

**ID:** `bp-expense-approval-trim`

**Intent:** Submitted versus approved amount delta by expense rule for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  expense_rule_description AS "Expense Rule",
  round(toFloat64(SUM(amount_approved)), 2) AS "Approved Total",
  round(toFloat64(SUM(amount_requested)), 2) AS "Submitted Total",
  round(toFloat64(SUM(amount_requested - amount_approved)), 2) AS "Trimmed Total",
  count() AS "Line Count"
FROM dbpcm_warehouse.expense
WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
GROUP BY expense_rule_description
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Trimmed Total" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Trimmed Total" END ASC, "Expense Rule"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.amount_requested`
- `dbpcm_warehouse.expense.expense_rule_description`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-expense-by-account-category

**ID:** `bp-expense-by-account-category`

**Intent:** Approved spend by expense account code category for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  expense_account_code AS "Account Code",
  any(expense_account_description) AS "Account Description",
  round(toFloat64(SUM(amount_approved)), 2) AS "Approved Spend"
FROM dbpcm_warehouse.expense
WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
GROUP BY expense_account_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Approved Spend" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Approved Spend" END ASC, "Account Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_account_code`
- `dbpcm_warehouse.expense.expense_account_description`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-expense-by-policy

**ID:** `bp-expense-by-policy`

**Intent:** Approved expense spend by policy description ranked for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  expense_policy_description AS "Policy",
  round(toFloat64(SUM(amount_approved)), 2) AS "Approved Spend",
  count() AS "Line Count"
FROM dbpcm_warehouse.expense
WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
GROUP BY expense_policy_description
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Approved Spend" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Approved Spend" END ASC, "Policy"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.expense.expense_policy_description`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-expense-concept-spend

**ID:** `bp-expense-concept-spend`

**Intent:** Expense spend matched to a user-supplied concept via rule or account descriptions for a payment date window

**Slots:**

- `concept` (string, required)
- `expense_window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  coalesce(expense_rule_description, expense_account_description) AS "Matched Description",
  round(toFloat64(SUM(amount_approved)), 2) AS "Approved Spend",
  count() AS "Line Count"
FROM dbpcm_warehouse.expense
WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
  AND (
    expense_rule_description ILIKE concat('%', {concept}, '%')
    OR expense_account_description ILIKE concat('%', {concept}, '%')
  )
GROUP BY coalesce(expense_rule_description, expense_account_description)
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Approved Spend" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Approved Spend" END ASC, "Matched Description"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.expense.expense_account_description`
- `dbpcm_warehouse.expense.expense_rule_description`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-expense-cycle-time

**ID:** `bp-expense-cycle-time`

**Intent:** Median expense reimbursement cycle time by month, submission to approval and approval to payment

**Slots:**

- `window_months` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  formatDateTime(toStartOfMonth(expense_paid_date), '%Y-%m') AS "Month",
  count() AS "Line Count",
  round(toFloat64(dateDiff('day', date_time_submitted, approved_date)), 1) AS "Median Submission To Approval",
  round(toFloat64(dateDiff('day', approved_date, expense_paid_date)), 1) AS "Median Approval To Payment"
FROM dbpcm_warehouse.expense
WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND date_time_submitted IS NOT NULL
  AND toDate32(date_time_submitted) > toDate32('1900-01-01')
  AND approved_date IS NOT NULL
  AND toDate32(approved_date) > toDate32('1900-01-01')
  AND expense_paid_date IS NOT NULL
  AND toDate32(expense_paid_date) > toDate32('1900-01-01')
  AND expense_paid_date >= (
    SELECT addMonths(toStartOfMonth(max(expense_paid_date)), -{window_months})
    FROM dbpcm_warehouse.expense
    WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
      AND expense_paid_date IS NOT NULL
      AND toDate32(expense_paid_date) > toDate32('1900-01-01')
  )
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.expense.date_time_submitted`
- `dbpcm_warehouse.expense.approved_date`
- `dbpcm_warehouse.expense.expense_paid_date`
- `dbpcm_warehouse.expense.expense_status`

---

### Blueprint: bp-expense-line-size-distribution

**ID:** `bp-expense-line-size-distribution`

**Intent:** Distribution of expense line amounts into bands by account description for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `band` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  expense_account_description AS "Account Description",
  toInt32(floor(toFloat64(amount_approved) / {band}) * {band}) AS "Band Start",
  count() AS "Line Count"
FROM dbpcm_warehouse.expense
WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
GROUP BY expense_account_description,
         toInt32(floor(toFloat64(amount_approved) / {band}) * {band})
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Account Description" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Account Description" END ASC, "Band Start"
```

**Uses:**

- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_account_description`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-expense-monthly-trend

**ID:** `bp-expense-monthly-trend`

**Intent:** Monthly approved expense spend trend over the last N months with top account slice

**Slots:**

- `window_months` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  formatDateTime(toStartOfMonth(expense_paid_date), '%Y-%m') AS "Month",
  round(toFloat64(SUM(amount_approved)), 2) AS "Total Approved Spend"
FROM dbpcm_warehouse.expense
WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND expense_paid_date >= (
    SELECT addMonths(toStartOfMonth(max(expense_paid_date)), -{window_months})
    FROM dbpcm_warehouse.expense
    WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
      AND toDate32(expense_paid_date) > toDate32('1900-01-01')
  )
  AND toDate32(expense_paid_date) > toDate32('1900-01-01')
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`
- `dbpcm_warehouse.expense.expense_account_description`

---

### Blueprint: bp-expense-payroll-recon

**ID:** `bp-expense-payroll-recon`

**Intent:** Reconcile approved expense amounts against payroll deduction reimbursement lines by payroll transaction number for a pay period

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH exp_agg AS (
  SELECT payroll_transaction_number AS txn,
         round(toFloat64(SUM(amount_approved)), 2) AS exp_amount
  FROM dbpcm_warehouse.expense
  WHERE expense_status IN ('Approved', 'Paid', 'Marked as Paid')
    AND payroll_transaction_number != ''
  GROUP BY payroll_transaction_number
),
pay_agg AS (
  SELECT transaction_number AS txn,
         round(toFloat64(SUM(amount)), 2) AS pay_amount
  FROM dbpcm_warehouse.payroll
  WHERE register_type = 'DDUCT'
    AND pay_period_end_date = {period}
  GROUP BY transaction_number
)
SELECT COALESCE(e.txn, p.txn) AS "Transaction Number",
       round(COALESCE(e.exp_amount, 0), 2) AS "Expense Amount",
       round(COALESCE(p.pay_amount, 0), 2) AS "Payroll Amount",
       round(COALESCE(e.exp_amount, 0) - COALESCE(p.pay_amount, 0), 2) AS "Difference",
       round(abs(COALESCE(e.exp_amount, 0) - COALESCE(p.pay_amount, 0))
         / nullIf(COALESCE(p.pay_amount, 0), 0) * 100, 2) AS "Pct Diff"
FROM exp_agg e
FULL OUTER JOIN pay_agg p
  ON p.txn = e.txn
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN abs(COALESCE(e.exp_amount, 0) - COALESCE(p.pay_amount, 0)) END DESC, CASE WHEN {order_direction} = 'ASC' THEN abs(COALESCE(e.exp_amount, 0) - COALESCE(p.pay_amount, 0)) END ASC
```

**Uses:**

- `dbpcm_warehouse.expense.payroll_transaction_number`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.payroll.transaction_number`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-expense-per-capita

**ID:** `bp-expense-per-capita`

**Intent:** Per-capita approved expense spend by department for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  COALESCE(e.department_name, x.department_description) AS "Department",
  round(toFloat64(SUM(x.amount_approved)), 2) AS "Approved Spend",
  count(DISTINCT e.employee_code) AS "Active Headcount",
  round(toFloat64(SUM(x.amount_approved)) / nullIf(count(DISTINCT e.employee_code), 0), 2) AS "Per Capita Spend"
FROM dbpcm_warehouse.expense AS x
LEFT JOIN dbpcm_warehouse.employee AS e
  ON e.employee_code = x.employee_code
  AND e.employee_status = 'Active'
WHERE x.expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND x.expense_paid_date >= {expense_window_start}
  AND x.expense_paid_date <= {expense_window_end}
GROUP BY COALESCE(e.department_name, x.department_description)
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Per Capita Spend" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Per Capita Spend" END ASC, "Department"
```

**Uses:**

- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`
- `dbpcm_warehouse.expense.department_description`
- `dbpcm_warehouse.expense.employee_code`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-expense-senders-no-punches

**ID:** `bp-expense-senders-no-punches`

**Intent:** Employees who submitted expense activity but have zero punched timecard records over the trailing N months — a cross-check audit of expense submitters with no timecards

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT DISTINCT e.employee_code AS "Employee Code",
       e.employee_name AS "Employee Name",
       e.department_name AS "Department"
FROM dbpcm_warehouse.expense AS ex
JOIN dbpcm_warehouse.employee AS e
  ON e.employee_code = ex.employee_code
WHERE ex.expense_paid_date >= addMonths(today(), -{win})
  AND ex.employee_code NOT IN (
    SELECT DISTINCT employee_code
    FROM dbpcm_warehouse.punch_pair
    WHERE punch_date >= addMonths(today(), -{win})
  )
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN e.employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN e.employee_code END ASC
```

**Uses:**

- `dbpcm_warehouse.expense.employee_code`
- `dbpcm_warehouse.expense.expense_paid_date`
- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-expenses-unreconciled-to-payroll

**ID:** `bp-expenses-unreconciled-to-payroll`

**Intent:** Paid expenses that lack a payroll transaction tie for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  expense_status AS "Expense Status",
  count() AS "Line Count",
  round(toFloat64(SUM(amount_approved)), 2) AS "Total Amount"
FROM dbpcm_warehouse.expense
WHERE expense_status = 'Paid'
  AND payroll_transaction_number = ''
  AND expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
GROUP BY expense_status
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Amount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Amount" END ASC
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.expense.payroll_transaction_number`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-mileage-summary

**ID:** `bp-mileage-summary`

**Intent:** Total approved mileage reimbursement and miles for a payment period

**Slots:**

- `period` (period, required) — binds: `dbpcm_warehouse.expense.expense_paid_date`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  round(toFloat64(SUM(amount_approved)), 2) AS "Approved Amount",
  round(toFloat64(SUM(approved_miles)), 2) AS "Approved Miles",
  round(toFloat64(SUM(submitted_miles)), 2) AS "Submitted Miles"
FROM dbpcm_warehouse.expense
WHERE (submitted_miles > 0 OR approved_miles > 0)
  AND expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND expense_paid_date = {period}
```

**Uses:**

- `dbpcm_warehouse.expense.approved_miles`
- `dbpcm_warehouse.expense.submitted_miles`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-missing-receipts

**ID:** `bp-missing-receipts`

**Intent:** Missing receipt count and share by expense rule for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  expense_rule AS "Expense Rule",
  any(expense_rule_description) AS "Rule Description",
  countIf(receipt_attached = 'No') AS "Missing Receipts",
  round(toFloat64(countIf(receipt_attached = 'No')) / nullIf(count(), 0), 4) AS "Missing Share"
FROM dbpcm_warehouse.expense
WHERE expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
GROUP BY expense_rule
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Missing Receipts" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Missing Receipts" END ASC, "Expense Rule"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.expense.receipt_attached`
- `dbpcm_warehouse.expense.expense_rule`
- `dbpcm_warehouse.expense.expense_rule_description`
- `dbpcm_warehouse.expense.expense_paid_date`

---

### Blueprint: bp-top-expense-spenders

**ID:** `bp-top-expense-spenders`

**Intent:** Top N employees by approved expense spend for a payment date window

**Slots:**

- `top_n` (positive_integer, required)
- `expense_window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  e.employee_code AS "Employee Code",
  any(e.employee_name) AS "Employee Name",
  round(toFloat64(SUM(x.amount_approved)), 2) AS "Approved Spend"
FROM dbpcm_warehouse.expense AS x
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = x.employee_code
WHERE x.expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND x.expense_paid_date >= {expense_window_start}
  AND x.expense_paid_date <= {expense_window_end}
GROUP BY e.employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Approved Spend" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Approved Spend" END ASC, "Employee Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.expense.employee_code`
- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

## Headcount

### Blueprint: bp-active-headcount-by-department

**ID:** `bp-active-headcount-by-department`

**Intent:** Employee headcount by department, including departments with no matching employees. Defaults to Active + On Leave; employee_status selects another status or All to remove the filter.

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.department.department_name`
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT
    d.department_code AS department_code,
    d.department_name AS department,
    coalesce(e.active_count, 0) AS headcount
FROM dbpcm_warehouse.department AS d
LEFT JOIN (
    SELECT department_code, uniqExact(employee_code) AS active_count
    FROM dbpcm_warehouse.employee
    WHERE (employee_status = {employee_status} OR {employee_status} = 'All')
    GROUP BY department_code
) AS e ON d.department_code = e.department_code
WHERE d.department_name = {department}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN headcount END DESC, CASE WHEN {order_direction} = 'ASC' THEN headcount END ASC, department, department_code
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.department.department_code`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-age-band-distribution

**ID:** `bp-age-band-distribution`

**Intent:** Active employee headcount by age band with average and median workforce age, computed from date of birth at query time

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT multiIf(age < 25, 'Under 25',
               age < 35, '25-34',
               age < 45, '35-44',
               age < 55, '45-54',
               age < 65, '55-64',
               '65+') AS "Age Band",
       count(DISTINCT employee_code) AS "Headcount",
       round(avg(age), 1) AS "Avg Age In Band",
       round((quantileExactLow(0.5)(toFloat64(age)) + quantileExactHigh(0.5)(toFloat64(age))) / 2, 1) AS "Median Age In Band"
FROM (
       SELECT employee_code,
              department_name,
              date_diff('year', date_of_birth, now()) AS age
       FROM dbpcm_warehouse.employee
       WHERE employee_status = 'Active'
         AND date_of_birth IS NOT NULL
         AND toDate32(date_of_birth) > toDate32('1900-01-01')
         AND department_name = {department}
     )
GROUP BY "Age Band"
ORDER BY min(age) ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.date_of_birth`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-department-directory

**ID:** `bp-department-directory`

**Intent:** Complete department dictionary with codes, names, active headcount and total headcount including zero-employee departments

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT d.department_code AS "Department Code",
       d.department_name AS "Department",
       countIf(e.employee_status = 'Active') AS "Active Headcount",
       count(DISTINCT e.employee_code) AS "Total Headcount"
FROM dbpcm_warehouse.department AS d
LEFT JOIN dbpcm_warehouse.employee AS e ON e.department_code = d.department_code
GROUP BY d.department_code, d.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Active Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Active Headcount" END ASC, d.department_name
```

**Uses:**

- `dbpcm_warehouse.department.department_code`
- `dbpcm_warehouse.department.department_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.department_code`

---

### Blueprint: bp-department-label-audit

**ID:** `bp-department-label-audit`

**Intent:** Department label normalization audit showing employee department names not present in the official department dimension with affected employee counts

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS "Department Name",
       count(DISTINCT e.employee_code) AS "Affected Employees"
FROM dbpcm_warehouse.employee AS e
LEFT JOIN dbpcm_warehouse.department AS d ON d.department_name = e.department_name
WHERE d.department_name IS NULL
  AND e.department_name IS NOT NULL
  AND e.department_name != ''
GROUP BY e.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Affected Employees" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Affected Employees" END ASC, e.department_name
```

**Uses:**

- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-department-scorecard

**ID:** `bp-department-scorecard`

**Intent:** Per-department KPI scorecard covering active headcount, pay, tenure, demographics and long-running leaves

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT department_name AS "Department",
       countIf(employee_status = 'Active') AS "Active Headcount",
       round(avgIf(toFloat64(annual_salary), employee_status = 'Active'), 2) AS "Avg Salary (Active)",
       round(avgIf(date_diff('day', hire_date, now()) / 365.25, employee_status = 'Active'), 1) AS "Avg Tenure Years (Active)",
       round(100 * countIf(employee_status = 'Active' AND gender = 'Female') / nullIf(countIf(employee_status = 'Active'), 0), 2) AS "Pct Female (Active)",
       round(100 * countIf(employee_status = 'Active' AND direct_deposit = 1) / nullIf(countIf(employee_status = 'Active'), 0), 2) AS "Pct Direct Deposit (Active)",
       countIf(employee_status = 'On Leave') AS "On Leave",
       countIf(employee_status = 'On Leave'
               AND leave_absence_start IS NOT NULL AND toDate32(leave_absence_start) > toDate32('1900-01-01')
               AND NOT (leave_absence_end IS NOT NULL AND toDate32(leave_absence_end) > toDate32('1900-01-01'))
               AND leave_absence_start < now() - INTERVAL 1 YEAR) AS "On Leave Over 1 Year No End"
FROM dbpcm_warehouse.employee
WHERE department_name = {department}
GROUP BY department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Department" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Department" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.gender`
- `dbpcm_warehouse.employee.direct_deposit`
- `dbpcm_warehouse.employee.leave_absence_start`
- `dbpcm_warehouse.employee.leave_absence_end`

---

### Blueprint: bp-dol-status-per-work-location

**ID:** `bp-dol-status-per-work-location`

**Intent:** Pivoted Full Time / Part Time / Seasonal / None active counts per work location

**Slots:**

- `location` (string, optional) — binds: `dbpcm_warehouse.employee.work_location_description`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT work_location_description AS "Work Location",
       countIf(dol_status_description = 'Full Time') AS "Full Time",
       countIf(dol_status_description = 'Part Time') AS "Part Time",
       countIf(dol_status_description = 'Seasonal') AS "Seasonal",
       countIf(dol_status_description = 'None') AS "None",
       count(*) AS "Total"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND work_location_description = {location}
GROUP BY work_location_description
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.work_location_description`
- `dbpcm_warehouse.employee.dol_status_description`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-headcount-by-dol-status

**ID:** `bp-headcount-by-dol-status`

**Intent:** Active headcount by DOL status (Full/Part-time DOL status) from employee.dol_status_description

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT dol_status_description AS "DOL Status",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
GROUP BY "DOL Status"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Headcount" END ASC, "DOL Status"
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.dol_status_description`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-headcount-by-hire-era

**ID:** `bp-headcount-by-hire-era`

**Intent:** Active headcount and average tenure split by hire era around a supplied window

**Slots:**

- `era_window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT multiIf(hire_date < {era_window_start}, 'Pre Era',
               hire_date < {era_window_end}, 'Era',
               'Post Era') AS "Hire Era",
       count(DISTINCT employee_code) AS "Headcount",
       round(avg(date_diff('day', hire_date, now()) / 365.25), 1) AS "Avg Tenure Years"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
GROUP BY "Hire Era"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Hire Era" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Hire Era" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-headcount-by-pay-frequency

**ID:** `bp-headcount-by-pay-frequency`

**Intent:** Active employee headcount grouped by pay frequency schedule with optional department filter

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT ifNull(pay_frequency, 'Unspecified') AS "Pay Frequency",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
GROUP BY pay_frequency
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Headcount" END ASC, "Pay Frequency"
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.pay_frequency`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-headcount-by-union

**ID:** `bp-headcount-by-union`

**Intent:** Headcount of employees by union membership — break down the workforce by union code affiliation (labor-relations lens)

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT ifNull(nullIf(union_code, ''), 'Unspecified') AS "Union Code",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
GROUP BY "Union Code"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Headcount" END ASC, "Union Code"
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.union_code`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-headcount-by-work-city-country

**ID:** `bp-headcount-by-work-city-country`

**Intent:** Active headcount by work-location country, state and city

**Slots:**

- `country` (string, optional) — binds: `dbpcm_warehouse.employee.work_location_country`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT work_location_country AS "Country",
       work_location_state AS "State",
       work_location_city AS "City",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND work_location_country = {country}
GROUP BY work_location_country, work_location_state, work_location_city
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Headcount" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.work_location_country`
- `dbpcm_warehouse.employee.work_location_state`
- `dbpcm_warehouse.employee.work_location_city`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-headcount-by-works-in-state

**ID:** `bp-headcount-by-works-in-state`

**Intent:** Active headcount per works-in state with a lives-elsewhere breakdown

**Slots:**

- `work_state` (string, optional) — binds: `dbpcm_warehouse.employee.works_in_state`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT works_in_state AS "Works In State",
       count(DISTINCT employee_code) AS "Headcount",
       countIf(lives_in_state IS NOT NULL AND lives_in_state != '' AND lives_in_state != works_in_state) AS "Lives Elsewhere"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND works_in_state = {work_state}
GROUP BY works_in_state
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Headcount" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.works_in_state`
- `dbpcm_warehouse.employee.lives_in_state`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-headcount-growth-rate

**ID:** `bp-headcount-growth-rate`

**Intent:** Monthly hires, terminations and net headcount delta over the last N months

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(m.month_start, '%Y-%m') AS "Month",
       countIf(e.most_recent_hire_date >= m.month_start
               AND e.most_recent_hire_date <= m.month_end
               AND e.most_recent_hire_date IS NOT NULL
               AND toDate32(e.most_recent_hire_date) > toDate32('1900-01-01')) AS "Hires",
       countIf(e.termination_date >= m.month_start
               AND e.termination_date <= m.month_end
               AND e.termination_date IS NOT NULL
               AND toDate32(e.termination_date) > toDate32('1900-01-01')
               AND (e.most_recent_hire_date IS NULL OR e.most_recent_hire_date < e.termination_date)) AS "Terms",
       countIf(e.most_recent_hire_date >= m.month_start
               AND e.most_recent_hire_date <= m.month_end
               AND e.most_recent_hire_date IS NOT NULL
               AND toDate32(e.most_recent_hire_date) > toDate32('1900-01-01'))
       - countIf(e.termination_date >= m.month_start
                 AND e.termination_date <= m.month_end
                 AND e.termination_date IS NOT NULL
                 AND toDate32(e.termination_date) > toDate32('1900-01-01')
                 AND (e.most_recent_hire_date IS NULL OR e.most_recent_hire_date < e.termination_date)) AS "Net Delta"
FROM (
       SELECT toStartOfMonth(addMonths(toStartOfMonth((SELECT max(most_recent_hire_date) FROM dbpcm_warehouse.employee WHERE toDate32(most_recent_hire_date) > toDate32('1900-01-01'))), 1) - INTERVAL 1 DAY - INTERVAL toInt32(number) MONTH) AS month_start,
              toStartOfMonth(addMonths(toStartOfMonth((SELECT max(most_recent_hire_date) FROM dbpcm_warehouse.employee WHERE toDate32(most_recent_hire_date) > toDate32('1900-01-01'))), 1) - INTERVAL 1 DAY - INTERVAL toInt32(number) MONTH + INTERVAL 1 MONTH) - INTERVAL 1 DAY AS month_end
        FROM (SELECT arrayJoin(range(0, toInt32({win}))) AS number) AS _seq
      ) AS m
CROSS JOIN dbpcm_warehouse.employee AS e
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.most_recent_hire_date`
- `dbpcm_warehouse.employee.termination_date`

---

### Blueprint: bp-headcount-monthly-trend

**ID:** `bp-headcount-monthly-trend`

**Intent:** End-of-month active headcount and monthly termination trend reconstructed from hire and termination dates over the last N months

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(toStartOfMonth(month_end), '%Y-%m') AS "Month",
       countIf(hire_date <= month_end
               AND (termination_date IS NULL
                    OR toDate32(termination_date) <= toDate32('1900-01-01')
                    OR termination_date > month_end)
              ) AS "Active Headcount",
       countIf(termination_date >= toStartOfMonth(month_end)
               AND termination_date <= month_end
               AND termination_date IS NOT NULL
               AND toDate32(termination_date) > toDate32('1900-01-01')
               AND (most_recent_hire_date IS NULL OR most_recent_hire_date < termination_date)
              ) AS "Terms In Month"
FROM (
       SELECT addMonths(toLastDayOfMonth((SELECT max(hire_date) FROM dbpcm_warehouse.employee)), -toInt32(number)) AS month_end
        FROM (SELECT arrayJoin(range(0, toInt32({win}))) AS number) AS _seq
      ) AS months
CROSS JOIN dbpcm_warehouse.employee AS e
WHERE hire_date IS NOT NULL
  AND toDate32(hire_date) > toDate32('1900-01-01')
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`

---

### Blueprint: bp-labor-allocation-coverage

**ID:** `bp-labor-allocation-coverage`

**Intent:** Coverage audit comparing active employees with and without labor allocation key 1 populated

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT 'Set' AS "Mapping Status",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND labor_allocation_key_1 IS NOT NULL
  AND labor_allocation_key_1 != ''
UNION ALL
SELECT 'Not Set' AS "Mapping Status",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND (labor_allocation_key_1 IS NULL OR labor_allocation_key_1 = '')
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Headcount" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.labor_allocation_key_1`

---

### Blueprint: bp-labor-allocation-dictionary

**ID:** `bp-labor-allocation-dictionary`

**Intent:** Distinct labor allocation codes and descriptions for a chosen slot ranked by active employee usage count

**Slots:**

- `slot` (positive_integer, required)
- `top_n` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT la.code AS "Allocation Code",
       la.description AS "Allocation Description",
       count(DISTINCT e.employee_code) AS "Usage Count"
FROM (
       SELECT employee_code,
              employee_status,
              CASE {slot}
                WHEN 1 THEN labor_allocation_key_1
                WHEN 2 THEN labor_allocation_key_2
                WHEN 3 THEN labor_allocation_key_3
                WHEN 4 THEN labor_allocation_key_4
                WHEN 5 THEN labor_allocation_key_5
                WHEN 6 THEN labor_allocation_key_6
                WHEN 7 THEN labor_allocation_key_7
                WHEN 8 THEN labor_allocation_key_8
                WHEN 9 THEN labor_allocation_key_9
                WHEN 10 THEN labor_allocation_key_10
                WHEN 11 THEN labor_allocation_key_11
                WHEN 12 THEN labor_allocation_key_12
                WHEN 13 THEN labor_allocation_key_13
                WHEN 14 THEN labor_allocation_key_14
                WHEN 15 THEN labor_allocation_key_15
                WHEN 16 THEN labor_allocation_key_16
                WHEN 17 THEN labor_allocation_key_17
                WHEN 18 THEN labor_allocation_key_18
                WHEN 19 THEN labor_allocation_key_19
                ELSE labor_allocation_key_20
              END AS selected_key
       FROM dbpcm_warehouse.employee
     ) AS e
LEFT JOIN dbpcm_warehouse.labor_allocation AS la ON la.join_key = e.selected_key
WHERE e.employee_status = 'Active'
  AND e.selected_key IS NOT NULL
  AND e.selected_key != ''
GROUP BY la.code, la.description
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Usage Count" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Usage Count" END ASC, "Allocation Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.labor_allocation_key_1`
- `dbpcm_warehouse.employee.labor_allocation_key_2`
- `dbpcm_warehouse.employee.labor_allocation_key_3`
- `dbpcm_warehouse.employee.labor_allocation_key_4`
- `dbpcm_warehouse.employee.labor_allocation_key_5`
- `dbpcm_warehouse.employee.labor_allocation_key_6`
- `dbpcm_warehouse.employee.labor_allocation_key_7`
- `dbpcm_warehouse.employee.labor_allocation_key_8`
- `dbpcm_warehouse.employee.labor_allocation_key_9`
- `dbpcm_warehouse.employee.labor_allocation_key_10`
- `dbpcm_warehouse.employee.labor_allocation_key_11`
- `dbpcm_warehouse.employee.labor_allocation_key_12`
- `dbpcm_warehouse.employee.labor_allocation_key_13`
- `dbpcm_warehouse.employee.labor_allocation_key_14`
- `dbpcm_warehouse.employee.labor_allocation_key_15`
- `dbpcm_warehouse.employee.labor_allocation_key_16`
- `dbpcm_warehouse.employee.labor_allocation_key_17`
- `dbpcm_warehouse.employee.labor_allocation_key_18`
- `dbpcm_warehouse.employee.labor_allocation_key_19`
- `dbpcm_warehouse.employee.labor_allocation_key_20`
- `dbpcm_warehouse.labor_allocation.join_key`
- `dbpcm_warehouse.labor_allocation.code`
- `dbpcm_warehouse.labor_allocation.description`

---

### Blueprint: bp-labor-allocation-resolution

**ID:** `bp-labor-allocation-resolution`

**Intent:** Resolve labor allocation slots 1-3 to codes and descriptions for active employees

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.employee_code AS "Employee Code",
       e.department_name AS "Department",
       la1.code AS "Allocation 1 Code",
       la1.description AS "Allocation 1 Description",
       la2.code AS "Allocation 2 Code",
       la2.description AS "Allocation 2 Description",
       la3.code AS "Allocation 3 Code",
       la3.description AS "Allocation 3 Description",
       toUInt8( (e.labor_allocation_key_1 IS NULL OR e.labor_allocation_key_1 = '')
             OR (e.labor_allocation_key_2 IS NULL OR e.labor_allocation_key_2 = '')
             OR (e.labor_allocation_key_3 IS NULL OR e.labor_allocation_key_3 = '') ) AS "Unallocated"
FROM dbpcm_warehouse.employee AS e
LEFT JOIN dbpcm_warehouse.labor_allocation AS la1 ON e.labor_allocation_key_1 = la1.join_key
LEFT JOIN dbpcm_warehouse.labor_allocation AS la2 ON e.labor_allocation_key_2 = la2.join_key
LEFT JOIN dbpcm_warehouse.labor_allocation AS la3 ON e.labor_allocation_key_3 = la3.join_key
WHERE e.employee_status = 'Active'
  AND e.department_name = {department}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN e.employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN e.employee_code END ASC LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.labor_allocation_key_1`
- `dbpcm_warehouse.employee.labor_allocation_key_2`
- `dbpcm_warehouse.employee.labor_allocation_key_3`
- `dbpcm_warehouse.labor_allocation.join_key`
- `dbpcm_warehouse.labor_allocation.code`
- `dbpcm_warehouse.labor_allocation.description`

---

### Blueprint: bp-labor-allocation-rollup

**ID:** `bp-labor-allocation-rollup`

**Intent:** Active headcount and average salary grouped by a chosen labor allocation slot value, ranked by headcount

**Slots:**

- `slot` (positive_integer, required)
- `top_n` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT la.code AS "Allocation Code",
       la.description AS "Allocation Description",
       count(DISTINCT e.employee_code) AS "Active Headcount",
       round(avgIf(toFloat64(e.annual_salary), e.annual_salary IS NOT NULL AND toFloat64(e.annual_salary) > 0), 2) AS "Avg Salary"
FROM (
       SELECT employee_code,
              employee_status,
              annual_salary,
              CASE {slot}
                WHEN 1 THEN labor_allocation_key_1
                WHEN 2 THEN labor_allocation_key_2
                WHEN 3 THEN labor_allocation_key_3
                WHEN 4 THEN labor_allocation_key_4
                WHEN 5 THEN labor_allocation_key_5
                WHEN 6 THEN labor_allocation_key_6
                WHEN 7 THEN labor_allocation_key_7
                WHEN 8 THEN labor_allocation_key_8
                WHEN 9 THEN labor_allocation_key_9
                WHEN 10 THEN labor_allocation_key_10
                WHEN 11 THEN labor_allocation_key_11
                WHEN 12 THEN labor_allocation_key_12
                WHEN 13 THEN labor_allocation_key_13
                WHEN 14 THEN labor_allocation_key_14
                WHEN 15 THEN labor_allocation_key_15
                WHEN 16 THEN labor_allocation_key_16
                WHEN 17 THEN labor_allocation_key_17
                WHEN 18 THEN labor_allocation_key_18
                WHEN 19 THEN labor_allocation_key_19
                ELSE labor_allocation_key_20
              END AS selected_key
       FROM dbpcm_warehouse.employee
     ) AS e
LEFT JOIN dbpcm_warehouse.labor_allocation AS la ON la.join_key = e.selected_key
WHERE e.employee_status = 'Active'
  AND e.selected_key IS NOT NULL
  AND e.selected_key != ''
GROUP BY la.code, la.description
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Active Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Active Headcount" END ASC, "Allocation Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.labor_allocation_key_1`
- `dbpcm_warehouse.employee.labor_allocation_key_2`
- `dbpcm_warehouse.employee.labor_allocation_key_3`
- `dbpcm_warehouse.employee.labor_allocation_key_4`
- `dbpcm_warehouse.employee.labor_allocation_key_5`
- `dbpcm_warehouse.employee.labor_allocation_key_6`
- `dbpcm_warehouse.employee.labor_allocation_key_7`
- `dbpcm_warehouse.employee.labor_allocation_key_8`
- `dbpcm_warehouse.employee.labor_allocation_key_9`
- `dbpcm_warehouse.employee.labor_allocation_key_10`
- `dbpcm_warehouse.employee.labor_allocation_key_11`
- `dbpcm_warehouse.employee.labor_allocation_key_12`
- `dbpcm_warehouse.employee.labor_allocation_key_13`
- `dbpcm_warehouse.employee.labor_allocation_key_14`
- `dbpcm_warehouse.employee.labor_allocation_key_15`
- `dbpcm_warehouse.employee.labor_allocation_key_16`
- `dbpcm_warehouse.employee.labor_allocation_key_17`
- `dbpcm_warehouse.employee.labor_allocation_key_18`
- `dbpcm_warehouse.employee.labor_allocation_key_19`
- `dbpcm_warehouse.employee.labor_allocation_key_20`
- `dbpcm_warehouse.labor_allocation.join_key`
- `dbpcm_warehouse.labor_allocation.code`
- `dbpcm_warehouse.labor_allocation.description`

---

### Blueprint: bp-manager-span-of-control

**ID:** `bp-manager-span-of-control`

**Intent:** Top-N primary supervisors by number of active direct reports

**Slots:**

- `top_n` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT primary_supervisor_ee_code AS "Manager Code",
       any(primary_supervisor_name) AS "Manager",
       count(DISTINCT employee_code) AS "Direct Reports"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND primary_supervisor_ee_code IS NOT NULL
  AND primary_supervisor_ee_code != ''
GROUP BY primary_supervisor_ee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Direct Reports" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Direct Reports" END ASC, "Manager Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.primary_supervisor_ee_code`
- `dbpcm_warehouse.employee.primary_supervisor_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-records-per-status

**ID:** `bp-records-per-status`

**Intent:** Record counts per employee status including Not Hired records

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_status AS "Employee Status",
       count() AS "Records"
FROM dbpcm_warehouse.employee
WHERE department_name = {department}
GROUP BY employee_status
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Records" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Records" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-supervisor-chain-depth

**ID:** `bp-supervisor-chain-depth`

**Intent:** Distribution of populated supervisor-level counts (0-4) across active employees

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT toString(depth) AS "Chain Depth",
       count() AS "Headcount",
       round(100 * count() / sum(count()) OVER (), 2) AS "Share Pct"
FROM (
       SELECT employee_code,
              (primary_supervisor_ee_code IS NOT NULL AND primary_supervisor_ee_code != '') +
              (secondary_supervisor_ee_code IS NOT NULL AND secondary_supervisor_ee_code != '') +
              (tertiary_supervisor_ee_code IS NOT NULL AND tertiary_supervisor_ee_code != '') +
              (quaternary_supervisor_ee_code IS NOT NULL AND quaternary_supervisor_ee_code != '') AS depth
       FROM dbpcm_warehouse.employee
       WHERE employee_status = 'Active'
     )
GROUP BY depth
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN depth END DESC, CASE WHEN {order_direction} = 'ASC' THEN depth END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.primary_supervisor_ee_code`
- `dbpcm_warehouse.employee.secondary_supervisor_ee_code`
- `dbpcm_warehouse.employee.tertiary_supervisor_ee_code`
- `dbpcm_warehouse.employee.quaternary_supervisor_ee_code`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-tenure-band-distribution

**ID:** `bp-tenure-band-distribution`

**Intent:** Active headcount by seniority-date tenure band

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT multiIf(
         NOT (seniority_date IS NOT NULL AND toDate32(seniority_date) > toDate32('1900-01-01')), 'Unknown',
         yrs < 1, 'Under 1 Year',
         yrs < 3, '1-3 Years',
         yrs < 5, '3-5 Years',
         yrs < 10, '5-10 Years',
         yrs < 20, '10-20 Years',
         '20+ Years') AS "Tenure Band",
       count(DISTINCT employee_code) AS "Headcount"
FROM (
       SELECT employee_code,
              seniority_date,
              date_diff('day', seniority_date, now()) / 365.25 AS yrs
       FROM dbpcm_warehouse.employee
       WHERE employee_status = 'Active'
         AND department_name = {department}
     )
GROUP BY "Tenure Band"
ORDER BY min(yrs) ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.seniority_date`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-top-home-zip-city

**ID:** `bp-top-home-zip-city`

**Intent:** Top-N employee home zip codes by active headcount

**Slots:**

- `top_n` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT zip_code AS "Zip Code",
       any(city) AS "City",
       any(state) AS "State",
       count(DISTINCT employee_code) AS "Headcount"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND zip_code IS NOT NULL
  AND zip_code != ''
GROUP BY zip_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Headcount" END ASC, "Zip Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.zip_code`
- `dbpcm_warehouse.employee.city`
- `dbpcm_warehouse.employee.state`
- `dbpcm_warehouse.employee.employee_status`

---

## Payroll

### Blueprint: bp-adjustment-line-volume

**ID:** `bp-adjustment-line-volume`

**Intent:** Volume of negative payroll register earnings lines per month, counting adjustment and reversal activity over the last N pay periods

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(toStartOfMonth(pay_period_end_date), '%Y-%m') AS "Month",
       count() AS "Adjustment Lines",
       round(SUM(amount), 2) AS "Adjustment Amount"
FROM dbpcm_warehouse.payroll
WHERE register_type = 'EARN'
  AND amount < 0
  AND pay_period_end_date >= addMonths((SELECT max(pay_period_end_date) FROM dbpcm_warehouse.payroll), -{win})
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-category-by-department

**ID:** `bp-category-by-department`

**Intent:** Earnings dollars by department for a specific earning category or all categories combined, for a punch date window, ranked by department

**Slots:**

- `concept` (string, optional) — binds: `dbpcm_warehouse.earning_category.category_name`
- `period` (period_range, required)
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**Catalog SHA:** `e76a795fec9b621b6998872cc6455a6314363ccd`

**SQL Template:**

```sql
SELECT e.department_name AS `Department`,
       round(SUM(te.earn_amount), 2) AS `Total Dollars`
FROM (SELECT employee_code, punch_date, earn_amount, earn_code FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = te.employee_code
WHERE ec.category_name = {concept}
  AND te.punch_date >= toDate({period_start}) AND te.punch_date < toDate({period_end})
GROUP BY e.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Total Dollars` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Total Dollars` END ASC, `Department`
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-category-earnings-per-employee

**ID:** `bp-category-earnings-per-employee`

**Intent:** Per-employee earnings dollars for a specific earning category over a date window, including all employees with matching earnings, ranked by amount

**Slots:**

- `concept` (string, required) — binds: `dbpcm_warehouse.earning_category.category_name`
- `window` (period_range, required)
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT te.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       any(e.department_name) AS "Department",
       round(SUM(te.earn_amount), 2) AS "Total Dollars"
FROM (SELECT employee_code, punch_date, earn_amount, earn_code FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
LEFT JOIN dbpcm_warehouse.employee AS e ON e.employee_code = te.employee_code
WHERE ec.category_name = {concept}
  AND te.punch_date BETWEEN {window_start} AND {window_end}
GROUP BY te.employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Dollars" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Dollars" END ASC, "Employee Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-category-hours-monthly

**ID:** `bp-category-hours-monthly`

**Intent:** Monthly trend of paid hours for a specific earning category (such as Sick or Vacation) from timecard earnings, bucketed by punch date month

**Slots:**

- `concept` (string, required) — binds: `dbpcm_warehouse.earning_category.category_name`
- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(toStartOfMonth(te.punch_date), '%Y-%m') AS "Month",
       round(SUM(te.earn_hours), 2) AS "Total Hours"
FROM (SELECT punch_date, earn_code, earn_hours FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
WHERE ec.category_name = {concept}
  AND te.punch_date >= addMonths((SELECT max(punch_date) FROM dbpcm_warehouse.total_earning), -{win})
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`

---

### Blueprint: bp-category-mom-volatility

**ID:** `bp-category-mom-volatility`

**Intent:** Month-over-month earnings dollar volatility by earning category, comparing the last two completed punch-date months via adjacent month self-join

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH monthly AS (
  SELECT toStartOfMonth(te.punch_date) AS month,
         ifNull(ec.category_name, 'Uncategorized') AS category,
         SUM(te.earn_amount) AS dollars
  FROM (SELECT punch_date, earn_code, earn_amount FROM dbpcm_warehouse.total_earning) AS te
  LEFT JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
  WHERE te.punch_date >= addMonths((SELECT max(punch_date) FROM dbpcm_warehouse.total_earning), -2)
  GROUP BY toStartOfMonth(te.punch_date), ifNull(ec.category_name, 'Uncategorized')
)
SELECT curr.category AS "Category",
       round(curr.dollars, 2) AS "Current Month Dollars",
       round(prior.dollars, 2) AS "Prior Month Dollars",
       round((curr.dollars - prior.dollars), 2) AS "Dollar Change",
       round((curr.dollars - prior.dollars) * 100.0 / nullIf(prior.dollars, 0), 2) AS "Pct Change"
FROM monthly AS curr
JOIN monthly AS prior ON curr.category = prior.category AND prior.month = date_add(month, -1, curr.month)
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN abs(round((curr.dollars - prior.dollars) * 100.0 / nullIf(prior.dollars, 0), 2)) END DESC, CASE WHEN {order_direction} = 'ASC' THEN abs(round((curr.dollars - prior.dollars) * 100.0 / nullIf(prior.dollars, 0), 2)) END ASC, "Category"
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`

---

### Blueprint: bp-category-spend-over-threshold

**ID:** `bp-category-spend-over-threshold`

**Intent:** Employees whose earnings in a specific category exceed a dollar threshold over a date window, from timecard earnings

**Slots:**

- `concept` (string, required) — binds: `dbpcm_warehouse.earning_category.category_name`
- `threshold` (positive_integer, required)
- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT te.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       any(e.department_name) AS "Department",
       round(SUM(te.earn_amount), 2) AS "Total Dollars"
FROM (SELECT employee_code, punch_date, earn_amount, earn_code FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
LEFT JOIN dbpcm_warehouse.employee AS e ON e.employee_code = te.employee_code
WHERE ec.category_name = {concept}
  AND te.punch_date BETWEEN {window_start} AND {window_end}
GROUP BY te.employee_code
HAVING SUM(te.earn_amount) > {threshold}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Dollars" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Dollars" END ASC, "Employee Code"
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-compare-employee-check-detail-two-periods

**ID:** `bp-compare-employee-check-detail-two-periods`

**Intent:** Compare a single employee's two paychecks line-item by line-item across two pay periods, with per-item deltas

**Slots:**

- `employee` (string, required) — binds: `dbpcm_warehouse.payroll.employee_code`
- `period_a` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `period_b` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`

**Status:** validated (drift: clean)

**SQL Template:**

```sql

```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.type_code_description`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.type_hours`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-compare-employee-paychecks-two-periods

**ID:** `bp-compare-employee-paychecks-two-periods`

**Intent:** Compare a single employee's paychecks line-item by line-item across two given pay periods, with per-item amounts, hours, and period-over-period change

**Slots:**

- `employee` (string, required) — binds: `dbpcm_warehouse.payroll.employee_code`
- `period_a` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `period_b` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT toString(register_type) AS "Pay Category",
       toString(type_code) AS "Pay Code",
       any(toString(type_code_description)) AS "Description",
       round(toFloat64(sumIf(amount, pay_period_end_date = {period_b})), 2) AS "Recent Amount",
       round(toFloat64(sumIf(type_hours, pay_period_end_date = {period_b})), 2) AS "Recent Hours",
       round(toFloat64(sumIf(amount, pay_period_end_date = {period_a})), 2) AS "Prior Amount",
       round(toFloat64(sumIf(type_hours, pay_period_end_date = {period_a})), 2) AS "Prior Hours",
       round(toFloat64(sumIf(amount, pay_period_end_date = {period_b}) - sumIf(amount, pay_period_end_date = {period_a})), 2) AS "Amount Change",
       round(toFloat64(sumIf(type_hours, pay_period_end_date = {period_b}) - sumIf(type_hours, pay_period_end_date = {period_a})), 2) AS "Hours Change"
FROM dbpcm_warehouse.payroll
WHERE employee_code = {employee}
  AND pay_period_end_date IN ({period_a}, {period_b})
GROUP BY register_type, type_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN toString(register_type) END DESC, CASE WHEN {order_direction} = 'ASC' THEN toString(register_type) END ASC, toString(type_code)
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.type_code_description`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.type_hours`

---

### Blueprint: bp-deduction-breakdown

**ID:** `bp-deduction-breakdown`

**Intent:** Total payroll deductions and breakdown by deduction type code for a pay period, from register deduction lines

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT type_code AS "Deduction Code",
       round(SUM(amount), 2) AS "Total Deductions"
FROM dbpcm_warehouse.payroll
WHERE register_type = 'DDUCT'
  AND pay_period_end_date = {period}
GROUP BY type_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Deductions" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Deductions" END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-department-ot-share

**ID:** `bp-department-ot-share`

**Intent:** Department overtime dollars as a percentage of department gross payroll earnings, ranked by share, combining timecard overtime with register gross

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH
  ot_agg AS (
      SELECT e.department_name AS dept,
             SUM(te.earn_amount) AS ot_dollars
      FROM (SELECT employee_code, punch_date, earn_code, earn_amount FROM dbpcm_warehouse.total_earning) AS te
      JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
      JOIN dbpcm_warehouse.employee AS e ON e.employee_code = te.employee_code
      WHERE ec.category_name = 'Overtime'
        AND te.punch_date BETWEEN addMonths(toDate({period}), -1) AND toDate({period})
      GROUP BY e.department_name
  ),
  gross_agg AS (
      SELECT e2.department_name AS dept,
             SUM(p2.amount) AS gross_dollars
      FROM dbpcm_warehouse.payroll AS p2
      JOIN dbpcm_warehouse.employee AS e2 ON e2.employee_code = p2.employee_code
      WHERE p2.register_type = 'EARN'
        AND p2.pay_period_end_date = {period}
      GROUP BY e2.department_name
  )
SELECT oa.dept AS "Department",
       round(oa.ot_dollars, 2) AS "Overtime Dollars",
       round(ga.gross_dollars, 2) AS "Gross Earnings",
       round(oa.ot_dollars / nullIf(ga.gross_dollars, 0) * 100, 2) AS "OT Share %"
FROM ot_agg AS oa
LEFT JOIN gross_agg AS ga ON ga.dept = oa.dept
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "OT Share %" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "OT Share %" END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-earning-category-dictionary

**ID:** `bp-earning-category-dictionary`

**Intent:** Full earning category dimension mapping with each earn code and its assigned category label, zero-slot static lookup

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `e76a795fec9b621b6998872cc6455a6314363ccd`

**SQL Template:**

```sql
SELECT earn_code AS `Earn Code`,
       category_name AS `Category`
FROM dbpcm_warehouse.earning_category
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN category_name END DESC, CASE WHEN {order_direction} = 'ASC' THEN category_name END ASC, earn_code
```

**Uses:**

- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`

---

### Blueprint: bp-earnings-by-category

**ID:** `bp-earnings-by-category`

**Intent:** Total timecard earnings and paid hours for a specific earning category resolved via the earning category dimension, for a punch date window

**Slots:**

- `concept` (string, required) — binds: `dbpcm_warehouse.earning_category.category_name`
- `period` (period_range, required)

**Status:** validated (drift: clean)

**Catalog SHA:** `e76a795fec9b621b6998872cc6455a6314363ccd`

**SQL Template:**

```sql
SELECT round(SUM(te.earn_amount), 2) AS `Total Earnings`,
       round(SUM(te.earn_hours), 2) AS `Total Hours`
FROM (SELECT punch_date, earn_amount, earn_hours, earn_code FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
WHERE ec.category_name = {concept}
  AND te.punch_date >= toDate({period_start}) AND te.punch_date < toDate({period_end})
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`

---

### Blueprint: bp-earnings-by-department-via-scratch-join

**ID:** `bp-earnings-by-department-via-scratch-join`

**Intent:** Total earnings by department, materializing per-employee earnings and joining departments

**Slots:**

- `department` (string, required) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.

**Status:** validated (drift: clean)

**SQL Template:**

```sql

```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-earnings-by-dol-status

**ID:** `bp-earnings-by-dol-status`

**Intent:** Gross earnings by employee DOL status for a pay period, joining the current employee roster

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.dol_status_description AS "DOL Status",
       round(SUM(p.amount), 2) AS "Gross Earnings"
FROM dbpcm_warehouse.payroll AS p
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code
WHERE p.register_type = 'EARN'
  AND p.pay_period_end_date = {period}
GROUP BY e.dol_status_description
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Gross Earnings" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Gross Earnings" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.dol_status_description`

---

### Blueprint: bp-earnings-by-pay-basis

**ID:** `bp-earnings-by-pay-basis`

**Intent:** Gross earnings by employee pay basis for a pay period, joining the current employee roster

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.pay_basis AS "Pay Basis",
       round(SUM(p.amount), 2) AS "Gross Earnings"
FROM dbpcm_warehouse.payroll AS p
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code
WHERE p.register_type = 'EARN'
  AND p.pay_period_end_date = {period}
GROUP BY e.pay_basis
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Gross Earnings" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Gross Earnings" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.pay_basis`

---

### Blueprint: bp-earnings-by-work-state

**ID:** `bp-earnings-by-work-state`

**Intent:** Gross earnings by employee works-in-state for a pay period, with optional state filter, reflecting tax-situs work location

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `state` (string, optional) — binds: `dbpcm_warehouse.employee.works_in_state`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.works_in_state AS "Work State",
       round(SUM(p.amount), 2) AS "Gross Earnings"
FROM dbpcm_warehouse.payroll AS p
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code
WHERE p.register_type = 'EARN'
  AND p.pay_period_end_date = {period}
  AND e.works_in_state = {state}
GROUP BY e.works_in_state
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Gross Earnings" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Gross Earnings" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.works_in_state`

---

### Blueprint: bp-earnings-category-mix

**ID:** `bp-earnings-category-mix`

**Intent:** Category mix showing percent share of total earnings dollars and paid hours by earning category for a punch date window, preserving uncategorized rows

**Slots:**

- `period` (period_range, required)
- `metric` (enum, required) — values: `dollars`, `hours`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**Catalog SHA:** `e76a795fec9b621b6998872cc6455a6314363ccd`

**SQL Template:**

```sql
WITH totals AS (
  SELECT SUM(earn_amount) AS all_dollars,
         SUM(earn_hours) AS all_hours
  FROM dbpcm_warehouse.total_earning
  WHERE punch_date >= toDate({period_start}) AND punch_date < toDate({period_end})
)
SELECT ifNull(ec.category_name, 'Uncategorized') AS `Category`,
       round(SUM(te.earn_amount), 2) AS `Total Dollars`,
       round(SUM(te.earn_hours), 2) AS `Total Hours`,
       round(SUM(te.earn_amount) * 100.0 / nullIf((SELECT all_dollars FROM totals), 0), 2) AS `Dollar Pct`,
       round(SUM(te.earn_hours) * 100.0 / nullIf((SELECT all_hours FROM totals), 0), 2) AS `Hours Pct`
FROM (SELECT punch_date, earn_amount, earn_hours, earn_code FROM dbpcm_warehouse.total_earning) AS te
LEFT JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
WHERE te.punch_date >= toDate({period_start}) AND te.punch_date < toDate({period_end})
  AND ({metric} = 'dollars' OR {metric} = 'hours')
GROUP BY ifNull(ec.category_name, 'Uncategorized')
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Dollar Pct` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Dollar Pct` END ASC, `Category`
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`

---

### Blueprint: bp-earnings-category-trend

**ID:** `bp-earnings-category-trend`

**Intent:** Monthly trend of earnings dollars for a specific earning category over the last N months, bucketed by punch date month

**Slots:**

- `concept` (string, required) — binds: `dbpcm_warehouse.earning_category.category_name`
- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(toStartOfMonth(te.punch_date), '%Y-%m') AS "Month",
       round(SUM(te.earn_amount), 2) AS "Total Dollars"
FROM (SELECT punch_date, earn_amount, earn_code FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
WHERE ec.category_name = {concept}
  AND te.punch_date >= addMonths((SELECT max(punch_date) FROM dbpcm_warehouse.total_earning), -{win})
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`

---

### Blueprint: bp-earnings-monthly-trend

**ID:** `bp-earnings-monthly-trend`

**Intent:** Monthly trend of gross or net payroll earnings over the last N completed pay periods, bucketed by pay-period end month

**Slots:**

- `win` (relative_window, required)
- `register` (enum, required) — values: `EARN`, `NETPAYDIST`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(toStartOfMonth(pay_period_end_date), '%Y-%m') AS "Month",
       round(SUM(amount), 2) AS "Total Earnings"
FROM dbpcm_warehouse.payroll
WHERE register_type = {register}
  AND pay_period_end_date >= addMonths((SELECT max(pay_period_end_date) FROM dbpcm_warehouse.payroll), -{win})
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.payroll.register_type`

---

### Blueprint: bp-earnings-quarter-to-date

**ID:** `bp-earnings-quarter-to-date`

**Intent:** Quarter-to-date gross and net earnings from the payroll register, matching pay periods that fall within the selected calendar quarter

**Slots:**

- `quarter` (period_range, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT round(SUMIf(amount, register_type = 'EARN'), 2) AS "Gross Earnings",
       round(SUMIf(amount, register_type = 'NETPAYDIST'), 2) AS "Net Pay"
FROM dbpcm_warehouse.payroll
WHERE pay_period_start_date >= {quarter_start}
  AND pay_period_end_date <= {quarter_end}
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_start_date`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-employer-tax-summary

**ID:** `bp-employer-tax-summary`

**Intent:** Total employer payroll tax and breakdown by tax type code for a pay period, from payroll register employer-tax lines

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT type_code AS "Tax Code",
       round(SUM(amount), 2) AS "Total Employer Tax"
FROM dbpcm_warehouse.payroll
WHERE register_type = 'ERTAX'
  AND pay_period_end_date = {period}
GROUP BY type_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Employer Tax" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Employer Tax" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-fica-medicare-totals

**ID:** `bp-fica-medicare-totals`

**Intent:** Combined employee and employer tax totals with type-code breakdown for a pay period, showing both employee-tax and employer-tax register lines

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT type_code AS "Tax Code",
       register_type AS "Tax Side",
       round(SUM(amount), 2) AS "Total Tax"
FROM dbpcm_warehouse.payroll
WHERE register_type IN ('EETAX', 'ERTAX')
  AND pay_period_end_date = {period}
GROUP BY type_code, register_type
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Tax" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Tax" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-gross-earnings-period-compare

**ID:** `bp-gross-earnings-period-compare`

**Intent:** Compare gross earnings between two pay periods company-wide or filtered by employee home department

**Slots:**

- `period_a` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `period_b` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `dept` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT pay_period_end_date AS "Period",
       round(SUM(p.amount), 2) AS "Gross Earnings"
FROM dbpcm_warehouse.payroll AS p
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code
WHERE p.register_type = 'EARN'
  AND p.pay_period_end_date IN ({period_a}, {period_b})
  AND e.department_name = {dept}
GROUP BY pay_period_end_date
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Period" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Period" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-gross-earnings-summary

**ID:** `bp-gross-earnings-summary`

**Intent:** Total gross earnings for a single pay period, summed from payroll register earnings lines by pay-period bounds

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT round(SUM(amount), 2) AS "Total Gross Earnings"
FROM dbpcm_warehouse.payroll
WHERE register_type = 'EARN'
  AND pay_period_end_date = {period}
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-gross-wages-date-window

**ID:** `bp-gross-wages-date-window`

**Intent:** Gross wages from payroll register for a custom date window, matching pay periods that overlap the requested date range

**Slots:**

- `window` (period_range, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT round(SUM(amount), 2) AS "Total Gross Wages"
FROM dbpcm_warehouse.payroll
WHERE register_type = 'EARN'
  AND pay_period_start_date <= {window_end}
  AND pay_period_end_date >= {window_start}
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_start_date`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-net-pay-summary

**ID:** `bp-net-pay-summary`

**Intent:** Total net pay distributed for a single pay period or date window, from payroll register net-pay-distribution lines by pay-period bounds

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT round(SUM(amount), 2) AS "Total Net Pay"
FROM dbpcm_warehouse.payroll
WHERE register_type = 'NETPAYDIST'
  AND pay_period_end_date = {period}
```

**Uses:**

- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-orphan-register-codes

**ID:** `bp-orphan-register-codes`

**Intent:** Payroll register employee codes that have no match in the employee roster, a data-quality orphan audit

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT p.employee_code AS "Employee Code",
       count() AS "Register Lines"
FROM dbpcm_warehouse.payroll AS p
WHERE p.employee_code NOT IN (
    SELECT DISTINCT employee_code
    FROM dbpcm_warehouse.employee
  )
  AND p.pay_period_end_date = {period}
GROUP BY p.employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Register Lines" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Register Lines" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.employee.employee_code`

---


### Blueprint: bp-ot-two-basis-tieout

**ID:** `bp-ot-two-basis-tieout`

**Intent:** Reconcile payroll register overtime dollars against timecard overtime category dollars per pay period per employee, with variance and percentage difference

**Slots:**

- `period` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH period_bounds AS (
  SELECT any(pay_period_start_date) AS start_dt,
         any(pay_period_end_date)   AS end_dt
  FROM dbpcm_warehouse.payroll
  WHERE pay_period_end_date = {period}
),
register_ot AS (
  SELECT employee_code,
         round(toFloat64(SUM(amount)), 2) AS reg_amount
  FROM dbpcm_warehouse.payroll
  WHERE register_type = 'EARN'
    AND type_code IN {ot_type_codes}
    AND pay_period_end_date = {period}
  GROUP BY employee_code
),
te_ot AS (
  SELECT te.employee_code,
         round(toFloat64(SUM(te.earn_amount)), 2) AS te_amount
  FROM (SELECT employee_code, punch_date, earn_amount, earn_code FROM dbpcm_warehouse.total_earning) AS te
  CROSS JOIN period_bounds pb
  WHERE te.earn_code IN {ot_earn_codes}
    AND te.punch_date BETWEEN pb.start_dt AND pb.end_dt
  GROUP BY te.employee_code
)
SELECT COALESCE(r.employee_code, t.employee_code) AS "Employee Code",
       max(e.employee_name) AS "Employee Name",
       {period} AS "Pay Period End",
       round(COALESCE(r.reg_amount, 0), 2) AS "Register OT",
       round(COALESCE(t.te_amount, 0), 2) AS "Timecard OT",
       round(COALESCE(t.te_amount, 0) - COALESCE(r.reg_amount, 0), 2) AS "Difference",
       round(abs(COALESCE(t.te_amount, 0) - COALESCE(r.reg_amount, 0))
         / nullIf(COALESCE(r.reg_amount, 0), 0) * 100, 2) AS "Pct Diff"
FROM register_ot r
FULL OUTER JOIN te_ot t
  ON t.employee_code = r.employee_code
LEFT JOIN dbpcm_warehouse.employee e
  ON e.employee_code = COALESCE(r.employee_code, t.employee_code)
GROUP BY COALESCE(r.employee_code, t.employee_code), {period}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN abs(COALESCE(t.te_amount, 0) - COALESCE(r.reg_amount, 0)) END DESC, CASE WHEN {order_direction} = 'ASC' THEN abs(COALESCE(t.te_amount, 0) - COALESCE(r.reg_amount, 0)) END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.pay_period_start_date`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-overtime-by-department

**ID:** `bp-overtime-by-department`

**Intent:** Earnings by department for a given pay period, filtered to a specific pay type (type_code) such as overtime

**Slots:**

- `department` (string, required) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `pay_period` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `type_code` (string, required) — binds: `dbpcm_warehouse.payroll.type_code`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS department, SUM(p.amount) AS earnings
FROM dbpcm_warehouse.payroll AS p
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code
WHERE p.register_type = 'EARN'
  AND p.type_code = {type_code}
  AND e.department_name = {department}
  AND p.pay_period_end_date = {pay_period}
GROUP BY e.department_name
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-overtime-hours-dollars

**ID:** `bp-overtime-hours-dollars`

**Intent:** Overtime dollars and hours by department from timecard earnings, resolved via the Overtime earning category, for a date window

**Slots:**

- `period` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS "Department",
       round(SUM(te.earn_amount), 2) AS "Overtime Dollars",
       round(SUM(te.earn_hours), 2) AS "Overtime Hours"
FROM (SELECT punch_date, earn_amount, earn_hours, earn_code, employee_code FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = te.employee_code
WHERE ec.category_name = 'Overtime'
  AND te.punch_date BETWEEN {period_start} AND {period_end}
GROUP BY e.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Overtime Dollars" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Overtime Dollars" END ASC
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-paid-hours-summary

**ID:** `bp-paid-hours-summary`

**Intent:** Total paid hours from the payroll register for a pay period, optionally filtered by employee home department

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `dept` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT round(SUM(p.type_hours), 2) AS "Total Paid Hours"
FROM dbpcm_warehouse.payroll AS p
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code
WHERE p.register_type = 'EARN'
  AND p.pay_period_end_date = {period}
  AND e.department_name = {dept}
```

**Uses:**

- `dbpcm_warehouse.payroll.type_hours`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-payroll-codes-unmapped

**ID:** `bp-payroll-codes-unmapped`

**Intent:** Payroll register earning type codes that have no corresponding mapping in the earning category dimension, flagged as uncategorized for timecard mapping

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT DISTINCT p.type_code AS "Type Code"
FROM dbpcm_warehouse.payroll AS p
LEFT JOIN dbpcm_warehouse.earning_category AS ec ON ec.earn_code = p.type_code
WHERE p.register_type = 'EARN'
  AND p.pay_period_end_date = {period}
  AND ec.earn_code IS NULL
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Type Code" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Type Code" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.earning_category.earn_code`

---

### Blueprint: bp-payroll-duplicate-line-audit

**ID:** `bp-payroll-duplicate-line-audit`

**Intent:** Candidate duplicate payroll register lines grouped by employee, period, register type, type code, and amount, with occurrence counts

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       pay_period_end_date AS "Pay Period End",
       register_type AS "Register Type",
       type_code AS "Type Code",
       amount AS "Amount",
       count() AS "Duplicate Count"
FROM dbpcm_warehouse.payroll
WHERE pay_period_end_date = {period}
GROUP BY employee_code, pay_period_end_date, register_type, type_code, amount
HAVING count() > 1
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Duplicate Count" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Duplicate Count" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-payroll-line-counts

**ID:** `bp-payroll-line-counts`

**Intent:** Count of payroll register lines and distinct employee-check combinations for a pay period

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT count() AS "Register Lines",
       uniqExact(employee_code, pay_period_end_date) AS "Distinct Checks"
FROM dbpcm_warehouse.payroll
WHERE pay_period_end_date = {period}
```

**Uses:**

- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-payroll-setup-gap-scorecard

**ID:** `bp-payroll-setup-gap-scorecard`

**Intent:** Missing-payroll-setup flags and gap count per active employee

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       toUInt8(badge_number IS NULL OR badge_number = '') AS "No Badge",
       toUInt8(employee_gl_code IS NULL OR employee_gl_code = '') AS "No GL Code",
       toUInt8(paycode_profile_code IS NULL OR paycode_profile_code = '') AS "No Paycode Profile",
       toUInt8(earning_profile IS NULL OR earning_profile = '') AS "No Earning Profile",
       toUInt8(pay_class IS NULL OR pay_class = '') AS "No Pay Class",
       toUInt8(pay_frequency IS NULL OR pay_frequency = '') AS "No Pay Frequency",
       toUInt8(schedule_group IS NULL OR schedule_group = '') AS "No Schedule Group",
       (badge_number IS NULL OR badge_number = '') +
       (employee_gl_code IS NULL OR employee_gl_code = '') +
       (paycode_profile_code IS NULL OR paycode_profile_code = '') +
       (earning_profile IS NULL OR earning_profile = '') +
       (pay_class IS NULL OR pay_class = '') +
       (pay_frequency IS NULL OR pay_frequency = '') +
       (schedule_group IS NULL OR schedule_group = '') AS "Gap Count"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
  AND ( (badge_number IS NULL OR badge_number = '') +
        (employee_gl_code IS NULL OR employee_gl_code = '') +
        (paycode_profile_code IS NULL OR paycode_profile_code = '') +
        (earning_profile IS NULL OR earning_profile = '') +
        (pay_class IS NULL OR pay_class = '') +
        (pay_frequency IS NULL OR pay_frequency = '') +
        (schedule_group IS NULL OR schedule_group = '') ) > 0
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Gap Count" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Gap Count" END ASC, employee_code
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.badge_number`
- `dbpcm_warehouse.employee.employee_gl_code`
- `dbpcm_warehouse.employee.paycode_profile_code`
- `dbpcm_warehouse.employee.earning_profile`
- `dbpcm_warehouse.employee.pay_class`
- `dbpcm_warehouse.employee.pay_frequency`
- `dbpcm_warehouse.employee.schedule_group`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-payroll-summary

**ID:** `bp-payroll-summary`

**Intent:** Payroll amount, source line count and distinct employee count by company, department or pay-date month. Required register_type enum and half-open pay-date period [start,end); select EARN for gross earnings. Optional type_code, employee status, department and employee. Earnings represent finalized total_earning after period close; use total_earning for in-progress earnings and never add both for the same covered earnings. Preaggregate payroll per employee/month before LEFT JOIN; unmatched employees retained unless employee filters supplied. Does not prove complete months or zero-fill missing months; no implicit earnings filter.

**Slots:**

- `group_by` (enum, required) — values: `company`, `department`, `month`
- `register_type` (enum, required) — values: `EARN`, `EETAX`, `DDUCT`, `NETPAYDIST`, `EEBEN`, `ERTAX`; binds: `dbpcm_warehouse.payroll.register_type`
- `period` (period_range, required)
- `type_code` (entity, optional) — binds: `dbpcm_warehouse.payroll.type_code`
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`; binds: `dbpcm_warehouse.employee.employee_status`
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
WITH p AS (
 SELECT employee_code, substring(toString(pay_date), 1, 7) AS month,
 sum(amount) AS amount, count(*) AS line_count
 FROM dbpcm_warehouse.payroll
 WHERE register_type = {register_type} AND type_code = {type_code}
 AND toDate(pay_date) >= toDate({period_start}) AND toDate(pay_date) < toDate({period_end})
 GROUP BY employee_code, substring(toString(pay_date), 1, 7)
)
SELECT CASE {group_by} WHEN 'department' THEN e.department_name WHEN 'month' THEN toString(p.month) ELSE 'Company' END AS `Group`, sum(CAST(p.amount AS Nullable(Decimal(38, 2)))) AS `Payroll Amount`,
 sum(CAST(p.line_count AS Nullable(Decimal(20, 0)))) AS `Source Line Count`, uniqExact(p.employee_code) AS `Employee Count`
FROM p LEFT JOIN dbpcm_warehouse.employee e ON p.employee_code = e.employee_code
WHERE e.employee_status = {employee_status} AND e.department_name = {department} AND e.employee_code = {employee}
GROUP BY CASE {group_by} WHEN 'department' THEN e.department_name WHEN 'month' THEN toString(p.month) ELSE 'Company' END
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Group` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Group` END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_date`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-placeholder-category-audit

**ID:** `bp-placeholder-category-audit`

**Intent:** Total timecard earnings dollars and paid hours flowing through the Select Category placeholder bucket for a punch date window

**Slots:**

- `window` (period_range, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT count(DISTINCT te.earn_code) AS "Distinct Earn Codes",
       round(SUM(te.earn_amount), 2) AS "Total Dollars",
       round(SUM(te.earn_hours), 2) AS "Total Hours"
FROM (SELECT punch_date, earn_amount, earn_hours, earn_code FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
WHERE ec.category_name = 'Select Category'
  AND te.punch_date BETWEEN {window_start} AND {window_end}
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`

---

### Blueprint: bp-post-termination-pay-audit

**ID:** `bp-post-termination-pay-audit`

**Intent:** Terminated employees who received payroll earnings after their termination date, rehire-aware, for a given pay-date window

**Slots:**

- `pay_window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT p.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       count() AS "Register Lines",
       round(SUM(p.amount), 2) AS "Total Earnings",
       any(e.termination_date) AS "Termination Date",
       max(p.pay_date) AS "Last Pay Date"
FROM dbpcm_warehouse.payroll AS p
JOIN dbpcm_warehouse.employee AS e
  ON e.employee_code = p.employee_code
WHERE p.register_type = 'EARN'
  AND p.pay_date >= {pay_window_start}
  AND p.pay_date <= {pay_window_end}
  AND e.termination_date IS NOT NULL
  AND toDate32(e.termination_date) > toDate32('1900-01-01')
  AND p.pay_date > e.termination_date
  AND (e.most_recent_hire_date IS NULL
       OR toDate32(e.most_recent_hire_date) <= toDate32('1900-01-01')
       OR e.most_recent_hire_date <= e.termination_date)
GROUP BY p.employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Earnings" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Earnings" END ASC
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_date`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`

---

### Blueprint: bp-register-type-breakdown

**ID:** `bp-register-type-breakdown`

**Intent:** Split of the payroll register across register types for a pay period — EARN earnings, EETAX employee taxes, DDUCT deductions, EEBEN benefits, ERTAX employer taxes, NETPAYDIST net pay — showing dollars by register type

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT register_type AS "Register Type",
       round(SUM(amount), 2) AS "Total Amount"
FROM dbpcm_warehouse.payroll
WHERE pay_period_end_date = {period}
GROUP BY register_type
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Amount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Amount" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-regular-vs-premium-hours-ratio

**ID:** `bp-regular-vs-premium-hours-ratio`

**Intent:** Monthly regular versus premium paid hours ratio from timecard earnings, bucketed by punch date month, showing regular share and premium share

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(toStartOfMonth(te.punch_date), '%Y-%m') AS "Month",
       round(SUMIf(te.earn_hours, ec.category_name = 'Regular'), 2) AS "Regular Hours",
       round(SUMIf(te.earn_hours, ec.category_name != 'Regular' OR ec.category_name IS NULL), 2) AS "Premium Hours",
       round(SUMIf(te.earn_hours, ec.category_name = 'Regular') * 100.0 / nullIf(SUM(te.earn_hours), 0), 2) AS "Regular Pct"
FROM (SELECT punch_date, earn_hours, earn_code FROM dbpcm_warehouse.total_earning) AS te
LEFT JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
WHERE te.punch_date >= addMonths((SELECT max(punch_date) FROM dbpcm_warehouse.total_earning), -{win})
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`

---

### Blueprint: bp-timecard-earnings-all-history

**ID:** `bp-timecard-earnings-all-history`

**Intent:** All-history timecard earnings and hours per earn code from total_earning, spanning all available punch dates, with optional employee or date-window filter

**Slots:**

- `ee` (string, optional) — binds: `dbpcm_warehouse.total_earning.employee_code`
- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT earn_code AS "Earn Code",
       round(SUM(earn_amount), 2) AS "Total Earnings",
       round(SUM(earn_hours), 2) AS "Total Hours"
FROM dbpcm_warehouse.total_earning
WHERE employee_code = {ee}
  AND punch_date BETWEEN {window_start} AND {window_end}
GROUP BY earn_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Earnings" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Earnings" END ASC
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.employee_code`

---

### Blueprint: bp-timecard-earnings-summary

**ID:** `bp-timecard-earnings-summary`

**Intent:** Total timecard earnings, paid hours, entry counts and employee counts by company, employee, allocated department, day, month or earning code. Supports per-person hours and pay in a period and monthly labor-cost trends. Uses daily total_earning entries: in-progress earnings finalized and moved to payroll after period close. Use payroll earnings for finalized periods. Do not add both lifecycle stages for the same earnings. This template does not infer period-close status or switch sources. Amount is not net pay, annual salary or proof of payment. Optional earning_code is client-defined: resolve regular/overtime concepts, never hardcode a universal OT code. No average-rate calculation across unlike earning codes. Required half-open punch-date period and resolved table_source prevent accidental cross-source double counting. FINAL deduplicates storage versions, not overlapping real-world events. Department means recorded labor-allocation department code, not current home department. No scheduled shift, tardiness or overtime policy is inferred. Observed first/last dates do not prove completeness; missing days/months are not zero-filled.

**Slots:**

- `group_by` (enum, required) — values: `company`, `employee`, `department`, `day`, `month`, `earning_code`
- `period` (period_range, required)
- `source` (entity, required) — binds: `dbpcm_warehouse.total_earning.table_source`
- `employee` (entity, optional) — binds: `dbpcm_warehouse.total_earning.employee_code`
- `department` (entity, optional) — binds: `dbpcm_warehouse.total_earning.department_code`
- `earning_code` (entity, optional) — binds: `dbpcm_warehouse.total_earning.earn_code`
- `pay_class` (entity, optional) — binds: `dbpcm_warehouse.total_earning.pay_class_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT CASE {group_by} WHEN 'employee' THEN employee_code WHEN 'department' THEN department_code WHEN 'day' THEN toString(punch_date) WHEN 'month' THEN substring(toString(punch_date),1,7) WHEN 'earning_code' THEN earn_code ELSE 'Company' END AS `Group`,
sum(earn_amount) AS `Timecard Earning Amount`, sum(earn_hours) AS `Paid Hours`, count(*) AS `Entry Count`, count(DISTINCT employee_code) AS `Employee Count`, count(DISTINCT punch_date) AS `Observed Days`, min(punch_date) AS `First Observed Date`, max(punch_date) AS `Last Observed Date`
FROM dbpcm_warehouse.total_earning
WHERE punch_date >= toDate({period_start}) AND punch_date < toDate({period_end}) AND table_source = {source} AND employee_code = {employee} AND department_code = {department} AND earn_code = {earning_code} AND pay_class_code = {pay_class}
GROUP BY CASE {group_by} WHEN 'employee' THEN employee_code WHEN 'department' THEN department_code WHEN 'day' THEN toString(punch_date) WHEN 'month' THEN substring(toString(punch_date),1,7) WHEN 'earning_code' THEN earn_code ELSE 'Company' END ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Group` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Group` END ASC
```

**Uses:**

- `dbpcm_warehouse.total_earning.department_code`
- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.pay_class_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.table_source`

---

### Blueprint: bp-timecard-vs-register-recon

**ID:** `bp-timecard-vs-register-recon`

**Intent:** Reconcile total_earning timecard hours against payroll register paid hours per employee for a single pay period, showing variance and percentage difference

**Slots:**

- `period` (period, required) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH period_bounds AS (
  SELECT any(pay_period_start_date) AS start_dt,
         any(pay_period_end_date)   AS end_dt
  FROM dbpcm_warehouse.payroll
  WHERE pay_period_end_date = {period}
),
register_hours AS (
  SELECT employee_code,
         SUM(type_hours) AS register_hours
  FROM dbpcm_warehouse.payroll
  WHERE register_type = 'EARN'
    AND pay_period_end_date = {period}
  GROUP BY employee_code
),
te_hours AS (
  SELECT te.employee_code,
         SUM(te.earn_hours) AS te_hours
  FROM (SELECT employee_code, punch_date, earn_hours FROM dbpcm_warehouse.total_earning) AS te
  CROSS JOIN period_bounds pb
  WHERE te.punch_date BETWEEN pb.start_dt AND pb.end_dt
  GROUP BY te.employee_code
)
SELECT COALESCE(r.employee_code, t.employee_code) AS "Employee Code",
       max(e.employee_name) AS "Employee Name",
       {period} AS "Pay Period End",
       round(COALESCE(r.register_hours, 0), 2) AS "Register Hours",
       round(COALESCE(t.te_hours, 0), 2) AS "Timecard Hours",
       round(COALESCE(t.te_hours, 0) - COALESCE(r.register_hours, 0), 2) AS "Difference",
       round(abs(COALESCE(t.te_hours, 0) - COALESCE(r.register_hours, 0))
         / nullIf(COALESCE(r.register_hours, 0), 0) * 100, 2) AS "Pct Diff"
FROM register_hours r
FULL OUTER JOIN te_hours t
  ON t.employee_code = r.employee_code
LEFT JOIN dbpcm_warehouse.employee e
  ON e.employee_code = COALESCE(r.employee_code, t.employee_code)
GROUP BY COALESCE(r.employee_code, t.employee_code), {period}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN abs(COALESCE(t.te_hours, 0) - COALESCE(r.register_hours, 0)) END DESC, CASE WHEN {order_direction} = 'ASC' THEN abs(COALESCE(t.te_hours, 0) - COALESCE(r.register_hours, 0)) END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.pay_period_start_date`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.type_hours`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-top-codes-in-category

**ID:** `bp-top-codes-in-category`

**Intent:** Top-N earn codes by earnings dollars within a specific earning category, using earn descriptions for display labels

**Slots:**

- `concept` (string, required) — binds: `dbpcm_warehouse.earning_category.category_name`
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT te.earn_code AS "Earn Code",
       any(te.earn_code_description) AS "Description",
       round(SUM(te.earn_amount), 2) AS "Total Dollars",
       round(SUM(te.earn_hours), 2) AS "Total Hours"
 FROM (SELECT punch_date, earn_amount, earn_code, earn_code_description, earn_hours FROM dbpcm_warehouse.total_earning) AS te
JOIN (SELECT earn_code, category_name FROM dbpcm_warehouse.earning_category) AS ec ON ec.earn_code = te.earn_code
WHERE ec.category_name = {concept}
  AND te.punch_date >= '2023-01-01'
  AND te.punch_date <= '2026-12-31'
GROUP BY te.earn_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Dollars" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Dollars" END ASC, "Earn Code"
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.total_earning.earn_amount`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.total_earning.earn_code_description`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.earning_category.earn_code`
- `dbpcm_warehouse.earning_category.category_name`

---

### Blueprint: bp-total-earnings-by-department

**ID:** `bp-total-earnings-by-department`

**Intent:** Total earnings for a department, resolving the earnings register codes dynamically

**Slots:**

- `department` (string, required) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS department, SUM(p.amount) AS total_earnings
FROM dbpcm_warehouse.payroll AS p
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code
WHERE p.register_type IN {earn_codes}
  AND e.department_name = {department}
GROUP BY e.department_name
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-unpaid-active-employees

**ID:** `bp-unpaid-active-employees`

**Intent:** Active employees who have no payroll earnings register line in a given pay period, identifying unpaid active roster entries

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.employee_code AS "Employee Code",
       e.employee_name AS "Employee Name",
       e.department_name AS "Department"
FROM dbpcm_warehouse.employee AS e
WHERE e.employee_status = 'Active'
  AND e.employee_code NOT IN (
    SELECT DISTINCT p.employee_code
    FROM dbpcm_warehouse.payroll AS p
    WHERE p.register_type = 'EARN'
      AND p.pay_period_end_date = {period}
  )
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN e.employee_code END DESC, CASE WHEN {order_direction} = 'ASC' THEN e.employee_code END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-weekly-overtime-hours

**ID:** `bp-weekly-overtime-hours`

**Intent:** Overtime hours per employee per work week based on total_earning earn_hours, the authoritative hours source, with a configurable weekly threshold

**Slots:**

- `window` (period_range, required)
- `threshold` (positive_integer, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH weekly AS (
  SELECT employee_code,
         toMonday(punch_date) AS week_start,
         SUM(earn_hours) AS weekly_hours
  FROM (SELECT employee_code, punch_date, earn_hours FROM dbpcm_warehouse.total_earning)
  WHERE punch_date BETWEEN {window_start} AND {window_end}
  GROUP BY employee_code, toMonday(punch_date)
)
SELECT w.employee_code AS "Employee Code",
       max(e.employee_name) AS "Employee Name",
       w.week_start AS "Week Start",
       round(w.weekly_hours, 2) AS "Weekly Hours",
       round(greatest(w.weekly_hours - {threshold}, 0), 2) AS "Overtime Hours"
FROM weekly w
LEFT JOIN dbpcm_warehouse.employee e
  ON e.employee_code = w.employee_code
WHERE w.weekly_hours > {threshold}
GROUP BY w.employee_code, w.week_start
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Overtime Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Overtime Hours" END ASC, "Employee Code"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-ytd-earnings-per-employee

**ID:** `bp-ytd-earnings-per-employee`

**Intent:** Year-to-date gross earnings, net pay, and paid hours per employee from the payroll register, optionally filtered by employee or department

**Slots:**

- `ee` (string, optional) — binds: `dbpcm_warehouse.payroll.employee_code`
- `dept` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT p.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       round(SUMIf(p.amount, p.register_type = 'EARN'), 2) AS "Gross Earnings",
       round(SUMIf(p.amount, p.register_type = 'NETPAYDIST'), 2) AS "Net Pay",
       round(SUMIf(p.type_hours, p.register_type = 'EARN'), 2) AS "Paid Hours"
FROM dbpcm_warehouse.payroll AS p
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = p.employee_code
WHERE p.employee_code = {ee}
  AND e.department_name = {dept}
GROUP BY p.employee_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Gross Earnings" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Gross Earnings" END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.type_hours`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-zero-net-pay-employees

**ID:** `bp-zero-net-pay-employees`

**Intent:** Employees with exactly $0.00 net pay in a pay period, distinguishing those with no net-pay-distribution rows from those with a zero net-pay amount

**Slots:**

- `period` (period, optional) — binds: `dbpcm_warehouse.payroll.pay_period_end_date`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code"
FROM dbpcm_warehouse.payroll
WHERE pay_period_end_date = {period}
GROUP BY employee_code
HAVING SUMIf(amount, register_type = 'EARN') > 0
   AND SUMIf(amount, register_type = 'NETPAYDIST') = 0
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Employee Code" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Employee Code" END ASC
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

## Punches

### Blueprint: bp-avg-shift-length

**ID:** `bp-avg-shift-length`

**Intent:** Average shift duration in hours from In/Out punch timestamps per punch pair by employee home department, using punch_pair event data only

**Slots:**

- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS "Department",
       round(AVG(
         dateDiff('minute', pp.in_punch_time, pp.out_punch_time) / 60.0
       ), 2) AS "Avg Shift Hours",
       count() AS "Pair Count"
FROM (SELECT employee_code, punch_date, in_punch_time, out_punch_time FROM dbpcm_warehouse.punch_pair) AS pp
JOIN dbpcm_warehouse.employee e
  ON e.employee_code = pp.employee_code
WHERE pp.punch_date BETWEEN {window_start} AND {window_end}
  AND pp.in_punch_time IS NOT NULL
  AND toDate32(pp.in_punch_time) > toDate32('1900-01-01')
  AND pp.out_punch_time IS NOT NULL
  AND toDate32(pp.out_punch_time) > toDate32('1900-01-01')
GROUP BY e.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Avg Shift Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Avg Shift Hours" END ASC
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.in_punch_time`
- `dbpcm_warehouse.punch_pair.out_punch_time`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-break-gap-median

**ID:** `bp-break-gap-median`

**Intent:** Median break gap in hours between consecutive punch pairs on the same employee-day using punch_pair event timestamps only

**Slots:**

- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH ordered AS (
  SELECT employee_code,
         punch_date,
         in_punch_time,
         out_punch_time,
         toInt64(row_number() OVER (
           PARTITION BY employee_code, punch_date
           ORDER BY in_punch_time
         )) AS rn
  FROM (SELECT employee_code, punch_date, in_punch_time, out_punch_time FROM dbpcm_warehouse.punch_pair)
  WHERE punch_date BETWEEN {window_start} AND {window_end}
    AND in_punch_time IS NOT NULL
    AND toDate32(in_punch_time) > toDate32('1900-01-01')
    AND out_punch_time IS NOT NULL
    AND toDate32(out_punch_time) > toDate32('1900-01-01')
)
SELECT current.employee_code AS "Employee Code",
       current.punch_date AS "Punch Date",
       round(
         (
           (quantileExactLow(0.5)(
             dateDiff('minute', prev.out_punch_time, current.in_punch_time)
           ) + quantileExactHigh(0.5)(
             dateDiff('minute', prev.out_punch_time, current.in_punch_time)
           )) / 2
         ) / 60.0, 2
       ) AS "Median Break Gap Hours",
       count() AS "Gap Count"
FROM ordered current
JOIN ordered prev
  ON prev.employee_code = current.employee_code
  AND prev.punch_date = current.punch_date
  AND prev.rn = current.rn - 1
GROUP BY current.employee_code, current.punch_date
HAVING count() > 0
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Median Break Gap Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Median Break Gap Hours" END ASC, "Employee Code"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.punch_pair.in_punch_time`
- `dbpcm_warehouse.punch_pair.out_punch_time`

---

### Blueprint: bp-clock-time-profile

**ID:** `bp-clock-time-profile`

**Intent:** Time-of-day clock-in and clock-out distribution per employee home department using punch_pair event timestamps, showing typical shift start and end profiles

**Slots:**

- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS "Department",
       round(
         (quantileExactLow(0.5)(
           toHour(pp.in_punch_time) * 60 + toMinute(pp.in_punch_time)
         ) + quantileExactHigh(0.5)(
           toHour(pp.in_punch_time) * 60 + toMinute(pp.in_punch_time)
         )) / 2, 0
       ) AS "Median Clock-In Min",
       round(
         (quantileExactLow(0.5)(
           toHour(pp.out_punch_time) * 60 + toMinute(pp.out_punch_time)
         ) + quantileExactHigh(0.5)(
           toHour(pp.out_punch_time) * 60 + toMinute(pp.out_punch_time)
         )) / 2, 0
       ) AS "Median Clock-Out Min",
       count() AS "Pair Count"
FROM (SELECT employee_code, punch_date, in_punch_time, out_punch_time FROM dbpcm_warehouse.punch_pair) AS pp
JOIN dbpcm_warehouse.employee e
  ON e.employee_code = pp.employee_code
WHERE pp.punch_date BETWEEN {window_start} AND {window_end}
  AND pp.in_punch_time IS NOT NULL
  AND toDate32(pp.in_punch_time) > toDate32('1900-01-01')
  AND pp.out_punch_time IS NOT NULL
  AND toDate32(pp.out_punch_time) > toDate32('1900-01-01')
GROUP BY e.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Median Clock-In Min" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Median Clock-In Min" END ASC
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.in_punch_time`
- `dbpcm_warehouse.punch_pair.out_punch_time`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-consecutive-day-streaks

**ID:** `bp-consecutive-day-streaks`

**Intent:** Employees with N or more consecutive calendar days containing at least one punch pair, showing streak length and date range

**Slots:**

- `streak` (positive_integer, required)
- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH distinct_days AS (
  SELECT DISTINCT employee_code, punch_date
  FROM dbpcm_warehouse.punch_pair
  WHERE punch_date BETWEEN {window_start} AND {window_end}
),
streak_groups AS (
  SELECT employee_code,
         punch_date,
         toRelativeDayNum(punch_date)
           - row_number() OVER (PARTITION BY employee_code ORDER BY punch_date) AS grp
  FROM distinct_days
)
SELECT sg.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       min(sg.punch_date) AS "Streak Start",
       max(sg.punch_date) AS "Streak End",
       count() AS "Streak Length"
FROM streak_groups sg
LEFT JOIN dbpcm_warehouse.employee e
  ON e.employee_code = sg.employee_code
GROUP BY sg.employee_code, sg.grp
HAVING count() >= {streak}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Streak Length" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Streak Length" END ASC, "Employee Code"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-holiday-work-audit

**ID:** `bp-holiday-work-audit`

**Intent:** Total paid hours and employee counts on company-observed holiday dates from total_earning using runtime-resolved holiday earn codes

**Slots:**

- `concept` (string, required) — binds: `dbpcm_warehouse.total_earning.earn_code`
- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT te.punch_date AS "Punch Date",
       count(DISTINCT te.employee_code) AS "Employees",
       round(SUM(te.earn_hours), 2) AS "Total Hours",
       count() AS "Line Count"
FROM (SELECT employee_code, punch_date, earn_hours, earn_code FROM dbpcm_warehouse.total_earning) te
WHERE te.punch_date BETWEEN {window_start} AND {window_end}
  AND te.earn_code = {concept}
GROUP BY te.punch_date
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Hours" END ASC, "Punch Date"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-long-shift-days

**ID:** `bp-long-shift-days`

**Intent:** Employees whose total paid hours across all timecard entries in a single day exceed a configurable threshold, using total_ earning as the authoritative hours source, ranked by daily hours

**Slots:**

- `threshold` (positive_integer, required)
- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT te.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       te.punch_date AS "Punch Date",
       round(SUM(te.earn_hours), 2) AS "Daily Hours",
       count() AS "Line Count"
FROM (SELECT employee_code, punch_date, earn_hours FROM dbpcm_warehouse.total_earning) AS te
JOIN dbpcm_warehouse.employee e
  ON e.employee_code = te.employee_code
WHERE te.punch_date BETWEEN {window_start} AND {window_end}
GROUP BY te.employee_code, te.punch_date
HAVING SUM(te.earn_hours) > {threshold}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Daily Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Daily Hours" END ASC, "Employee Code"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-night-shift-share

**ID:** `bp-night-shift-share`

**Intent:** Night-shift hours share by department using total_earning earn_hours where the shift date is identified by In punch time between configurable night-start and night-end hours

**Slots:**

- `window` (period_range, required)
- `night_start` (positive_integer, required)
- `night_end` (positive_integer, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH night_dates AS (
  SELECT DISTINCT pp.employee_code, pp.punch_date
  FROM (SELECT employee_code, punch_date, in_punch_time FROM dbpcm_warehouse.punch_pair) pp
  WHERE pp.punch_date BETWEEN {window_start} AND {window_end}
    AND pp.in_punch_time IS NOT NULL
    AND toDate32(pp.in_punch_time) > toDate32('1900-01-01')
    AND (
      toHour(pp.in_punch_time) >= {night_start}
      OR toHour(pp.in_punch_time) < {night_end}
    )
),
dept_total AS (
  SELECT e.department_name AS dept,
         SUM(te.earn_hours) AS total_hours
  FROM (SELECT employee_code, punch_date, earn_hours FROM dbpcm_warehouse.total_earning) te
  JOIN dbpcm_warehouse.employee e
    ON e.employee_code = te.employee_code
  WHERE te.punch_date BETWEEN {window_start} AND {window_end}
  GROUP BY e.department_name
),
dept_night AS (
  SELECT e.department_name AS dept,
         SUM(te.earn_hours) AS night_hours
  FROM night_dates nd
  JOIN (SELECT employee_code, punch_date, earn_hours FROM dbpcm_warehouse.total_earning) te
    ON te.employee_code = nd.employee_code
    AND te.punch_date = nd.punch_date
  JOIN dbpcm_warehouse.employee e
    ON e.employee_code = te.employee_code
  GROUP BY e.department_name
)
SELECT COALESCE(d.dept, n.dept) AS "Department",
       round(COALESCE(d.total_hours, 0), 2) AS "Total Hours",
       round(COALESCE(n.night_hours, 0), 2) AS "Night Hours",
       round(
         100.0 * COALESCE(n.night_hours, 0)
         / nullIf(COALESCE(d.total_hours, 0), 0), 2
       ) AS "Night Share Pct"
FROM dept_total d
FULL OUTER JOIN dept_night n ON n.dept = d.dept
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Night Share Pct" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Night Share Pct" END ASC, "Department"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.in_punch_time`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-pairs-per-day-distribution

**ID:** `bp-pairs-per-day-distribution`

**Intent:** Distribution of punch pair counts per employee per day showing how many days had 1 pair, 2 pairs, or 3 or more pairs

**Slots:**

- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT ppd.ppd AS "Pairs Per Day",
       count() AS "Employee-Days"
FROM (
  SELECT employee_code,
         punch_date,
         CASE
           WHEN count() = 1 THEN '1 pair'
           WHEN count() = 2 THEN '2 pairs'
           ELSE '3+ pairs'
         END AS ppd
   FROM (SELECT employee_code, punch_date FROM dbpcm_warehouse.punch_pair)
  WHERE punch_date BETWEEN {window_start} AND {window_end}
  GROUP BY employee_code, punch_date
) AS ppd
GROUP BY ppd
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN CASE "Pairs Per Day"
    WHEN '1 pair' THEN 1
    WHEN '2 pairs' THEN 2
    ELSE 3
  END END DESC, CASE WHEN {order_direction} = 'ASC' THEN CASE "Pairs Per Day"
    WHEN '1 pair' THEN 1
    WHEN '2 pairs' THEN 2
    ELSE 3
  END END ASC
```

**Uses:**

- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.punch_date`

---

### Blueprint: bp-punch-anomalies

**ID:** `bp-punch-anomalies`

**Intent:** Matched In/Out punch pairs with a missing In punch or missing Out punch edge detected via sentinel timestamps

**Slots:**

- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT "Employee Code", "Employee Name", "Punch Date", "Anomaly Type"
FROM (
  SELECT pp.employee_code AS "Employee Code",
         any(e.employee_name) AS "Employee Name",
         pp.punch_date AS "Punch Date",
         'Missing Out Punch' AS "Anomaly Type"
  FROM (SELECT employee_code, punch_date, in_punch_time, out_punch_time FROM dbpcm_warehouse.punch_pair) AS pp
  LEFT JOIN dbpcm_warehouse.employee e
    ON e.employee_code = pp.employee_code
  WHERE pp.in_punch_time IS NOT NULL
    AND toDate32(pp.in_punch_time) > toDate32('1900-01-01')
    AND NOT (pp.out_punch_time IS NOT NULL AND toDate32(pp.out_punch_time) > toDate32('1900-01-01'))
    AND pp.punch_date BETWEEN {window_start} AND {window_end}
  GROUP BY pp.employee_code, pp.punch_date

  UNION ALL

  SELECT pp.employee_code AS "Employee Code",
          any(e.employee_name) AS "Employee Name",
          pp.punch_date AS "Punch Date",
          'Missing In Punch' AS "Anomaly Type"
  FROM (SELECT employee_code, punch_date, in_punch_time, out_punch_time FROM dbpcm_warehouse.punch_pair) AS pp
  LEFT JOIN dbpcm_warehouse.employee e
    ON e.employee_code = pp.employee_code
  WHERE pp.out_punch_time IS NOT NULL
    AND toDate32(pp.out_punch_time) > toDate32('1900-01-01')
    AND NOT (pp.in_punch_time IS NOT NULL AND toDate32(pp.in_punch_time) > toDate32('1900-01-01'))
    AND pp.punch_date BETWEEN {window_start} AND {window_end}
  GROUP BY pp.employee_code, pp.punch_date
) AS anomalies
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Punch Date" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Punch Date" END ASC, "Employee Code"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.punch_pair.in_punch_time`
- `dbpcm_warehouse.punch_pair.out_punch_time`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-punch-coverage-gap

**ID:** `bp-punch-coverage-gap`

**Intent:** Coverage metric showing the share of active employees who had at least one punch in the last N days, with counts of punchers and non-punchers

**Slots:**

- `win` (relative_window, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT count(DISTINCT e.employee_code) AS "Active Employees",
       count(DISTINCT pp.employee_code) AS "Employees With Punches",
       count(DISTINCT e.employee_code) - count(DISTINCT pp.employee_code) AS "Employees Without Punches",
       round(100.0 * count(DISTINCT pp.employee_code) / nullIf(count(DISTINCT e.employee_code), 0), 2) AS "Coverage Pct"
FROM dbpcm_warehouse.employee e
LEFT JOIN (
  SELECT DISTINCT employee_code
  FROM dbpcm_warehouse.punch_pair
  WHERE punch_date >= today() - {win}
) pp ON pp.employee_code = e.employee_code
WHERE e.employee_status = 'Active'
```

**Uses:**

- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-punch-pair-summary

**ID:** `bp-punch-pair-summary`

**Intent:** Matched punch-pair counts, distinct employees, observed days, paid hours and pair earning amount by company, employee, allocated department, day, month or hours type. Did this employee have punches in the period? Paid hours use earn_hours, not elapsed clock duration. Single-argument distinct counts. Required half-open punch-date period and resolved table_source prevent accidental cross-source double counting. FINAL deduplicates storage versions, not overlapping real-world events. Department means recorded labor-allocation department code, not current home department. No scheduled shift, tardiness or overtime policy is inferred. Observed first/last dates do not prove completeness; missing days/months are not zero-filled.

**Slots:**

- `group_by` (enum, required) — values: `company`, `employee`, `department`, `day`, `month`, `hours_type`
- `period` (period_range, required)
- `source` (entity, required) — binds: `dbpcm_warehouse.punch_pair.table_source`
- `employee` (entity, optional) — binds: `dbpcm_warehouse.punch_pair.employee_code`
- `department` (entity, optional) — binds: `dbpcm_warehouse.punch_pair.department_code`
- `hours_type` (entity, optional) — binds: `dbpcm_warehouse.punch_pair.hours_type`
- `pay_class` (entity, optional) — binds: `dbpcm_warehouse.punch_pair.pay_class_code`
- `in_type` (enum, optional) — values: `ID`, `IL`, `IB`, `IX`; binds: `dbpcm_warehouse.punch_pair.in_punch_type`
- `out_type` (enum, optional) — values: `OD`, `OL`, `OB`, `OX`; binds: `dbpcm_warehouse.punch_pair.out_punch_type`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT CASE {group_by} WHEN 'employee' THEN employee_code WHEN 'department' THEN department_code WHEN 'day' THEN toString(punch_date) WHEN 'month' THEN substring(toString(punch_date),1,7) WHEN 'hours_type' THEN hours_type ELSE 'Company' END AS `Group`,
count(*) AS `Pair Count`, count(DISTINCT employee_code) AS `Employee Count`, count(DISTINCT punch_date) AS `Observed Days`,
 sum(earn_hours) AS `Paid Hours`, sum(earn_amount) AS `Pair Earning Amount`, min(punch_date) AS `First Observed Date`, max(punch_date) AS `Last Observed Date`
FROM dbpcm_warehouse.punch_pair
WHERE punch_date >= toDate({period_start}) AND punch_date < toDate({period_end}) AND table_source = {source} AND employee_code = {employee} AND department_code = {department} AND hours_type = {hours_type} AND pay_class_code = {pay_class} AND in_punch_type = {in_type} AND out_punch_type = {out_type}
GROUP BY CASE {group_by} WHEN 'employee' THEN employee_code WHEN 'department' THEN department_code WHEN 'day' THEN toString(punch_date) WHEN 'month' THEN substring(toString(punch_date),1,7) WHEN 'hours_type' THEN hours_type ELSE 'Company' END ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Group` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Group` END ASC
```

**Uses:**

- `dbpcm_warehouse.punch_pair.department_code`
- `dbpcm_warehouse.punch_pair.earn_amount`
- `dbpcm_warehouse.punch_pair.earn_hours`
- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.hours_type`
- `dbpcm_warehouse.punch_pair.in_punch_type`
- `dbpcm_warehouse.punch_pair.out_punch_type`
- `dbpcm_warehouse.punch_pair.pay_class_code`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.punch_pair.table_source`

---

### Blueprint: bp-punch-plus-timeoff-overlap

**ID:** `bp-punch-plus-timeoff-overlap`

**Intent:** Employees with punch pair entries on days that also have approved time-off requests, a same-day double-dip audit with configurable overlap-day threshold

**Slots:**

- `window` (period_range, required)
- `threshold` (positive_integer, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH punch_days AS (
  SELECT DISTINCT employee_code,
         punch_date
  FROM dbpcm_warehouse.punch_pair
  WHERE punch_date BETWEEN {window_start} AND {window_end}
),
overlap AS (
  SELECT pd.employee_code,
         pd.punch_date AS overlap_date
  FROM punch_days pd
  JOIN dbpcm_warehouse.time_off_request tor
    ON tor.employee_code = pd.employee_code
  WHERE tor.status = 'Approved'
    AND toDate32(tor.request_date) = pd.punch_date
)
SELECT e.employee_code AS "Employee Code",
       e.employee_name AS "Employee Name",
       e.department_name AS "Department",
       count(DISTINCT o.overlap_date) AS "Overlap Days"
FROM overlap o
JOIN dbpcm_warehouse.employee AS e
  ON e.employee_code = o.employee_code
GROUP BY e.employee_code,
         e.employee_name,
         e.department_name
HAVING count(DISTINCT o.overlap_date) >= {threshold}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Overlap Days" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Overlap Days" END ASC, e.employee_code
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.punch_pair.employee_code`
- `dbpcm_warehouse.punch_pair.punch_date`
- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-punch-source-audit

**ID:** `bp-punch-source-audit`

**Intent:** Monthly count of punch pairs by ingestion source ARCHIVED versus FREEZE, showing pair counts per source per month

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(toStartOfMonth(punch_date), '%Y-%m') AS "Month",
       table_source AS "Table Source",
       count() AS "Pair Count"
FROM (SELECT punch_date, table_source FROM dbpcm_warehouse.punch_pair)
WHERE punch_date >= today() - toIntervalMonth({win})
GROUP BY "Month", table_source
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC, "Table Source"
```

**Uses:**

- `dbpcm_warehouse.punch_pair.table_source`
- `dbpcm_warehouse.punch_pair.punch_date`

---

### Blueprint: bp-punch-type-mix

**ID:** `bp-punch-type-mix`

**Intent:** Breakdown of punch pair counts by In punch type and Out punch type for a given date window, using punch-level event data only

**Slots:**

- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT in_punch_type AS "In Punch Type",
       out_punch_type AS "Out Punch Type",
       count() AS "Pair Count"
FROM dbpcm_warehouse.punch_pair
WHERE punch_date BETWEEN {window_start} AND {window_end}
GROUP BY in_punch_type, out_punch_type
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Pair Count" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Pair Count" END ASC, "In Punch Type", "Out Punch Type"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.punch_pair.in_punch_type`
- `dbpcm_warehouse.punch_pair.out_punch_type`
- `dbpcm_warehouse.punch_pair.punch_date`

---

### Blueprint: bp-shift-differential-spend

**ID:** `bp-shift-differential-spend`

**Intent:** Shift-differential dollars and hours from the payroll register, resolving earn codes dynamically against type_code for a date window

**Slots:**

- `window` (period_range, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT round(SUM(p.amount), 2) AS "Shift Differential Dollars",
       round(SUM(p.type_hours), 2) AS "Shift Differential Hours"
FROM dbpcm_warehouse.payroll AS p
WHERE p.register_type = 'EARN'
  AND p.type_code IN {shift_codes}
  AND p.pay_period_end_date BETWEEN {window_start} AND {window_end}
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.type_code`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.type_hours`
- `dbpcm_warehouse.payroll.pay_period_end_date`

---

### Blueprint: bp-weekend-work-share

**ID:** `bp-weekend-work-share`

**Intent:** Weekend versus weekday paid hours share by month from total_earning, the authoritative hours source, showing weekend percentage and totals

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(toStartOfMonth(punch_date), '%Y-%m') AS "Month",
       round(SUM(earn_hours), 2) AS "Total Hours",
       round(SUMIf(earn_hours, dayOfWeek(punch_date) IN (6, 7)), 2) AS "Weekend Hours",
       round(SUMIf(earn_hours, dayOfWeek(punch_date) NOT IN (6, 7)), 2) AS "Weekday Hours",
       round(
         100 * SUMIf(earn_hours, dayOfWeek(punch_date) IN (6, 7))
         / nullIf(SUM(earn_hours), 0), 2
       ) AS "Weekend Share Pct"
FROM (SELECT punch_date, earn_hours FROM dbpcm_warehouse.total_earning)
WHERE punch_date >= today() - toIntervalMonth({win})
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_hours`

---

### Blueprint: bp-worked-hours-daily

**ID:** `bp-worked-hours-daily`

**Intent:** Total timecard-grade paid hours per calendar day from total_earning, the authoritative hours source, for a date window

**Slots:**

- `window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT punch_date AS "Punch Date",
       round(SUM(earn_hours), 2) AS "Total Hours",
       count() AS "Line Count"
FROM (SELECT punch_date, earn_hours FROM dbpcm_warehouse.total_earning)
WHERE punch_date BETWEEN {window_start} AND {window_end}
GROUP BY punch_date
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Punch Date" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Punch Date" END ASC
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_hours`

---

## Time-off

### Blueprint: bp-accrual-event-summary

**ID:** `bp-accrual-event-summary`

**Intent:** Accrual event/day row counts, event hours and distinct employees by company, department, workflow status, event type, event-date month or department_status (department and status together). Includes all event types and leave categories unless explicitly filtered. Required half-open requested-for-date period [start,end); optional workflow status enum, resolved event_type and earn_code strings, employee status, department and employee. No implicit Approved or PTO filter. COUNT counts event/day rows, not distinct multi-day requests. Approved hours are not taken balance usage. LEFT JOIN preserves unmatched events unless employee filters supplied; does not zero-fill periods or prove full coverage. For time-off-taken questions bind event_status=Approved and event_type=Time Off Request, plus the requested category/date scope; assume these requests were taken and disclose it. Offer a separate payroll-paid-hours check without executing it unless requested.

**Slots:**

- `group_by` (enum, required) — values: `company`, `department`, `status`, `event_type`, `month`, `department_status`
- `period` (period_range, required)
- `event_status` (enum, optional) — values: `Approved`, `Requested`, `Denied`, `Removed`, `Calendar Only`; binds: `dbpcm_warehouse.timeoff_request.status`
- `event_type` (entity, optional) — binds: `dbpcm_warehouse.timeoff_request.event_type`
- `earn_code` (entity, optional) — binds: `dbpcm_warehouse.timeoff_request.earn_code`
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`; binds: `dbpcm_warehouse.employee.employee_status`
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `employee` (entity, optional) — binds: `dbpcm_warehouse.employee.employee_code`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
WITH a AS (
 SELECT employee_code, status, event_type, substring(toString(request_date), 1, 7) AS month,
 count(*) AS event_rows, sum(hours) AS hours
 FROM dbpcm_warehouse.timeoff_request
 WHERE status = {event_status} AND event_type = {event_type} AND earn_code = {earn_code}
 AND toDate(request_date) >= toDate({period_start}) AND toDate(request_date) < toDate({period_end})
 GROUP BY employee_code, status, event_type, substring(toString(request_date), 1, 7)
)
SELECT CASE {group_by} WHEN 'department' THEN e.department_name WHEN 'department_status' THEN e.department_name WHEN 'status' THEN a.status WHEN 'event_type' THEN a.event_type WHEN 'month' THEN toString(a.month) ELSE 'Company' END AS `Group`, CASE WHEN {group_by} = 'department_status' THEN a.status ELSE 'All selected statuses' END AS `Event Status`, sum(CAST(a.event_rows AS Nullable(Decimal(20, 0)))) AS `Event Day Rows`,
 sum(CAST(a.hours AS Nullable(Decimal(38, 6)))) AS `Event Hours`, uniqExact(a.employee_code) AS `Employee Count`
FROM a LEFT JOIN dbpcm_warehouse.employee e ON a.employee_code = e.employee_code
WHERE e.employee_status = {employee_status} AND e.department_name = {department} AND e.employee_code = {employee}
GROUP BY CASE {group_by} WHEN 'department' THEN e.department_name WHEN 'department_status' THEN e.department_name WHEN 'status' THEN a.status WHEN 'event_type' THEN a.event_type WHEN 'month' THEN toString(a.month) ELSE 'Company' END
, CASE WHEN {group_by} = 'department_status' THEN a.status ELSE 'All selected statuses' END
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN `Group` END DESC, CASE WHEN {order_direction} = 'ASC' THEN `Group` END ASC, `Event Status`
```

**Uses:**

- `dbpcm_warehouse.timeoff_request.employee_code`
- `dbpcm_warehouse.timeoff_request.status`
- `dbpcm_warehouse.timeoff_request.event_type`
- `dbpcm_warehouse.timeoff_request.earn_code`
- `dbpcm_warehouse.timeoff_request.request_date`
- `dbpcm_warehouse.timeoff_request.hours`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-approved-timeoff-hours

**ID:** `bp-approved-timeoff-hours`

**Intent:** Approved time-off hours by department or company-wide for a request-date window, counting only Approved status leave events

**Slots:**

- `window` (period_range, required)
- `dept` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS "Department",
       round(SUM(t.hours), 2) AS "Approved Hours"
FROM dbpcm_warehouse.time_off_request AS t
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = t.employee_code
WHERE t.status = 'Approved'
  AND t.hours IS NOT NULL
  AND toDate32(t.request_date) BETWEEN {window_start} AND {window_end}
  AND e.department_name = {dept}
GROUP BY e.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Approved Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Approved Hours" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.time_off_request.hours`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-approved-timeoff-ranking

**ID:** `bp-approved-timeoff-ranking`

**Intent:** Rank employees by approved time-off request hours in a half-open requested-for-date period. Assume approved requests were taken and disclose that assumption. Include all accessible request records, retain ties at the requested rank, and offer a separate payroll-paid-hours check. Days are distinct requested calendar dates, not punch days.

**Slots:**

- `period` (period_range, required)
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
WITH totals AS (
SELECT employee_code, sum(hours) AS approved_hours, uniqExact(toDate(request_date)) AS requested_days
FROM dbpcm_warehouse.timeoff_request
WHERE status = 'Approved' AND event_type = 'Time Off Request'
AND request_date >= {period_start} AND request_date < {period_end}
GROUP BY employee_code
), ranked AS (
SELECT employee_code, approved_hours, requested_days, dense_rank() OVER (ORDER BY CASE WHEN {order_direction} = 'DESC' THEN approved_hours END DESC, CASE WHEN {order_direction} = 'ASC' THEN approved_hours END ASC) AS hours_rank FROM totals
)
SELECT r.employee_code AS `Employee Code`, e.employee_name AS `Employee Name`, e.department_name AS `Department`, r.approved_hours AS `Approved Time-Off Hours`, r.requested_days AS `Requested Days`, r.hours_rank AS `Rank`
FROM ranked r LEFT JOIN dbpcm_warehouse.employee e ON e.employee_code = r.employee_code WHERE r.hours_rank <= {top_n} ORDER BY r.hours_rank, r.employee_code
```

**Uses:**

- `dbpcm_warehouse.timeoff_request.employee_code`
- `dbpcm_warehouse.timeoff_request.hours`
- `dbpcm_warehouse.timeoff_request.request_date`
- `dbpcm_warehouse.timeoff_request.status`
- `dbpcm_warehouse.timeoff_request.event_type`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-avg-request-size

**ID:** `bp-avg-request-size`

**Intent:** Average approved time-off request hours per row by leave type over a request-date window

**Slots:**

- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT t.earn_description AS "Leave Type",
       t.earn_code AS "Earn Code",
       round(AVG(t.hours), 2) AS "Average Hours",
       COUNT(*) AS "Request Rows"
FROM dbpcm_warehouse.time_off_request AS t
WHERE t.status = 'Approved'
  AND t.hours IS NOT NULL
  AND toDate32(t.request_date) BETWEEN {window_start} AND {window_end}
GROUP BY t.earn_description, t.earn_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Average Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Average Hours" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_request.hours`
- `dbpcm_warehouse.time_off_request.earn_code`
- `dbpcm_warehouse.time_off_request.earn_description`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.request_date`

---

### Blueprint: bp-payout-request-summary

**ID:** `bp-payout-request-summary`

**Intent:** Payout request summary of counts and hours over a request-date window, returning empty when no payout rows exist

**Slots:**

- `window` (period_range, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT COUNT(*) AS "Payout Requests",
       round(SUM(t.hours), 2) AS "Total Hours",
       COUNT(DISTINCT t.employee_code) AS "Employees"
FROM dbpcm_warehouse.time_off_request AS t
WHERE t.event_type = 'Payout Request'
  AND t.status = 'Approved'
  AND toDate32(t.request_date) BETWEEN {window_start} AND {window_end}
```

**Uses:**

- `dbpcm_warehouse.time_off_request.event_type`
- `dbpcm_warehouse.time_off_request.hours`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.time_off_request.employee_code`

---

### Blueprint: bp-pending-timeoff-queue

**ID:** `bp-pending-timeoff-queue`

**Intent:** Pending time-off requests queue with days waiting since submission, optionally filtered by department

**Slots:**

- `dept` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `min_wait` (nonnegative_integer, optional)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT t.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       any(e.department_name) AS "Department",
       toDate32(t.request_date) AS "Request Date",
       round(SUM(t.hours), 2) AS "Pending Hours",
       dateDiff('day', t.date_added, today()) AS "Days Waiting"
FROM dbpcm_warehouse.time_off_request AS t
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = t.employee_code
WHERE t.status = 'Requested'
  AND e.department_name = {dept}
  AND dateDiff('day', t.date_added, today()) >= {min_wait}
GROUP BY t.employee_code, toDate32(t.request_date)
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Request Date" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Request Date" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.date_added`
- `dbpcm_warehouse.time_off_request.hours`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-timeoff-by-leave-type

**ID:** `bp-timeoff-by-leave-type`

**Intent:** Approved time-off hours grouped by earn code concept resolved dynamically via earn_code, or all earn codes when no concept is provided

**Slots:**

- `concept` (string, optional) — binds: `dbpcm_warehouse.time_off_request.earn_description`
- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT t.earn_description AS "Earn Description",
       t.earn_code AS "Earn Code",
       round(SUM(t.hours), 2) AS "Approved Hours"
FROM dbpcm_warehouse.time_off_request AS t
WHERE t.status = 'Approved'
  AND t.hours IS NOT NULL
  AND toDate32(t.request_date) BETWEEN {window_start} AND {window_end}
  AND t.earn_description = {concept}
GROUP BY t.earn_description, t.earn_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Approved Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Approved Hours" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.time_off_request.earn_code`
- `dbpcm_warehouse.time_off_request.earn_description`
- `dbpcm_warehouse.time_off_request.hours`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.request_date`

---

### Blueprint: bp-timeoff-denial-rate

**ID:** `bp-timeoff-denial-rate`

**Intent:** Time-off denial rate by department for decided requests (Approved plus Denied) over a request-date window

**Slots:**

- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT d."Department" AS "Department",
       d."Decided Requests" AS "Decided Requests",
       d."Denied Requests" AS "Denied Requests",
       round((d."Denied Requests" / nullIf(d."Decided Requests", 0)) * 100, 2) AS "Denial Rate Pct"
FROM (
       SELECT e.department_name AS "Department",
              COUNT(*) AS "Decided Requests",
              COUNTIf(t.status = 'Denied') AS "Denied Requests"
       FROM dbpcm_warehouse.time_off_request AS t
       JOIN dbpcm_warehouse.employee AS e ON e.employee_code = t.employee_code
       WHERE t.status IN ('Approved', 'Denied')
         AND toDate32(t.request_date) BETWEEN {window_start} AND {window_end}
       GROUP BY e.department_name
     ) AS d
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Denial Rate Pct" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Denial Rate Pct" END ASC, "Department"
```

**Uses:**

- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-timeoff-funnel

**ID:** `bp-timeoff-funnel`

**Intent:** Time-off request lifecycle funnel counts by status over a request-date window

**Slots:**

- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT t.status AS "Status",
       COUNT(*) AS "Request Count"
FROM dbpcm_warehouse.time_off_request AS t
WHERE toDate32(t.request_date) BETWEEN {window_start} AND {window_end}
GROUP BY t.status
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Request Count" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Request Count" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.request_date`

---

### Blueprint: bp-timeoff-lead-time

**ID:** `bp-timeoff-lead-time`

**Intent:** Median days from submission date to requested leave date by leave type for approved requests

**Slots:**

- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT t.earn_description AS "Leave Type",
       t.earn_code AS "Earn Code",
       round((quantileExactLow(0.5)(t.lead_days) + quantileExactHigh(0.5)(t.lead_days)) / 2, 1) AS "Median Lead Days",
       COUNT(*) AS "Request Rows"
FROM (
       SELECT earn_description, earn_code,
              dateDiff('day', date_added, request_date) AS lead_days
       FROM dbpcm_warehouse.time_off_request
       WHERE status = 'Approved'
         AND request_date IS NOT NULL
         AND toDate32(request_date) > toDate32('1900-01-01')
         AND toDate32(request_date) BETWEEN {window_start} AND {window_end}
     ) AS t
GROUP BY t.earn_description, t.earn_code
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Median Lead Days" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Median Lead Days" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_request.date_added`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.time_off_request.earn_code`
- `dbpcm_warehouse.time_off_request.earn_description`
- `dbpcm_warehouse.time_off_request.status`

---

### Blueprint: bp-timeoff-seasonality

**ID:** `bp-timeoff-seasonality`

**Intent:** Approved time-off hours bucketed by month and by day-of-week over a trailing N-month window based on request date

**Slots:**

- `win` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT formatDateTime(toStartOfMonth(t.request_date), '%Y-%m') AS "Month",
       round(SUM(t.hours), 2) AS "Approved Hours"
FROM dbpcm_warehouse.time_off_request AS t
WHERE t.status = 'Approved'
  AND t.hours IS NOT NULL
  AND t.request_date >= (
        SELECT MAX(request_date) FROM dbpcm_warehouse.time_off_request
      ) - INTERVAL {win} MONTH
GROUP BY "Month"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Month" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Month" END ASC
```

**Uses:**

- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.time_off_request.hours`
- `dbpcm_warehouse.time_off_request.status`

---

### Blueprint: bp-timeoff-vs-paid-leave-tieout

**ID:** `bp-timeoff-vs-paid-leave-tieout`

**Intent:** Reconcile approved time-off request hours against paid leave hours from total_earning per employee per month, showing variance and percentage difference

**Slots:**

- `win` (relative_window, required)
- `top_n` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
WITH tor_monthly AS (
  SELECT employee_code,
         formatDateTime(toStartOfMonth(request_date), '%Y-%m') AS month,
         round(SUM(hours), 2) AS tor_hours
  FROM dbpcm_warehouse.time_off_request
  WHERE status = 'Approved'
    AND request_date >= addMonths(today(), -{win})
    AND earn_code IN {pto_codes}
  GROUP BY employee_code, formatDateTime(toStartOfMonth(request_date), '%Y-%m')
),
te_monthly AS (
  SELECT employee_code,
         formatDateTime(toStartOfMonth(punch_date), '%Y-%m') AS month,
         round(SUM(earn_hours), 2) AS te_hours
  FROM (SELECT employee_code, punch_date, earn_code, earn_hours FROM dbpcm_warehouse.total_earning)
  WHERE punch_date >= addMonths(today(), -{win})
    AND earn_code IN {pto_codes}
  GROUP BY employee_code, formatDateTime(toStartOfMonth(punch_date), '%Y-%m')
)
SELECT COALESCE(r.employee_code, t.employee_code) AS "Employee Code",
       max(e.employee_name) AS "Employee Name",
       COALESCE(r.month, t.month) AS "Month",
       round(COALESCE(r.tor_hours, 0), 2) AS "Approved Timeoff Hours",
       round(COALESCE(t.te_hours, 0), 2) AS "Paid Leave Hours",
       round(COALESCE(r.tor_hours, 0) - COALESCE(t.te_hours, 0), 2) AS "Difference",
       round(abs(COALESCE(r.tor_hours, 0) - COALESCE(t.te_hours, 0))
         / nullIf(COALESCE(t.te_hours, 0), 0) * 100, 2) AS "Pct Diff"
FROM tor_monthly r
FULL OUTER JOIN te_monthly t
  ON t.employee_code = r.employee_code
 AND t.month = r.month
LEFT JOIN dbpcm_warehouse.employee e
  ON e.employee_code = COALESCE(r.employee_code, t.employee_code)
GROUP BY COALESCE(r.employee_code, t.employee_code), COALESCE(r.month, t.month)
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN abs(COALESCE(r.tor_hours, 0) - COALESCE(t.te_hours, 0)) END DESC, CASE WHEN {order_direction} = 'ASC' THEN abs(COALESCE(r.tor_hours, 0) - COALESCE(t.te_hours, 0)) END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.time_off_request.hours`
- `dbpcm_warehouse.time_off_request.earn_code`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.total_earning.employee_code`
- `dbpcm_warehouse.total_earning.punch_date`
- `dbpcm_warehouse.total_earning.earn_code`
- `dbpcm_warehouse.total_earning.earn_hours`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`

---

### Blueprint: bp-timeoff-withdrawal-rate

**ID:** `bp-timeoff-withdrawal-rate`

**Intent:** Removed or withdrawn time-off request rate by department, with denominator counting all active workflow statuses

**Slots:**

- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT e.department_name AS "Department",
       COUNT(*) AS "Total Requests",
       COUNTIf(t.status = 'Removed') AS "Removed Requests",
       round((COUNTIf(t.status = 'Removed') / nullIf(COUNT(*), 0)) * 100, 2) AS "Withdrawal Rate Pct"
FROM dbpcm_warehouse.time_off_request AS t
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = t.employee_code
WHERE t.status IN ('Approved', 'Denied', 'Removed', 'Requested')
  AND toDate32(t.request_date) BETWEEN {window_start} AND {window_end}
GROUP BY e.department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Withdrawal Rate Pct" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Withdrawal Rate Pct" END ASC, "Department"
```

**Uses:**

- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-top-timeoff-users

**ID:** `bp-top-timeoff-users`

**Intent:** Top-N employees by approved time-off hours in a window, joined with their current accrual balance totals

**Slots:**

- `top_n` (relative_window, required)
- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT req."Employee Code",
       req."Employee Name",
       req."Department",
       req."Approved Hours",
       round(COALESCE(bal.balance_total, 0), 2) AS "Current Balance Hours"
FROM (
       SELECT t.employee_code AS "Employee Code",
              any(e.employee_name) AS "Employee Name",
              any(e.department_name) AS "Department",
              round(SUM(t.hours), 2) AS "Approved Hours"
       FROM dbpcm_warehouse.time_off_request AS t
       JOIN dbpcm_warehouse.employee AS e ON e.employee_code = t.employee_code
       WHERE t.status = 'Approved'
         AND t.hours IS NOT NULL
         AND toDate32(t.request_date) BETWEEN {window_start} AND {window_end}
       GROUP BY t.employee_code
     ) AS req
      LEFT JOIN (
        SELECT employee_code, round(SUM(beginning_balance + accrued_amount - taken_amount), 2) AS balance_total
       FROM dbpcm_warehouse.time_off_balance
       GROUP BY employee_code
     ) AS bal ON bal.employee_code = req."Employee Code"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Approved Hours" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Approved Hours" END ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.time_off_request.hours`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.time_off_balance.employee_code`
- `dbpcm_warehouse.time_off_balance.beginning_balance`
- `dbpcm_warehouse.time_off_balance.accrued_amount`
- `dbpcm_warehouse.time_off_balance.taken_amount`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-upcoming-timeoff-roster

**ID:** `bp-upcoming-timeoff-roster`

**Intent:** Roster of employees with approved time-off requests in the next N days for coverage planning, optionally filtered by department

**Slots:**

- `win` (relative_window, required)
- `dept` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT t.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       any(e.department_name) AS "Department",
       toDate32(t.request_date) AS "Request Date",
       t.earn_description AS "Leave Type",
       round(SUM(t.hours), 2) AS "Hours"
FROM dbpcm_warehouse.time_off_request AS t
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = t.employee_code
WHERE t.status = 'Approved'
  AND toDate32(t.request_date) >= today()
  AND toDate32(t.request_date) <= today() + INTERVAL {win} DAY
  AND e.department_name = {dept}
GROUP BY t.employee_code, toDate32(t.request_date), t.earn_description
ORDER BY toDate32(t.request_date) ASC, "Employee Code"
```

**Uses:**

- `dbpcm_warehouse.time_off_request.employee_code`
- `dbpcm_warehouse.time_off_request.request_date`
- `dbpcm_warehouse.time_off_request.hours`
- `dbpcm_warehouse.time_off_request.status`
- `dbpcm_warehouse.time_off_request.earn_description`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

## Workforce

### Blueprint: bp-department-turnover-rate

**ID:** `bp-department-turnover-rate`

**Intent:** Department turnover rate for a window using terms divided by current active headcount, ranked by rate

**Slots:**

- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT department_name AS "Department",
       countIf(termination_date >= {window_start}
               AND termination_date < {window_end}
               AND termination_date IS NOT NULL
               AND toDate32(termination_date) > toDate32('1900-01-01')
               AND (most_recent_hire_date IS NULL OR most_recent_hire_date < termination_date)
              ) AS "Terms",
       round(100 * countIf(termination_date >= {window_start}
                           AND termination_date < {window_end}
                           AND termination_date IS NOT NULL
                           AND toDate32(termination_date) > toDate32('1900-01-01')
                           AND (most_recent_hire_date IS NULL OR most_recent_hire_date < termination_date)
                          )
             / nullIf(countIf(employee_status = 'Active'), 0), 2) AS "Turnover Rate Pct"
FROM dbpcm_warehouse.employee
WHERE department_name IS NOT NULL AND department_name != ''
GROUP BY department_name
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Turnover Rate Pct" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Turnover Rate Pct" END ASC, "Department"
```

**Uses:**

- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-effective-mileage-rate

**ID:** `bp-effective-mileage-rate`

**Intent:** Effective mileage rate by expense rule for a payment date window

**Slots:**

- `expense_window` (period_range, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  expense_rule_description AS "Expense Rule",
  round(toFloat64(SUM(amount_approved)), 2) AS "Total Approved",
  round(toFloat64(SUM(approved_miles)), 2) AS "Total Miles",
  round(toFloat64(SUM(amount_approved)) / nullIf(toFloat64(SUM(approved_miles)), 0), 4) AS "Effective Rate"
FROM dbpcm_warehouse.expense
WHERE (submitted_miles > 0 OR approved_miles > 0)
  AND expense_status IN ('Approved', 'Paid', 'Marked as Paid')
  AND expense_paid_date >= {expense_window_start}
  AND expense_paid_date <= {expense_window_end}
GROUP BY expense_rule_description
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Total Approved" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Total Approved" END ASC, "Expense Rule"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.expense.amount_approved`
- `dbpcm_warehouse.expense.approved_miles`
- `dbpcm_warehouse.expense.expense_rule_description`
- `dbpcm_warehouse.expense.expense_status`
- `dbpcm_warehouse.expense.expense_paid_date`
- `dbpcm_warehouse.expense.submitted_miles`

---

### Blueprint: bp-future-dated-position-changes

**ID:** `bp-future-dated-position-changes`

**Intent:** Active employees with position or seat effective dates inside a forward window

**Slots:**

- `change_window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       position_code AS "Position Code",
       position_seat_number AS "Seat Number",
       effective_date_position_info AS "Position Effective",
       effective_date_position_seat AS "Seat Effective"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND ( (effective_date_position_info >= {change_window_start} AND effective_date_position_info < {change_window_end})
     OR (effective_date_position_seat >= {change_window_start} AND effective_date_position_seat < {change_window_end}) )
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Position Effective" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Position Effective" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.position_code`
- `dbpcm_warehouse.employee.position_seat_number`
- `dbpcm_warehouse.employee.effective_date_position_info`
- `dbpcm_warehouse.employee.effective_date_position_seat`
- `dbpcm_warehouse.employee.employee_status`

---


### Blueprint: bp-future-hire-roster

**ID:** `bp-future-hire-roster`

**Intent:** Employees with future hire dates already on the roster, pre-hires on the books whose start date is after today

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       position_title_position_info AS "Title",
       hire_date AS "Hire Date"
FROM dbpcm_warehouse.employee
WHERE hire_date > now()
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN hire_date END DESC, CASE WHEN {order_direction} = 'ASC' THEN hire_date END ASC, employee_code
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.position_title_position_info`
- `dbpcm_warehouse.employee.hire_date`

---

### Blueprint: bp-future-termination-roster

**ID:** `bp-future-termination-roster`

**Intent:** Employees with future termination dates already on the roster, upcoming planned departures

**Slots:**

- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `ASC`. Sort direction for the primary ORDER BY column. Defaults to ASC (original presentation order).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       employee_status AS "Status",
       termination_date AS "Termination Date"
FROM dbpcm_warehouse.employee
WHERE termination_date IS NOT NULL
  AND toDate32(termination_date) > toDate32('1900-01-01')
  AND termination_date > now()
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN termination_date END DESC, CASE WHEN {order_direction} = 'ASC' THEN termination_date END ASC, employee_code
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.termination_date`

---

### Blueprint: bp-hires-in-range

**ID:** `bp-hires-in-range`

**Intent:** New hires per month within an explicit start/end date range — counts by original hire date; rehires are not re-counted

**Slots:**

- `hire_window` (period_range, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT toStartOfMonth(hire_date) AS month,
       COUNT(DISTINCT employee_code) AS hires
FROM dbpcm_warehouse.employee
WHERE hire_date >= {hire_window_start}
  AND hire_date < {hire_window_end}
  AND (employee_status != 'Not Hired' OR employee_status IS NULL)
GROUP BY toStartOfMonth(hire_date)
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-hires-per-month

**ID:** `bp-hires-per-month`

**Intent:** New hires per month over a trailing window of the last N months — counts by original hire date; rehires are not re-counted

**Slots:**

- `window_months` (relative_window, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT toStartOfMonth(hire_date) AS month,
       COUNT(DISTINCT employee_code) AS hires
FROM dbpcm_warehouse.employee
WHERE hire_date >= (
        SELECT toStartOfMonth(max(hire_date))
        FROM dbpcm_warehouse.employee
        WHERE employee_status != 'N' OR employee_status IS NULL
      ) - INTERVAL {window_months} MONTH
  AND (employee_status != 'N' OR employee_status IS NULL)
GROUP BY toStartOfMonth(hire_date)
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-hires-projection

**ID:** `bp-hires-projection`

**Intent:** Projected number of hires over the next N months at the current hiring pace (run-rate over a trailing window) — counts by original hire date; rehires are not re-counted

**Slots:**

- `window_months` (relative_window, required)
- `horizon_months` (relative_window, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT
  COUNT(DISTINCT employee_code) AS hires_in_window,
  round(COUNT(DISTINCT employee_code) / {window_months}, 1) AS avg_monthly_hires,
  round(COUNT(DISTINCT employee_code) / {window_months} * {horizon_months}) AS projected_next_n_hires
FROM dbpcm_warehouse.employee
WHERE hire_date >= addMonths((
        SELECT toStartOfMonth(max(hire_date))
        FROM dbpcm_warehouse.employee
        WHERE employee_status != 'N' OR employee_status IS NULL
      ), 1) - INTERVAL {window_months} MONTH
  AND (employee_status != 'N' OR employee_status IS NULL)
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-latest-hired-employees

**ID:** `bp-latest-hired-employees`

**Intent:** Latest hired employee list ordered by most recent hire date, including rehires. Defaults to Active + On Leave. Excludes missing/sentinel and future hire dates. row_limit is the requested top N; original hire is not the ranking date.

**Slots:**

- `row_limit` (positive_integer, required)
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for Active + On Leave; select a status to override, or All to remove the status filter.
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT e.employee_code AS `Employee Code`, e.employee_name AS `Employee Name`, e.department_name AS `Department`, e.employee_status AS `Status`, e.most_recent_hire_date AS `Most Recent Hire Date`
FROM dbpcm_warehouse.employee e
WHERE (e.employee_status = {employee_status} OR {employee_status} = 'All') AND e.department_name = {department}
AND e.most_recent_hire_date > toDate('1900-01-01') AND e.most_recent_hire_date <= now()
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN e.most_recent_hire_date END DESC, CASE WHEN {order_direction} = 'ASC' THEN e.most_recent_hire_date END ASC, e.employee_code LIMIT {row_limit}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.most_recent_hire_date`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-negative-paycheck-employees

**ID:** `bp-negative-paycheck-employees`

**Intent:** Employees and pay periods where net pay is negative, flagging reversals and adjustments that result in a negative net-pay amount

**Slots:**

- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT p.employee_code AS "Employee Code",
       e.employee_name AS "Employee Name",
       e.department_name AS "Department",
       p.pay_period_end_date AS "Pay Period End",
       round(SUMIf(p.amount, p.register_type = 'EARN'), 2) AS "Gross Earnings",
       round(SUMIf(p.amount, p.register_type = 'NETPAYDIST'), 2) AS "Net Pay"
FROM dbpcm_warehouse.payroll p
LEFT JOIN dbpcm_warehouse.employee e ON e.employee_code = p.employee_code
WHERE p.pay_period_end_date BETWEEN {window_start} AND {window_end}
GROUP BY p.employee_code, e.employee_name, e.department_name, p.pay_period_end_date
HAVING "Gross Earnings" < 0 OR "Net Pay" < 0
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Pay Period End" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Pay Period End" END ASC, p.employee_code
```

**Uses:**

- `dbpcm_warehouse.payroll.employee_code`
- `dbpcm_warehouse.payroll.amount`
- `dbpcm_warehouse.payroll.register_type`
- `dbpcm_warehouse.payroll.pay_period_end_date`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-negative-pto-balances

**ID:** `bp-negative-pto-balances`

**Intent:** Employees with negative accrual balances per bank, ranked by most overdrawn

**Slots:**

- `top_n` (positive_integer, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT b.employee_code AS "Employee Code",
       any(e.employee_name) AS "Employee Name",
       any(e.department_name) AS "Department",
       b.accrual_code AS "Accrual Code",
       any(b.accrual_description) AS "Accrual Description",
        round(SUM(b.beginning_balance + b.accrued_amount - b.taken_amount), 2) AS "Balance Hours"
FROM dbpcm_warehouse.time_off_balance AS b
LEFT JOIN dbpcm_warehouse.employee AS e ON e.employee_code = b.employee_code
WHERE b.beginning_balance + b.accrued_amount - b.taken_amount < 0
GROUP BY b.employee_code, b.accrual_code
ORDER BY "Balance Hours" ASC
LIMIT {top_n}
```

**Uses:**

- `dbpcm_warehouse.time_off_balance.beginning_balance`
- `dbpcm_warehouse.time_off_balance.accrued_amount`
- `dbpcm_warehouse.time_off_balance.taken_amount`
- `dbpcm_warehouse.time_off_balance.employee_code`
- `dbpcm_warehouse.time_off_balance.accrual_code`
- `dbpcm_warehouse.time_off_balance.accrual_description`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-new-hire-90d-attrition

**ID:** `bp-new-hire-90d-attrition`

**Intent:** New hires in a window who terminated within 90 days of hire, with the 90-day attrition rate as a ratio

**Slots:**

- `window` (period_range, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT count(DISTINCT employee_code) AS "Hires In Window",
       countIf(DISTINCT employee_code,
               termination_date IS NOT NULL
               AND toDate32(termination_date) > toDate32('1900-01-01')
               AND date_diff('day', hire_date, termination_date) <= 90
               AND (most_recent_hire_date IS NULL OR most_recent_hire_date < termination_date)
              ) AS "Terms Within 90 Days",
       round(100 * countIf(DISTINCT employee_code,
                           termination_date IS NOT NULL
                           AND toDate32(termination_date) > toDate32('1900-01-01')
                           AND date_diff('day', hire_date, termination_date) <= 90
                           AND (most_recent_hire_date IS NULL OR most_recent_hire_date < termination_date)
                          )
             / nullIf(count(DISTINCT employee_code), 0), 2) AS "90-Day Attrition Pct"
FROM dbpcm_warehouse.employee
WHERE hire_date IS NOT NULL
  AND toDate32(hire_date) > toDate32('1900-01-01')
  AND hire_date >= {window_start}
  AND hire_date < {window_end}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`

---

### Blueprint: bp-position-change-activity

**ID:** `bp-position-change-activity`

**Intent:** Employee master-data change counts by department and changed field, useful for auditing position seat, title, level, and family churn

**Slots:**

- `changed_field` (string, optional) — binds: `dbpcm_warehouse.employee_change.changed_field`
- `win` (relative_window, required)
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
# Output aliases are Title-Case display names; consumers keying on column names
# must use the quoted aliases.
SELECT any(e.department_name) AS "Department",
       ec.changed_field AS "Changed Field",
       count() AS "Change Events"
FROM (SELECT employee_code, changed_field, change_time_added FROM dbpcm_warehouse.employee_change) AS ec
JOIN dbpcm_warehouse.employee AS e ON e.employee_code = ec.employee_code
WHERE ec.change_time_added >= addMonths(today(), -{win})
  AND ec.changed_field = {changed_field}
GROUP BY e.department_name, ec.changed_field
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Change Events" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Change Events" END ASC, "Department", "Changed Field"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee_change.employee_code`
- `dbpcm_warehouse.employee_change.changed_field`
- `dbpcm_warehouse.employee_change.change_time_added`
- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.department_name`

---

### Blueprint: bp-quarterly-terminations-with-top-reasons

**ID:** `bp-quarterly-terminations-with-top-reasons`

**Intent:** Terminations per quarter within a window, ranked to the top-N termination reasons per quarter

**Slots:**

- `term_window` (period_range, required)
- `top_n` (relative_window, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT "Quarter",
       "Term Reason",
       "Terms",
       "Quarter Total"
FROM (
       SELECT "Quarter",
              "Term Reason",
              "Terms",
              sum("Terms") OVER (PARTITION BY "Quarter") AS "Quarter Total",
              row_number() OVER (PARTITION BY "Quarter" ORDER BY "Terms" DESC) AS reason_rank
       FROM (
              SELECT toStartOfQuarter(termination_date) AS "Quarter",
                     ifNull(nullIf(term_reason, ''), 'Unspecified') AS "Term Reason",
                     count(DISTINCT employee_code) AS "Terms"
              FROM dbpcm_warehouse.employee
              WHERE employee_status = 'Terminated'
                AND termination_date >= {term_window_start}
                AND termination_date < {term_window_end}
                AND termination_date IS NOT NULL AND toDate32(termination_date) > toDate32('1900-01-01')
                AND (most_recent_hire_date IS NULL OR most_recent_hire_date < termination_date)
              GROUP BY "Quarter", "Term Reason"
            )
     )
WHERE reason_rank <= {top_n}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Quarter" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Quarter" END ASC, "Terms" DESC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`
- `dbpcm_warehouse.employee.term_reason`

---

### Blueprint: bp-rehired-employees

**ID:** `bp-rehired-employees`

**Intent:** Terminated-then-rehired employees with rehire event history — employees who left and came back, showing previous termination and rehire dates within a calendar window

**Slots:**

- `window` (period_range, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employee_code AS "Employee Code",
       employee_name AS "Name",
       department_name AS "Department",
       old_termination_date AS "Old Termination Date",
       ifNull(rehire_date, most_recent_hire_date) AS "Rehire Date",
       date_diff('day', old_termination_date, ifNull(rehire_date, most_recent_hire_date)) AS "Days Between"
FROM dbpcm_warehouse.employee
WHERE old_termination_date IS NOT NULL
  AND toDate32(old_termination_date) > toDate32('1900-01-01')
  AND ((rehire_date IS NOT NULL AND toDate32(rehire_date) > toDate32('1900-01-01') AND rehire_date > old_termination_date)
       OR (most_recent_hire_date IS NOT NULL AND most_recent_hire_date > old_termination_date))
  AND ifNull(rehire_date, most_recent_hire_date) >= {window_start}
  AND ifNull(rehire_date, most_recent_hire_date) < {window_end}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Rehire Date" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Rehire Date" END ASC, "Employee Code"
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.old_termination_date`
- `dbpcm_warehouse.employee.rehire_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`

---

### Blueprint: bp-termination-family

**ID:** `bp-termination-family`

**Intent:** Employees with a recorded termination date in a half-open period, with total matching record count before the display limit. Includes same-day/later rehires and employees no longer currently Terminated. Apply current status only when explicitly requested. For counts/grouped summaries use bp-employee-date-summary with date_basis=termination.

**Slots:**

- `period` (period_range, required)
- `row_limit` (positive_integer, required)
- `employee_status` (enum, optional) — values: `Active`, `Deceased`, `Inactive`, `Retired`, `Terminated`, `On Leave`, `Not Hired`, `Prehire`, `Unknown`, `All`; binds: `dbpcm_warehouse.employee.employee_status`. Omit for all recorded terminations in the period; set only when current status is explicitly requested.
- `department` (entity, optional) — binds: `dbpcm_warehouse.department.department_name`. Resolve department names/abbreviations against the directory; a known department may have no matching employees.
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**Catalog SHA:** `0c0c24a9bb5ccc742d3b7f9685607355a6d6a051`

**SQL Template:**

```sql
SELECT e.employee_code AS `Employee Code`, e.employee_name AS `Employee Name`, e.department_name AS `Department`, e.employee_status AS `Current Status`, e.termination_date AS `Termination Date`, count(*) OVER () AS `Total Matching Records`
FROM dbpcm_warehouse.employee e
WHERE e.termination_date >= {period_start} AND e.termination_date < {period_end}
AND (e.employee_status = {employee_status} OR {employee_status} = 'All') AND e.department_name = {department}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN e.termination_date END DESC, CASE WHEN {order_direction} = 'ASC' THEN e.termination_date END ASC, e.employee_code LIMIT {row_limit}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.department.department_name`

---

### Blueprint: bp-workforce-composition

**ID:** `bp-workforce-composition`

**Intent:** Active workforce composition by employment type and DOL status with percent share of active headcount

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT employment_type AS "Employment Type",
       dol_status_description AS "DOL Status",
       count(DISTINCT employee_code) AS "Headcount",
       round(100 * count(DISTINCT employee_code) / sum(count(DISTINCT employee_code)) OVER (), 2) AS "Share Pct"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
GROUP BY employment_type, dol_status_description
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Headcount" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employment_type`
- `dbpcm_warehouse.employee.dol_status_description`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-workforce-diversity-profile

**ID:** `bp-workforce-diversity-profile`

**Intent:** Active headcount distribution across gender, marital status, age band, EEO-1 category and leadership cohort

**Slots:**

- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT multiIf(gender = '', 'Not Reported', gender) AS "Gender",
       ifNull(nullIf(marital_status_description, ''), 'Not Reported') AS "Marital Status",
       multiIf(
         NOT (date_of_birth IS NOT NULL AND toDate32(date_of_birth) > toDate32('1900-01-01')), 'Unknown',
         date_diff('year', date_of_birth, now()) < 20, 'Under 20',
         date_diff('year', date_of_birth, now()) < 30, '20-29',
         date_diff('year', date_of_birth, now()) < 40, '30-39',
         date_diff('year', date_of_birth, now()) < 50, '40-49',
         date_diff('year', date_of_birth, now()) < 60, '50-59',
         date_diff('year', date_of_birth, now()) < 70, '60-69',
         '70+') AS "Age Band",
       eeo1_category AS "EEO-1 Category",
       multiIf(eeo1_category IN ('Executive/Senior Level Officials and Managers', 'First/Mid-Level Officials and Managers'),
               'Leadership', 'All Other') AS "Leadership Cohort",
       count(DISTINCT employee_code) AS "Headcount",
       round(100 * count(DISTINCT employee_code) / sum(count(DISTINCT employee_code)) OVER (), 2) AS "Share Pct"
FROM dbpcm_warehouse.employee
WHERE employee_status = 'Active'
  AND department_name = {department}
GROUP BY "Gender", "Marital Status", "Age Band", "EEO-1 Category", "Leadership Cohort"
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Headcount" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Headcount" END ASC
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.gender`
- `dbpcm_warehouse.employee.marital_status_description`
- `dbpcm_warehouse.employee.date_of_birth`
- `dbpcm_warehouse.employee.eeo1_category`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`

---

### Blueprint: bp-workforce-kpi-bundle

**ID:** `bp-workforce-kpi-bundle`

**Intent:** Single-row executive KPI bundle with current-state metrics plus hires, terms and turnover for one window

**Slots:**

- `kpi_window` (period_range, required)

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT ( SELECT count(DISTINCT employee_code)
         FROM dbpcm_warehouse.employee
         WHERE employee_status = 'Active' ) AS "Active Headcount",
       ( SELECT count()
         FROM dbpcm_warehouse.employee ) AS "Total Records",
       round(100 * ( SELECT count(DISTINCT employee_code)
                     FROM dbpcm_warehouse.employee
                     WHERE employee_status = 'Active' AND employment_type = 'W2' )
             / nullIf(( SELECT count(DISTINCT employee_code)
                        FROM dbpcm_warehouse.employee
                        WHERE employee_status = 'Active' ), 0), 2) AS "W2 Share Pct",
       ( SELECT round((quantileExactLow(0.5)(toFloat64(annual_salary)) + quantileExactHigh(0.5)(toFloat64(annual_salary))) / 2, 2)
         FROM dbpcm_warehouse.employee
         WHERE employee_status = 'Active' AND annual_salary IS NOT NULL ) AS "Median Annual Salary",
       ( SELECT round(avg(date_diff('day', hire_date, now()) / 365.25), 1)
         FROM dbpcm_warehouse.employee
         WHERE employee_status = 'Active' ) AS "Avg Tenure Years",
       round(100 * ( SELECT count(DISTINCT employee_code)
                     FROM dbpcm_warehouse.employee
                     WHERE employee_status = 'Active' AND dol_status_description = 'Full Time' )
             / nullIf(( SELECT count(DISTINCT employee_code)
                        FROM dbpcm_warehouse.employee
                        WHERE employee_status = 'Active' ), 0), 2) AS "Pct Full Time",
       round(100 * ( SELECT count(DISTINCT employee_code)
                     FROM dbpcm_warehouse.employee
                     WHERE employee_status = 'Active' AND direct_deposit = 1 )
             / nullIf(( SELECT count(DISTINCT employee_code)
                        FROM dbpcm_warehouse.employee
                        WHERE employee_status = 'Active' ), 0), 2) AS "Pct Direct Deposit",
       ( SELECT count(DISTINCT employee_code)
         FROM dbpcm_warehouse.employee
         WHERE (employee_status != 'Not Hired' OR employee_status IS NULL)
           AND hire_date >= {kpi_window_start}
           AND hire_date < {kpi_window_end} ) AS "Hires In Window",
       ( SELECT count(DISTINCT employee_code)
         FROM dbpcm_warehouse.employee
         WHERE employee_status = 'Terminated'
           AND termination_date IS NOT NULL AND toDate32(termination_date) > toDate32('1900-01-01')
           AND (most_recent_hire_date IS NULL OR most_recent_hire_date < termination_date)
           AND termination_date >= {kpi_window_start}
           AND termination_date < {kpi_window_end} ) AS "Terms In Window",
       round(( SELECT count(DISTINCT employee_code)
               FROM dbpcm_warehouse.employee
               WHERE employee_status = 'Terminated'
                 AND termination_date IS NOT NULL AND toDate32(termination_date) > toDate32('1900-01-01')
                 AND (most_recent_hire_date IS NULL OR most_recent_hire_date < termination_date)
                 AND termination_date >= {kpi_window_start}
                 AND termination_date < {kpi_window_end} )
             / nullIf(( SELECT count(DISTINCT employee_code)
                        FROM dbpcm_warehouse.employee
                        WHERE employee_status = 'Active' ), 0), 4) AS "Turnover Rate In Window",
       ( SELECT count(DISTINCT employee_code)
         FROM dbpcm_warehouse.employee
         WHERE employee_status = 'On Leave' ) AS "On Leave",
       ( SELECT count(DISTINCT employee_code)
         FROM dbpcm_warehouse.employee
         WHERE employee_status = 'Active'
           AND (primary_emergency_contact_phone IS NULL OR primary_emergency_contact_phone = '') ) AS "Missing Emergency Contacts"
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.employment_type`
- `dbpcm_warehouse.employee.annual_salary`
- `dbpcm_warehouse.employee.hire_date`
- `dbpcm_warehouse.employee.dol_status_description`
- `dbpcm_warehouse.employee.direct_deposit`
- `dbpcm_warehouse.employee.termination_date`
- `dbpcm_warehouse.employee.most_recent_hire_date`
- `dbpcm_warehouse.employee.primary_emergency_contact_phone`

---

### Blueprint: bp-workforce-movements

**ID:** `bp-workforce-movements`

**Intent:** Recent-hire, boomerang-return and full-to-part-time movements within one window

**Slots:**

- `move_window` (period_range, required)
- `min_gap_days` (nonnegative_integer, optional)
- `department` (string, optional) — binds: `dbpcm_warehouse.employee.department_name`
- `max_rows` (positive_integer, required)
- `order_direction` (enum, optional) — values: `DESC`, `ASC`; default: `DESC`. Sort direction for the primary ORDER BY column. Defaults to DESC (highest first).

**Status:** validated (drift: clean)

**SQL Template:**

```sql
SELECT "Employee Code",
       "Name",
       "Department",
       "Movement Type",
       "Movement Date",
       "Gap Days"
FROM (
       SELECT employee_code AS "Employee Code",
              employee_name AS "Name",
              department_name AS "Department",
              'Recent Hire' AS "Movement Type",
              most_recent_hire_date AS "Movement Date",
              CAST(NULL AS Nullable(Int32)) AS "Gap Days"
       FROM dbpcm_warehouse.employee
       WHERE (employee_status != 'Not Hired' OR employee_status IS NULL)
         AND most_recent_hire_date >= {move_window_start}
         AND most_recent_hire_date < {move_window_end}
       UNION ALL
       SELECT employee_code AS "Employee Code",
              employee_name AS "Name",
              department_name AS "Department",
              'Boomerang Hire' AS "Movement Type",
              most_recent_hire_date AS "Movement Date",
              date_diff('day', old_termination_date, most_recent_hire_date) AS "Gap Days"
       FROM dbpcm_warehouse.employee
       WHERE (employee_status != 'Not Hired' OR employee_status IS NULL)
         AND old_termination_date IS NOT NULL AND toDate32(old_termination_date) > toDate32('1900-01-01')
         AND most_recent_hire_date IS NOT NULL AND toDate32(most_recent_hire_date) > toDate32('1900-01-01')
         AND most_recent_hire_date > old_termination_date
         AND most_recent_hire_date >= {move_window_start}
         AND most_recent_hire_date < {move_window_end}
         AND date_diff('day', old_termination_date, most_recent_hire_date) >= {min_gap_days}
       UNION ALL
       SELECT employee_code AS "Employee Code",
              employee_name AS "Name",
              department_name AS "Department",
              'FT To PT' AS "Movement Type",
              full_time_to_part_time_date AS "Movement Date",
              CAST(NULL AS Nullable(Int32)) AS "Gap Days"
       FROM dbpcm_warehouse.employee
       WHERE (employee_status != 'Not Hired' OR employee_status IS NULL)
         AND full_time_to_part_time_date IS NOT NULL AND toDate32(full_time_to_part_time_date) > toDate32('1900-01-01')
         AND full_time_to_part_time_date >= {move_window_start}
         AND full_time_to_part_time_date < {move_window_end}
     )
WHERE "Department" = {department}
ORDER BY CASE WHEN {order_direction} = 'DESC' THEN "Movement Date" END DESC, CASE WHEN {order_direction} = 'ASC' THEN "Movement Date" END ASC, "Employee Code"
LIMIT {max_rows}
```

**Uses:**

- `dbpcm_warehouse.employee.employee_code`
- `dbpcm_warehouse.employee.employee_name`
- `dbpcm_warehouse.employee.department_name`
- `dbpcm_warehouse.employee.employee_status`
- `dbpcm_warehouse.employee.most_recent_hire_date`
- `dbpcm_warehouse.employee.old_termination_date`
- `dbpcm_warehouse.employee.full_time_to_part_time_date`

---
