# Employee Attendance & Analytics API (HROne Assignment)

Backend service built with FastAPI, PyMongo, and MongoDB following the Contract v2.0 specification.

---

## 1. Prerequisites & Environment

- **Python**: 3.11+ (Tested on Python 3.14)
- **MongoDB**: MongoDB Server 6.0+ (Tested on MongoDB Server 8.x / 7.x locally on port `27017`)

---

## 2. Setup & Installation

### Virtual Environment Setup

```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Environment Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

`.env` configuration example:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_dev
```

*Note: `.env` is ignored by Git. Never commit real credentials.*

### Database Initialization

To seed the development database with the 9 sample reference documents:

```bash
python sample_seed.py
```

---

## 3. Running the Application

Start the development server:

```bash
uvicorn app.main:app --port 8000 --reload
```

Interactive API documentation (Swagger UI) is accessible at:
- **Swagger UI**: `http://localhost:8000/docs`
- **Readiness probe**: `http://localhost:8000/health`

---

## 4. Running Automated Tests

All tests run against an isolated, disposable test database (`attendance_test_<uuid>`) and will automatically clean up after execution:

```bash
# Run complete test suite with verbose output (164 tests across all Sprints)
python -m pytest -v

# Byte-compile check
python -m compileall -q app tests benchmarks scripts

# Run Sprint 5 scale & performance benchmark (~100k records, requires explicit --confirm)
python benchmarks/scale_benchmark.py --confirm

# Build clean release ZIP archive (excludes .env, .git, .venv, caches)
python scripts/build_release.py
```

---

## 5. Scope & Current Status

- **Sprint 1 (Complete)**:
  - Base configuration, connection resilience, and automated index creation on startup.
  - Health check endpoint (`GET /health`) with live MongoDB ping.
  - Employee endpoints (`POST /employees`, `GET /employees`) with strict Pydantic validation, deterministic sorting, and 1-based pagination.
  - Attendance punch-in (`POST /attendance/punch-in`) with business rules R1 (overnight shifts, IST timezone, truncation), R2 (10-minute grace period boundary), and race-condition prevention via compound unique index.
  - Attendance listing (`GET /attendance`) with database-level pagination, sorting, and strict calendar-date validation.
  - Comprehensive defect analysis recorded in `REVIEW.md`.
- **Sprint 2 (Complete)**:
  - Attendance punch-out (`POST /attendance/punch-out`) with temporal validation, shift window bounding, atomic closure, work hours (R4), overtime (R3), and half-day (R5) calculations.
  - Manual regularization (`PATCH /attendance/{emp_code}/{date}`) with immutable audit history, field-level diff calculation, and optimistic concurrency control.
- **Sprint 3 (Complete)**:
  - Monthly attendance summary (`GET /analytics/employees/{emp_code}/monthly`) backed by MongoDB aggregation pipeline and R8 4-decimal half-up percentage rounding.
  - Department summary (`GET /analytics/departments/summary`) with cross-collection totals, record-weighted average work hours, and zero-log headcount accounting.
  - Late-comers leaderboard (`GET /analytics/leaderboard/late`) with competition ranking (`$rank`), tie preservation at cutoff limits, and department isolation.
  - Daily department attendance trend (`GET /analytics/departments/{department}/trend`) featuring dense MongoDB-generated calendar series, point-in-time headcounts, and a 7-day trailing moving average window.
- **Sprint 4 (Complete)**:
  - Admin query explain endpoint (`GET /admin/explain/{endpoint}`) with executionStats for all 5 canonical aliases (`attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`).
  - Shared query and aggregation pipeline builders reused verbatim between runtime and explain endpoints.
  - Sanitized database error handling (zero disclosure of internal database exceptions or connection strings).
  - Compound query and unique indexes with startup fail-fast guarantee.
  - Plan inspection verifying index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`) across 102,000 records.
  - Reproducible scale benchmark in `benchmarks/scale_benchmark.py` and saved artifact in `benchmarks/benchmark_results.json`.
  - Complete architectural decision records in `DECISIONS.md`.
- **Sprint 5 (Complete)**:
  - Fail-closed benchmark safety: required `--confirm` opt-in before client instantiation; exact allowlist matching (`APPROVED_BENCHMARK_DATABASES`) using explicit `ValueError` exceptions working in normal and `python -O` modes.
  - Metric completeness: distinguish missing/null/malformed metrics from observed zero (`0`); reject missing metrics as `INCONCLUSIVE_METRICS`.
  - Independent lookup scan enforcement: nonzero lookup collection scans fail even with scan-free primary plan.
  - Scenario-specific index policies and recorded decision reasons per query.
  - Overall status aggregation: any non-PASS scenario makes overall status non-PASS and process exits nonzero.
  - Automated release packaging and integrity checks via `scripts/build_release.py`.
  - 164 automated regression tests passing across all 5 sprints; comprehensive evidence recorded in `benchmarks/benchmark_results.json`.

