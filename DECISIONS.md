# Architectural Decisions & Technical Rationale

### 1. Index Inventory & Key Ordering Rationale

| Collection | Index Key Pattern | Unique | Supported Query / Aggregation Shape | Key Order & ESR Rationale | Trade-offs & Redundancies |
|---|---|:---:|---|---|---|
| `employees` | `[("emp_code", 1)]` | **Yes** | Single employee lookups, punch-in/out employee validation, `$lookup` joins from `attendance_logs`. | Natural foreign key; equality lookup $O(1)$. | Primary entity identifier; essential for data integrity. |
| `employees` | `[("department", 1)]` | No | Department filtering in `GET /employees?department=...`. | Equality lookup on department. | Technically a prefix of `[("department", 1), ("emp_code", 1)]` and `[("department", 1), ("joined_on", 1)]`. Preserved for backward compatibility with Sprint 1 index inspection tests. Zero overhead given low employee write volume. |
| `employees` | `[("department", 1), ("emp_code", 1)]` | No | Department employee code projections for department trend and summary aggregations. | Equality (`department`) $\rightarrow$ Sort/Project (`emp_code`). | Compound index avoiding fetches when projecting employee codes by department. |
| `employees` | `[("department", 1), ("joined_on", 1)]` | No | Department summary headcount calculations filtered by department and `joined_on <= month_end`. | ESR Rule: Equality (`department`) $\rightarrow$ Range (`joined_on`). | Delivers index seek directly to department before scanning bounded joining date range. |
| `employees` | `[("joined_on", 1), ("department", 1)]` | No | Company-wide department summary headcount across all departments (`joined_on <= month_end`). | Range (`joined_on`) $\rightarrow$ Group/Equality (`department`). | Range filter on `joined_on` satisfies company-wide queries where department is not pre-filtered. |
| `attendance_logs` | `[("emp_code", 1), ("date", 1)]` | **Yes** | Single attendance record retrieval, punch-in duplicate prevention, employee monthly summary (`GET /analytics/employees/{emp_code}/monthly`), `$lookup` joins. | ESR Rule: Equality (`emp_code`) $\rightarrow$ Range (`date`). | Enforces contract invariant of $\le 1$ log per employee per date. B-tree seek isolates employee before scanning monthly range. |
| `attendance_logs` | `[("date", -1), ("emp_code", 1)]` | No | Paginated attendance listing (`GET /attendance`) with default ordering. | Matches sort specification `[("date", -1), ("emp_code", 1)]` verbatim. | Allows cursor-directed streaming of paginated results without blocking in-memory sort stages (`SORT`). |
| `attendance_logs` | `[("date", 1), ("late_minutes", 1)]` | No | Monthly late leaderboard match stage (`date` in month, `late_minutes > 0`). | ESR Rule: Range (`date`) $\rightarrow$ Range (`late_minutes`). | Delivers index seek on calendar month boundaries while filtering non-late records. |
| `attendance_logs` | `[("date", 1), ("emp_code", 1)]` | No | Date-range foreign key joins against employees collection. | Range (`date`) $\rightarrow$ Equality (`emp_code`). | Accelerates date-bounded lookup joins from employee pipelines. |

---

### 2. Index Scans (`IXSCAN`) vs Covered Queries Clarification

- **Index Scans (`IXSCAN + FETCH`)**:
  All five production endpoints and explain aliases utilize index scans. However, they are **not covered queries** (index-only scans).
- **Rationale**:
  Subsequent processing stages require unindexed document attributes (such as `work_hours`, `status`, `name`, `half_day`, `shift_start`, `shift_end`). Because packing every unindexed payload field into compound index keys would cause massive index bloat and degrade write performance on high-frequency punch-in/out operations, MongoDB performs an `IXSCAN` to seek candidate index keys followed by a `FETCH` (or SBE `PROJECTION_SIMPLE -> FETCH -> IXSCAN`) stage to retrieve document payloads.
- **Evidence**:
  Explain output confirms winning plan stage hierarchies consist of `FETCH -> IXSCAN` or `PROJECTION_SIMPLE -> FETCH -> IXSCAN` with `totalDocsExamined == nReturned`, proving bounded document retrieval without full collection scans (`COLLSCAN`).

---

