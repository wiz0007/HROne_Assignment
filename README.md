# Employee Attendance & Analytics API

FastAPI and MongoDB backend service implementing employee attendance tracking, regularization, analytics aggregations, and query plan inspection under OpenAPI Contract v2.0.

## 1. Prerequisites
- **Python**: 3.11+ (Tested on Python 3.14)
- **MongoDB**: 6.0+ (Tested on MongoDB Server 8.x locally on default port `27017`)

## 2. Setup & Installation

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate       # Windows (PowerShell): .\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
```

Ensure `.env` points to your target MongoDB instance (read via `os.environ`):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_dev
```

Seed the 9 sample reference documents into the development database:
```bash
python sample_seed.py
```

## 3. Running the Service

Start the application with Uvicorn:
```bash
uvicorn app.main:app --port 8000 --reload
```

- **Health check**: `http://localhost:8000/health`
- **Interactive OpenAPI docs**: `http://localhost:8000/docs` (or `/redoc`)
- **OpenAPI Contract Specification**: `openapi.yaml`

All required unique and compound indexes are created automatically and idempotently on application startup.

## 4. Running Tests & Benchmarks

```bash
# Run complete test suite (164 tests in isolated disposable databases)
python -m pytest -v

# Run 100k scale benchmark (requires explicit --confirm opt-in)
python benchmarks/scale_benchmark.py --confirm

# Build verified release submission archive
python scripts/build_release.py
```

## 5. Reviewer Notes & Limitations
- **Timezone**: All business rules (R1–R10) compute lateness, overtime, work hours, and attendance dates in Indian Standard Time (IST, `UTC+05:30`).
- **Rounding**: Stored work hours use 2-decimal half-up rounding (R4); rates and analytics percentages use 4-decimal half-up rounding (R8).
- **Concurrency**: Duplicate punch-ins are prevented by a unique compound index on `attendance_logs(emp_code, date)`, which raises `DuplicateKeyError` and returns HTTP 409 Conflict.
- **Single-Node Compatibility**: Designed to run reliably on standalone MongoDB instances; document-level atomic operations and compound unique indexes enforce consistency without requiring a replica set.
