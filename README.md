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
# Run complete test suite with verbose output
python -m pytest -v

# Byte-compile check
python -m compileall -q app tests
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
  - Monthly attendance summary (`GET /analytics/employees/{emp_code}/monthly`) backed by MongoDB aggregation pipeline.
  - Department summary (`GET /analytics/departments/summary`) with cross-collection totals, record-weighted average work hours, and zero-log headcount accounting.
  - Late-comers leaderboard (`GET /analytics/leaderboard/late`) with competition ranking (`$rank`), tie preservation at cutoff limits, and department isolation.
  - Daily department attendance trend (`GET /analytics/departments/{department}/trend`) featuring dense MongoDB-generated calendar series, point-in-time headcounts, and a 7-day trailing moving average window.
- **Sprint 4 (Complete)**:
  - Admin query explain endpoint (`GET /admin/explain/{endpoint}`) with executionStats for all 5 canonical aliases (`attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`).
  - Compound query and unique indexes with startup fail-fast guarantee.
  - Plan inspection verifying index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`) across 100k+ records.
  - Complete architectural decision records in `DECISIONS.md` and traceability matrix in `SPRINT4_VERIFICATION.md`.
- **Sprint 5 (Planned)**:
  - Final packaging, release artifact inspection, and submission audit against rubric.