### 3. Cursor Statistics vs Whole-Pipeline Aggregation Totals

In MongoDB aggregation explain output (`executionStats`), metrics reported under the initial `$cursor` stage represent **cursor-level statistics** (the documents examined and returned by the initial `$match` stage) rather than whole-pipeline totals:
- **`employee_monthly`**: The initial `$match` on `attendance_logs` examines 23 days of records (`cursor_stats.totalDocsExamined: 23`). The subsequent `$group` stage aggregates these records into **1 final summary document** (`final_result_count: 1`).
- **`department_summary`**: The initial `$match` on `employees` examines 100 department employees (`cursor_stats.totalDocsExamined: 100`). The `$lookup` joins attendance logs for each employee (`lookup_metrics.totalDocsExamined: 2,250`), and the `$group` stage produces **1 final department summary** (or 10 summaries for company-wide).
- **`late_leaderboard`**: The initial `$match` on `attendance_logs` examines 2,656 late records (`cursor_stats.totalDocsExamined: 2,656`). The `$group` aggregates per employee, and `$setWindowFields` ranks them, returning **80 tied employees** at rank $\le 10$ (company-wide) or **10 employees** (department-scoped).
- **`department_trend`**: The cursor matches 1 department employee (`cursor_stats.nReturned: 1`), `$unwind` unfolds the dense calendar series into 30 daily items, and lookups join daily metrics, returning **30 daily trend points** (`final_result_count: 30`).

---

### 4. Lookup Stage Metrics & Zero Collection Scan Guarantee

All aggregation pipelines performing left-outer joins (`$lookup`) utilize target collection indexes:
- `department_summary` lookup against `attendance_logs` uses `emp_code_1_date_1` (`collectionScans: 0`).
- `late_leaderboard` lookup against `employees` uses `emp_code_1` (`collectionScans: 0`).
- `department_trend` lookup against `employees` uses `department_1_joined_on_1` (`collectionScans: 0`).
- `department_trend` lookup against `attendance_logs` uses `date_-1_emp_code_1` (`collectionScans: 0`).

Zero collection scans (`COLLSCAN: 0`) were observed across all primary queries and lookup stages.

---

### 5. Punch-in Concurrency & Race-Condition Safety

Two identical punch-in requests arriving simultaneously for the same employee on the same date:
1. Both requests pass input validation and calculate identical attendance dates in IST (`UTC+05:30`).
2. Both proceed to `db.attendance_logs.insert_one(doc)`.
3. MongoDB's WiredTiger storage engine enforces the unique compound index on `("emp_code", 1, "date", 1)`. The first thread writes the document and returns HTTP 201 Created.
4. The second concurrent thread encounters a unique constraint violation and PyMongo raises `DuplicateKeyError` (error code 11000).
5. The application catches `DuplicateKeyError` and translates it into HTTP 409 Conflict (`"already punched in for this date"`). No partial writes, duplicate logs, or race conditions occur.

---

### 6. Ties at Limit Cutoff (Competition Ranking)

When multiple employees tie on total late minutes at the cutoff limit:
- The pipeline uses MongoDB `$setWindowFields` with `$rank` to compute standard competition ranking (`1, 2, 2, 4`).
- Tied employees share the identical rank number (e.g. employees tied at rank 10 all receive rank 10).
- The OpenAPI contract dictates filtering by `rank <= limit`, rather than truncating array length with a naive limit stage.
- Consequently, all tied employees matching the rank boundary are preserved, ordered deterministically by `total_late_minutes` descending, then `emp_code` ascending.

---

### 7. Headcount Accounting for Employees with Zero Logs

To ensure employees with zero logs are accurately counted:
- The `department_summary` pipeline anchors its aggregation on the `employees` collection rather than `attendance_logs`.
- It matches all department employees who joined on or before `month_end`, establishing the exact ground-truth headcount.
- A `$lookup` performs a left outer join against `attendance_logs`. Employees with zero records for the month receive an empty array (`[]`).
- The subsequent grouping computes headcount via `$sum: 1`, while log-derived metrics default safely to zero and average work hours excludes zero-count denominators.

---

### 8. Serving 100x Data Volume

