# Architectural Decisions & Technical Rationale

### 1. Indexes
- **Indexes Created:**
  - `employees.emp_code` (`unique=True`): Guarantees employee identity uniqueness and serves $O(1)$ lookups for punch-in/out and join stages.
  - `employees.department`: Baseline department filtering index.
  - `employees.[("department", 1), ("emp_code", 1)]`: Optimizes projection of employee codes for department-scoped aggregations.
  - `employees.[("department", 1), ("joined_on", 1)]` & `[("joined_on", 1), ("department", 1)]`: Delivers index-only scans (`IXSCAN`) for Department Summary and Trend headcount joins filtering by department and joining dates.
  - `attendance_logs.[("emp_code", 1), ("date", 1)]` (`unique=True`): Enforces R1 single-record-per-day invariant and speeds up employee monthly queries.
  - `attendance_logs.[("date", -1), ("emp_code", 1)]`: Serves paginated attendance listing with sort order parity.
  - `attendance_logs.[("date", 1), ("late_minutes", 1)]`: Powers the late leaderboard match stage (`late_minutes > 0` within date range) using `IXSCAN`.
  - `attendance_logs.[("date", 1), ("emp_code", 1)]`: Accelerates date-scoped employee attendance lookups.
- **Considered & Rejected:**
  - A partial filter index on open punch-in records (`{"punch_out": null}`). Rejected because open records are ephemeral (closed within hours), and the primary unique index `("emp_code", 1, "date", 1)` already limits the scan range to a single record without partial index maintenance overhead.

---

### 2. Punch-in Race
Two identical punch-in requests arrive simultaneously for the same employee on the same date:
1. Both requests pass validation and compute identical attendance dates in IST.
2. Both proceed to `db.attendance_logs.insert_one(doc)`.
3. MongoDB's storage engine enforces the unique compound index on `("emp_code", 1, "date", 1)`. The first thread writes the document and returns HTTP 201.
4. The second concurrent thread encounters a unique constraint violation and PyMongo raises `DuplicateKeyError` (error code 11000).
5. The application catches `DuplicateKeyError` and translates it into HTTP 409 Conflict (`"already punched in for this date"`). No partial writes, duplicate logs, or race conditions occur.

---

### 3. Ties at Limit Cutoff
When two employees tie on total late minutes at the cutoff of `limit`, the leaderboard returns **both employees** (the result set length can exceed `limit`):
- The pipeline uses MongoDB `$setWindowFields` with `$rank` to compute standard competition ranking (e.g. `1, 2, 2, 4`).
- Tied employees share the identical rank number (e.g., both receive rank 10).
- The OpenAPI contract dictates filtering by `rank <= limit`, rather than truncating array length with a naive limit stage.
- Consequently, all tied employees matching the rank boundary are preserved, ordered deterministically by `total_late_minutes` descending, then `emp_code` ascending.

---

### 4. Headcount Handling with Zero Logs
To ensure employees with zero logs are accurately counted:
- The `department_summary` pipeline anchors its aggregation on the `employees` collection rather than `attendance_logs`.
- It matches all department employees who joined on or before `month_end`, establishing the exact ground-truth headcount.
- A `$lookup` performs a left outer join against `attendance_logs`. Employees with zero records for the month receive an empty array (`[]`).
- The subsequent grouping computes headcount via `$sum: 1`, while log-derived metrics default safely to zero and average work hours excludes zero-count denominators.

---

### 5. Serving 100x the Data
If the dataset scaled to 100x (tens of millions of attendance logs and hundreds of thousands of employees), I would:
- Replace real-time `$lookup` aggregations with **pre-aggregated summary collections** (e.g., `daily_department_rollups` and `monthly_employee_summaries`) maintained via MongoDB Change Streams or background transactional jobs upon punch-out/regularization.
- Shard the `attendance_logs` collection by `emp_code` (hash-sharded) to distribute write and read traffic evenly across cluster shards.
