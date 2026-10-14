# DECISIONS.md

### 1. Which indexes would you add, and why?
- `employees`: unique `emp_code` (O(1) lookups); `(department, joined_on)` (department filtering and headcount range scans).
- `attendance_logs`: unique `(emp_code, date)` (lookup, duplicate prevention, monthly analytics); `(date, -1, emp_code, 1)` (sorted pagination); `(date, late_minutes)` (leaderboard filtering).

### 2. How would you prevent two concurrent punch-in requests from creating duplicate attendance records?
Enforce unique compound index `attendance_logs(emp_code, date)`. MongoDB commits the first insert and rejects duplicates with `DuplicateKeyError` (code 11000). The API catches this, returning HTTP 409 Conflict.

### 3. How should leaderboard ties be handled?
Use competition ranking (1, 2, 2, 4) via `$rank` in `$setWindowFields`. Filter by `rank <= limit` to include all tied employees at cutoff, sorted by `emp_code` ascending.

### 4. How should headcount be calculated for employees who have no attendance logs?
Anchor aggregation on `employees` filtered by `joined_on <= month_end`. Left-join (`$lookup`) `attendance_logs`; employees lacking logs yield empty arrays but increment headcount (`$sum: 1`).

### 5. What is one change you would make to support 100x more data?
Maintain pre-aggregated summary collections updated asynchronously via MongoDB Change Streams on punch events, replacing on-the-fly `$lookup` pipeline scans across tens of millions of records.