If the dataset scaled to 100x (tens of millions of attendance logs and hundreds of thousands of employees):
- **Pre-Aggregated Summaries**: Replace on-the-fly multi-stage `$lookup` aggregations with pre-aggregated summary collections (`daily_department_rollups`, `monthly_employee_summaries`) maintained via MongoDB Change Streams or background jobs upon punch-out/regularization.
- **Horizontal Sharding**: Hash-shard the `attendance_logs` collection by `emp_code` to distribute write and query loads uniformly across cluster shards.
- **Read Secondaries**: Route read-heavy analytics queries (`/analytics/*` and `/admin/explain/*`) to secondary replica set members using `ReadPreference.SECONDARY_PREFERRED`.

---

### 9. Empirical Scale Benchmark Evidence (~100,000 Records)

Measured against an isolated benchmark database (`attendance_benchmark_100k`) on MongoDB 8.3.9 using `benchmarks/scale_benchmark.py` on **102,000 attendance records** across **1,000 employees** (140 calendar days). Complete reproducible output saved in `benchmarks/benchmark_results.json`.

| Query / Explain Shape | Target Collection | Winning Plan Stages | Primary Index Used | Cursor Matched | Final Output | Docs Examined | Keys Examined | Lookup Examined | Explain Latency | Real Latency | Target SLA |
|---|---|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `attendance_list` (selective) | `attendance_logs` | `LIMIT -> FETCH -> IXSCAN` | `emp_code_1_date_1` | 20 | 20 | 20 | 20 | 0 | 2 ms | 4.15 ms | $\le 50$ ms |
| `attendance_list` (broad date range) | `attendance_logs` | `LIMIT -> FETCH -> IXSCAN` | `date_-1_emp_code_1` | 20 | 20 | 20 | 20 | 0 | 2 ms | 3.58 ms | $\le 50$ ms |
| `employee_monthly` | `attendance_logs` | `GROUP -> FETCH -> IXSCAN` | `emp_code_1_date_1` | 1 | 1 | 23 | 23 | 0 | 6 ms | 7.12 ms | $\le 50$ ms |
| `department_summary` (Engineering) | `employees` | `PROJECTION_SIMPLE -> FETCH -> IXSCAN` | `department_1_joined_on_1` | 100 | 1 | 100 | 100 | 2,250 (`IXSCAN`) | 200 ms | 213.95 ms | $\le 500$ ms |
| `department_summary` (Company-wide) | `employees` | `PROJECTION_SIMPLE -> FETCH -> IXSCAN` | `joined_on_1_department_1` | 1,000 | 10 | 1,000 | 1,000 | 22,450 (`IXSCAN`) | 2,433 ms | 1,408.79 ms | $\le 3,000$ ms |
| `late_leaderboard` (Company-wide) | `attendance_logs` | `PROJECTION_SIMPLE -> FETCH -> IXSCAN` | `date_1_late_minutes_1` | 2,656 | 80 | 2,656 | 2,687 | 2,657 (`IXSCAN`) | 481 ms | 472.81 ms | $\le 1,000$ ms |
| `late_leaderboard` (Engineering) | `attendance_logs` | `PROJECTION_SIMPLE -> FETCH -> IXSCAN` | `date_1_late_minutes_1` | 2,656 | 10 | 2,656 | 2,687 | 2,657 (`IXSCAN`) | 448 ms | 538.97 ms | $\le 1,000$ ms |
| `department_trend` (30-day window) | `employees` | `EXPRESS_IXSCAN` | `department_1` | 1 | 30 | 1 | 1 | 5,150 (`IXSCAN`) | 198 ms | 200.40 ms | $\le 500$ ms |

All winning plans utilized index scans with zero collection scans (`COLLSCAN`), and all `$lookup` stages executed with zero collection scans (`lookup_collscans: 0`), demonstrating bounded key/document examination, predictable scaling, and 100% compliance with target SLAs.

---

### 10. Sprint 5 Benchmark Safety Hardening & Metric Completeness Policy

To guarantee reproducible and fail-closed scale verification, Sprint 5 implemented four safety and evaluation policies:
1. **Explicit Opt-in Requirement (S5-SAFE-01)**:
   - Destructive benchmark setup and cleanup requires an explicit `--confirm` CLI flag or `CONFIRM_BENCHMARK=1` environment variable.
   - Without opt-in, the script immediately halts before constructing a `MongoClient` or issuing any database calls.
   - Application imports are made lazily side-effect-free via `_LazyMongoClient` and `_LazyDatabase` proxies.
