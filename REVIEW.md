# REVIEW.md

## Part A — Defects found and fixes

| # | Where | What is wrong | How I would notice it | How I fixed it |
|---:|---|---|---|---|
| 1 | `punch_in` | `result.inserted_id` is used, but `result` is never assigned. This raises `NameError` after the insert has already succeeded. | Every punch-in returns `500`, yet the record exists; retrying the same request returns `409`. | Removed the invalid line. |
| 2 | `punch_in` response | The response leaks an `id` derived from the internal `_id` and returns raw datetimes instead of epoch milliseconds. | The response contains `id` and ISO strings in `punch_in`. | Routed the response through `serialize_attendance`. |
| 3 | `serialize_attendance` | The function is called by `GET /attendance` and `PATCH` but was not defined. | Both endpoints return `500` on the first call. | Implemented the serializer to omit `_id`, convert instants (including history timestamps) to epoch milliseconds, and default missing `half_day`/`history` to `false`/`[]`. |
| 4 | `compute_late_minutes` | It rolled back one day whenever punch-in time-of-day was before `shift_start`, for every shift. | For a 09:30 shift, an 08:00 punch-in gives `late_minutes = 1350` instead of `0`. | Limited rollback to overnight shifts and used the shared `attendance_date_for` logic (R1); the function now takes `shift_end`. |
| 5 | `epoch_ms` | `int(timestamp() * 1000)` can lose a millisecond because of floating-point error. | A stored timestamp ending in `...123` ms can read back as `...122`. | Replaced floating-point conversion with integer arithmetic on the timedelta. |
| 6 | `compute_work_hours` | An unused duplicate uses Python's float `round()` rather than half-up rounding. | A value such as `8.005` hours can round to `8.00` instead of `8.01`. | Removed the duplicate; `calculate_work_hours` using `Decimal` and half-up rounding is the single implementation. |
| 7 | `PunchInIn.status` | Any string was accepted, including `ABSENT`, `LEAVE`, and invalid values. | `status: "LEAVE"` creates an attendance record with a punch-in. | Restricted the field to `Literal["PRESENT", "WFH", "ON_DUTY"]`, so invalid values return `422`. |
| 8 | `punched_at` on punch-in/out | A lax integer conversion accepted numeric strings and floats, and did not reject second-based timestamps. | `punched_at: 1700000000` is interpreted as a date in January 1970. | Used `StrictInt` and a millisecond-range check so these inputs return `422`. |
| 9 | `EmployeeIn.validate_non_empty` | Calling `value.strip()` on a non-string raises `AttributeError`. | `{"emp_code": 123, ...}` returns `500`. | Non-strings are passed to Pydantic validation so the request is rejected with `422`. |
| 10 | `EmployeeIn.joined_on` | `strptime` accepts unpadded dates such as `2026-1-5`, while dates are compared as strings elsewhere. | An employee with an unpadded date can be incorrectly included or excluded from headcount and working-day calculations. | Added a round-trip check against `isoformat()`. |
| 11 | `POST /employees` | `created_at` was returned as a datetime/ISO string. | Its type differs from the value returned by `GET /employees`. | Converted `created_at` to epoch milliseconds. |
| 12 | `PATCH` date check | The path date was compared with the plain IST calendar date of `punch_in`, which is incorrect for overnight shifts. | Correcting a 01:00 punch-in on a 22:00–06:00 shift returns `422`. | Used `attendance_date_for` to derive the attendance date. |
| 13 | `PATCH` concurrency | A conditional update that missed because another correction happened concurrently returned `409` for an otherwise valid correction. | Parallel corrections on one record can cause valid requests to receive `409`. | Re-read and recomputed the correction, retrying up to 10 times; `$push` preserves each history entry. |
| 14 | Month parsing | `int()` accepts values such as `+1` and strings with spaces, and `9999-12` can overflow when calculating the next month. | `month=2026-+1` is accepted or `month=9999-12` returns `500`. | Centralized strict validation in `parse_month`. |
| 15 | `/admin/explain` | Copy-pasted pipelines drifted from the pipelines used by the real endpoints. For example, the summary explained a different pipeline, the trend used a different collection, and the leaderboard omitted `late_count`. | Explain output shows a plan that the endpoint does not actually run. | Shared `build_*_pipeline` and `build_attendance_query` functions between endpoint and explain logic. |
| 16 | Indexes | Department summary without a department filter matches employees by `joined_on` only, which could cause a collection scan. | Explain output shows `COLLSCAN` on employees. | Added an `employees(joined_on)` index. |
| 17 | `attendance_pct` | Float division and double rounding were used; one rounded result was an unused variable. | Values near an `x.xx5` boundary can round down unexpectedly. | Used `Decimal` arithmetic with half-up rounding. |
| 18 | Startup / health | A 30-second server-selection timeout delayed failures, and an index-creation error could prevent the application from starting. | `/health` cannot respond promptly with `503` when MongoDB is unavailable. | Reduced the timeout to 5 seconds and logged index-creation failures while allowing the app to remain available. |

## Examined and decided not to be defects

- The unique `(emp_code, date)` index makes concurrent punch-in creation race-safe.
- Punch-out uses a conditional update (`punch_out: None`) so only one concurrent request can succeed.
- `calculate_overtime` handles R3, including overnight shifts.
- `calculate_work_hours` applies the required rounding.
- The strict `> 10:00` grace check follows R2.
