# Architectural Decisions & Technical Rationale

### 1. Indexes
- **Indexes Created:**
  - `employees.emp_code` (`unique=True`): Guarantees employee identity uniqueness and serves $O(1)$ equality lookups for punch-in/out and join foreign keys.
  - `employees.department`: Baseline department equality filtering.
  - `employees.[("department", 1), ("emp_code", 1)]`: Optimizes department-scoped lookups projecting employee codes for trend and summary pipelines.
  - `employees.[("department", 1), ("joined_on", 1)]` & `[("joined_on", 1), ("department", 1)]`: Delivers selective index scans (`IXSCAN`) followed by document fetch (`FETCH`) for department summary and trend headcount calculations filtering by department and joining date.
  - `attendance_logs.[("emp_code", 1), ("date", 1)]` (`unique=True`): Enforces R1 single-attendance-record-per-day invariant and provides equality-plus-range lookups for employee monthly summaries following the ESR (Equality, Sort, Range) rule.
  - `attendance_logs.[("date", -1), ("emp_code", 1)]`: Powers paginated attendance listing queries with sort-order parity (`date` descending, then `emp_code` ascending) without in-memory sorting.
  - `attendance_logs.[("date", 1), ("late_minutes", 1)]`: Powers the monthly late leaderboard match stage (`late_minutes > 0` within date range) using `IXSCAN`.
  - `attendance_logs.[("date", 1), ("emp_code", 1)]`: Accelerates date-range joins against employees.

- **Index-Only / Covered Query Clarification:**
  - Note on query plans: These operations achieve index scans (`IXSCAN`), but they are **not covered queries** (index-only scans). Because subsequent aggregation stages require document fields (such as `work_hours`, `status`, `name`, and `half_day`) that are not packed into the index key pattern, MongoDB executes an `IXSCAN` to seek index keys followed by a `FETCH` stage to retrieve the matching documents.

- **Compound Key Order Rationale:**
  - `[("emp_code", 1), ("date", 1)]`: `emp_code` is evaluated as an exact match (`$eq`), while `date` is evaluated as an interval range (`$gte`, `$lte`). Placing the equality field first ensures the B-tree seek narrows directly to the specific employee before scanning only the relevant date range.
  - `[("date", -1), ("emp_code", 1)]`: Matches the exact sort specification of `GET /attendance` (`sort=[("date", -1), ("emp_code", 1)]`), allowing MongoDB to return documents in requested order directly from the index cursor without an in-memory blocking sort.

- **Trade-offs and Potential Redundancies:**
  - The standalone index `employees.department` is technically a prefix of compound indexes `[("department", 1), ("emp_code", 1)]` and `[("department", 1), ("joined_on", 1)]`. In MongoDB, queries filtering solely on `department` could be satisfied by either compound index. The standalone index was intentionally preserved to guarantee backward compatibility with existing Sprint 1 index inspection tests. Because the `employees` collection has low write volume compared to attendance logs, the index storage and write cost is negligible.

- **Considered & Rejected:**
  - A partial filter index on open punch-in records (`{"punch_out": null}`). Rejected because open records are ephemeral (closed within hours), and the primary unique index `[("emp_code", 1), ("date", 1)]` already bounds candidate lookups to a single document seek without partial index maintenance overhead.

---

### 2. Punch-in Race
Two identical punch-in requests arrive simultaneously for the same employee on the same date:
1. Both requests pass input validation and calculate identical attendance dates in IST.
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
If the dataset scaled to 100x (tens of millions of attendance logs and hundreds of thousands of employees):
- **Pre-Aggregated Summaries**: Replace on-the-fly multi-stage `$lookup` aggregations with pre-aggregated summary collections (e.g., `daily_department_rollups` and `monthly_employee_summaries`) maintained via MongoDB Change Streams or background jobs upon punch-out/regularization.
- **Horizontal Sharding**: Hash-shard the `attendance_logs` collection by `emp_code` to distribute write and query loads uniformly across cluster shards.
- **Read Secondaries**: Route read-heavy analytics queries (`/analytics/*` and `/admin/explain/*`) to secondary replica set members using `ReadPreference.SECONDARY_PREFERRED`.

---

### 6. Empirical Scale Benchmark Evidence (~100,000 Records)
Measured against a local MongoDB instance using `benchmarks/scale_benchmark.py` on **102,000 attendance records** across **1,000 employees**:

| Query / Explain Shape | Winning Plan Stages | Index Used | Returned | Docs Examined | Keys Examined | Latency |
|---|---|---|:---:|:---:|:---:|:---:|
| `attendance_list` (selective) | `LIMIT -> FETCH -> IXSCAN` | `emp_code_1_date_1` | 20 | 20 | 20 | 2 ms |
| `attendance_list` (broad date range) | `LIMIT -> FETCH -> IXSCAN` | `date_-1_emp_code_1` | 20 | 20 | 20 | 1 ms |
| `employee_monthly` | `GROUP -> FETCH -> IXSCAN` | `emp_code_1_date_1` | 1 | 23 | 23 | 7 ms |
| `department_summary` (Engineering) | `PROJECTION -> FETCH -> IXSCAN` | `department_1_joined_on_1` | 100 | 100 | 100 | 149 ms |
| `department_summary` (Company-wide) | `PROJECTION -> FETCH -> IXSCAN` | `joined_on_1_department_1` | 1,000 | 1,000 | 1,000 | 1,465 ms |
| `late_leaderboard` (Company-wide) | `PROJECTION -> FETCH -> IXSCAN` | `date_1_late_minutes_1` | 2,656 | 2,656 | 2,687 | 488 ms |
| `late_leaderboard` (Engineering) | `PROJECTION -> FETCH -> IXSCAN` | `date_1_late_minutes_1` | 2,656 | 2,656 | 2,687 | 604 ms |
| `department_trend` (30-day window) | `EXPRESS_IXSCAN` | `department_1` | 1 | 1 | 1 | 187 ms |

All winning plans utilized index scans with zero full collection scans (`COLLSCAN`), demonstrating bounded key/document examination and predictable scaling.