2. **Fail-Closed Database and Server Target Guard (S5-SAFE-02)**:
   - Database name guards replace soft `assert` statements with explicit `ValueError` exceptions that cannot be disabled under Python optimized mode (`python -O`).
   - Replaced substring matching (`"benchmark" in name`) with strict equality matching against `APPROVED_BENCHMARK_DATABASES = {"attendance_benchmark_100k"}`. Unapproved names (e.g. `my_benchmark_hack`, `prod_benchmark`) are rejected immediately.
   - Target verification (`verify_server_target`) verifies ping, server info, and database listing fail closed without swallowing exceptions, and checks for production database markers.
3. **Metric Completeness & Evidence Validation (S5-BENCH-01)**:
   - MongoDB explain metrics distinguish observed zero (`collectionScans: 0`, `executionTimeMillis: 0`) from missing (`None`), malformed, negative, or unsupported values.
   - A winning plan containing an `IXSCAN` but lacking required execution statistics returns `INCONCLUSIVE_MISSING_METRICS`, never `PASS`.
   - Pass/fail checks evaluate the primary query plan and each `$lookup` stage independently.
4. **Indexed-Lookup Enforcement Independent of Document Counts (S5-BENCH-02)**:
   - When a benchmark scenario requires indexed `$lookup`, having `indexesUsed: []` or empty/missing index evidence returns `FAIL_UNINDEXED_LOOKUP`, even if `totalDocsExamined == 0`.
   - Multiple `$lookup` stages are evaluated independently; a passing lookup cannot hide an unindexed or scanning lookup stage.
5. **Exact Authoritative Scenario Set & Mandatory Evidence Validation (S5-BENCH-03)**:
   - `aggregate_benchmark_status` validates query results against an exact set of 8 authoritative scenarios (`AUTHORITATIVE_SCENARIOS`).
   - Rejects missing scenarios, unexpected extra scenarios (e.g. 9 instead of 8), duplicate identifiers, and records omitting mandatory evidence.
   - Strictly enforces that every `PASS` scenario provides a valid, non-empty `cursor_stats` evidence dictionary with complete numeric schema (`totalDocsExamined`, `totalKeysExamined`, `executionTimeMillis`, `nReturned`). Missing or null metrics trigger `INCONCLUSIVE`; malformed or negative values trigger `FAIL`.
   - Strict precedence: `FAIL` > `INCONCLUSIVE` > `PASS`. CLI produces a nonzero exit code on any non-PASS status.
6. **Credential & Query Parameter Sanitization (S5-SAFE-03)**:
   - MongoDB connection URIs are safely parsed and scrubbed via `sanitize_mongo_uri` and exception messages via `sanitize_error`.
   - Redacts username/password authority credentials as well as all sensitive query string parameters (`password`, `token`, `tlsCertificateKeyFilePassword`, `authMechanismProperties`, `secret`, `apiKey`, `key`).
   - Preserves non-sensitive operational parameters (`authSource`, `replicaSet`, `readPreference`).
   - Exception strings, logs, and `benchmark_results.json` scrub both URIs and inline credential key-values.
7. **Atomic Release Replacement & Comprehensive Secret Exclusion (S5-REL-01)**:
   - `scripts/build_release.py` uses `os.replace()` for atomic replacement of the target zip without pre-deleting the existing archive, guaranteeing the original archive remains untouched if validation or replacement fails.
   - Inspects file and directory symlinks and rejects any link resolving outside the repository root.
   - Comprehensive filename-pattern exclusions cover `credentials.json`, `secrets.json`, `password.txt`, `client.pfx`, `id_rsa`, `id_ed25519`, `.env*`, and cache/dump directories.
   - Verifies `.env.example` contains only safe placeholder configuration.
   - Temporary archive `.tmp.zip` is cleaned up in a `finally` block on both success and failure.
8. **Application Input Validation Hardening (S5-API-01)**:
   - Employee creation rejects whitespace-only `name` and `department` with HTTP 422.
   - Rejects equal `shift_start` and `shift_end` with HTTP 422.
   - Department filters and employee code filters containing whitespace-only strings are rejected with HTTP 422 across listing and analytics endpoints.



