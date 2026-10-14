# DECISIONS.md

**Indexes.** `employees(emp_code)` unique and `attendance_logs(emp_code, date)` unique: identity and race-safe punch-in. `attendance_logs(date -1, emp_code)` serves the list sort and date ranges. `(date, late_minutes, emp_code)` lets the leaderboard read only the index. `employees(department, joined_on, emp_code)` and `employees(joined_on)` serve summary and trend.

**Identical punch-ins.** Both pass the employee check and both try to insert. The unique `(emp_code, date)` index lets exactly one insert win (201); the other gets `DuplicateKeyError` and returns 409. There is no read-then-write check to race.

**Leaderboard tie at the cutoff.** `$rank` gives 1, 2, 2, 4 on total late minutes, and I keep rows with `rank <= limit`. A tie at the cutoff is returned in full, so more than `limit` rows can come back; ties are ordered by `emp_code`.

**Employees with no logs.** The summary starts from `employees` (joined on or before month end), then `$lookup`s logs and keeps empty results. They count in headcount and add zeros. The average work hours is total hours / number of records.

**100x data.** Replace `skip` with keyset pagination, pre-aggregate monthly rollups per employee and department (updated on write), drop `$lookup` for those reads, and shard by `emp_code`, archiving old months.
