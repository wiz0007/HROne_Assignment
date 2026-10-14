"""
Employee Attendance & Analytics API - Starter & Sprint 1 Fixes

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (loaded from environment / local .env)
"""
import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")


class _LazyMongoClient:
    """Lazy proxy for MongoClient to avoid import-time database side effects (S5-SAFE-01)."""

    def __init__(self, uri_getter):
        self._uri_getter = uri_getter
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = MongoClient(self._uri_getter())
        return self._client

    def __getitem__(self, item):
        return self._get_client()[item]

    def __getattr__(self, name):
        return getattr(self._get_client(), name)

    def __repr__(self):
        return repr(self._client) if self._client else f"<LazyMongoClient uri={self._uri_getter()}>"


class _LazyDatabase:
    """Lazy proxy for MongoDB database to avoid import-time database side effects (S5-SAFE-01)."""

    def __init__(self, client_proxy, db_name_getter):
        self._client_proxy = client_proxy
        self._db_name_getter = db_name_getter
        self._db = None

    def _get_db(self):
        if self._db is not None:
            return self._db
        return self._client_proxy[self._db_name_getter()]

    def __getitem__(self, item):
        return self._get_db()[item]

    def __getattr__(self, name):
        return getattr(self._get_db(), name)

    def __repr__(self):
        return repr(self._db) if self._db else f"<LazyDatabase db={self._db_name_getter()}>"


client = _LazyMongoClient(lambda: os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = _LazyDatabase(client, lambda: os.getenv("MONGO_DB", "attendance_db"))
logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def set_database(target_db):
    """Sets the active database instance (used for testing and isolation)."""
    global db
    db = target_db


def create_required_indexes(target_db=None):
    """
    Creates required unique and query indexes.
    Raises an exception if index creation fails; must not be swallowed.
    """
    database = target_db if target_db is not None else db
    database.employees.create_index("emp_code", unique=True)
    database.employees.create_index("department")
    database.employees.create_index([("department", 1), ("emp_code", 1)])
    database.employees.create_index([("department", 1), ("joined_on", 1)])
    database.employees.create_index([("joined_on", 1), ("department", 1)])

    database.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    database.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    database.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    database.attendance_logs.create_index([("date", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure required indexes on startup before accepting requests.
    # Fail fast if critical uniqueness indexes cannot be established.
    try:
        create_required_indexes(db)
    except Exception as e:
        raise RuntimeError(f"Critical error initializing MongoDB indexes on startup: {e}") from e
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R5)
# --------------------------------------------------------------------------- #
def validate_calendar_date(value: Optional[str], field_name: str) -> Optional[str]:
    """Validates strict YYYY-MM-DD format and real calendar dates."""
    if value is None:
        return None
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise HTTPException(422, f"Invalid date format for {field_name}: '{value}'")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"Impossible calendar date for {field_name}: '{value}'")
    return value


MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def validate_month_string(month: Optional[str]) -> tuple[int, int, str, str]:
    """
    Validates calendar month string YYYY-MM.
    Returns (year, month_num, month_start_str, month_end_str).
    """
    if not month or not isinstance(month, str) or not MONTH_REGEX.match(month):
        raise HTTPException(422, f"Invalid month format: '{month}'. Expected YYYY-MM")
    year = int(month[:4])
    if year == 0:
        raise HTTPException(422, f"Invalid month: '{month}'. Year zero is not a valid calendar year")
    month_num = int(month[5:7])
    last_day = calendar.monthrange(year, month_num)[1]
    month_start = f"{year:04d}-{month_num:02d}-01"
    month_end = f"{year:04d}-{month_num:02d}-{last_day:02d}"
    return year, month_num, month_start, month_end


def compute_working_days(start_date: date, end_date: date) -> int:
    """
    R7: Computes count of weekdays (Monday through Friday) between start_date and end_date inclusive.
    """
    if start_date > end_date:
        return 0
    total_days = (end_date - start_date).days + 1
    full_weeks, rem_days = divmod(total_days, 7)
    count = full_weeks * 5
    for i in range(rem_days):
        if (start_date.weekday() + i) % 7 < 5:
            count += 1
    return count


def round_half_up_2(val: Optional[float | Decimal | int]) -> Optional[float]:
    """R8: Rounds quantities, hours, and percentages to 2 decimal places HALF_UP."""
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float | Decimal | int]) -> Optional[float]:
    """R8: Rounds rates to 4 decimal places HALF_UP."""
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def to_utc_datetime(ms: int) -> datetime:
    """Converts epoch milliseconds (truncated to whole seconds) to UTC-aware datetime."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensures a datetime is timezone-aware in UTC. If naive, assumes UTC."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return dt


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a datetime (or existing ms) to integer epoch milliseconds."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    if isinstance(dt, (int, float)):
        return int(dt)
    return None


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shifts (shift_end <= shift_start): punch-ins strictly earlier than
    shift_end in IST belong to the shift that started the previous day.
    """
    punch_in_ist = punch_in.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight and punch_in_ist.time() < time(end_h, end_m, 0):
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period is 10 min. Late only if strictly > 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    Otherwise 0.
    """
    att_date = date.fromisoformat(attendance_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.combine(att_date, time(start_h, start_m), tzinfo=IST)
    punch_in_ist = punch_in.astimezone(IST)
    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = Decimal(str(int((punch_out - punch_in).total_seconds())))
    hours = diff_sec / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(
    punch_out: datetime, shift_end: str, shift_start: str, attendance_date_str: str
) -> int:
    """
    R3: whole minutes (floor) between shift_end and punch_out, counted only when >= 30;
    otherwise 0. Shift end falls on next calendar day for overnight shifts.
    """
    att_date = date.fromisoformat(attendance_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)
    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    shift_end_dt = datetime.combine(end_date, time(end_h, end_m), tzinfo=IST)
    punch_out_ist = punch_out.astimezone(IST)
    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_seconds < 0:
        return 0
    mins = int(diff_seconds // 60)
    return mins if mins >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Models & Enums
# --------------------------------------------------------------------------- #
class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class ExplainEndpoint(str, Enum):
    ATTENDANCE_LIST = "attendance_list"
    EMPLOYEE_MONTHLY = "employee_monthly"
    DEPARTMENT_SUMMARY = "department_summary"
    LATE_LEADERBOARD = "late_leaderboard"
    DEPARTMENT_TREND = "department_trend"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts_and_joined(self):
        if not self.name.strip():
            raise ValueError("name cannot be empty or whitespace-only")
        if not self.department.strip():
            raise ValueError("department cannot be empty or whitespace-only")
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date in YYYY-MM-DD format")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Sprint 3 Analytics Response Models
# --------------------------------------------------------------------------- #
class EmployeeMonthlyResponse(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class TrendResponse(BaseModel):
    department: str
    items: list[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: dict


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_employee(emp: dict) -> dict:
    return {
        "emp_code": emp["emp_code"],
        "name": emp["name"],
        "email": emp["email"],
        "department": emp["department"],
        "shift_start": emp["shift_start"],
        "shift_end": emp["shift_end"],
        "joined_on": emp["joined_on"],
        "created_at": to_epoch_ms(emp.get("created_at")),
    }


def serialize_attendance(doc: dict) -> dict:
    history = []
    for h in doc.get("history", []):
        changes = {}
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                from_val = to_epoch_ms(from_val)
                to_val = to_epoch_ms(to_val)
            changes[k] = {"from": from_val, "to": to_val}
        history.append({
            "at": to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def ping_database():
    """Pings MongoDB admin command; raises an exception if unreachable."""
    client.admin.command("ping")


@app.get("/health")
def health():
    try:
        ping_database()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created_at_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        if not department.strip():
            raise HTTPException(422, "Department filter cannot be empty or whitespace-only")
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(emp) for emp in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_in_dt = to_utc_datetime(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
        "revision": 0,
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance(doc)


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum | str] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[dict, list[tuple[str, int]], int, int]:
    if date_from is not None:
        validate_calendar_date(date_from, "date_from")
    if date_to is not None:
        validate_calendar_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be greater than date_to")

    q = {}
    if emp_code is not None:
        if not emp_code.strip():
            raise HTTPException(422, "emp_code cannot be empty or whitespace-only")
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        if isinstance(status, StatusEnum):
            q["status"] = status.value
        elif isinstance(status, str):
            try:
                q["status"] = StatusEnum(status).value
            except ValueError:
                raise HTTPException(422, f"Invalid status: '{status}'")
        else:
            raise HTTPException(422, f"Invalid status: '{status}'")

    skip = (page - 1) * page_size
    sort_spec = [("date", -1), ("emp_code", 1)]
    return q, sort_spec, skip, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[StatusEnum] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, sort_spec, skip, limit_val = build_attendance_list_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        page_size=page_size,
    )
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort(sort_spec)
        .skip(skip)
        .limit(limit_val)
    )
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    # Step 4.1: Lookup employee
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Timestamp handling: defaults to server now; truncated to whole seconds
    if body.punched_at is not None:
        out_dt = to_utc_datetime(body.punched_at)
    else:
        out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Step 4.2: Candidate record selection
    # Closes the employee's most recent record whose punch_in is at or before punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out_dt}},
        sort=[("punch_in", -1)],
    )

    if not candidate:
        raise HTTPException(404, "No punch-in found")

    # If candidate is already closed -> 409
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "that record is already punched out")

    cand_punch_in = ensure_utc(candidate.get("punch_in"))
    if cand_punch_in is None:
        raise HTTPException(404, "No punch-in found")

    # Step 4.3: Temporal bounds
    if out_dt <= cand_punch_in:
        raise HTTPException(422, "punched_at must be strictly later than punch_in")

    elapsed_seconds = (out_dt - cand_punch_in).total_seconds()
    if elapsed_seconds > 86400:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Derived calculations
    work_hours = compute_work_hours(cand_punch_in, out_dt)
    overtime_min = compute_overtime(
        out_dt, emp["shift_end"], emp["shift_start"], candidate["date"]
    )
    half_day = compute_half_day(work_hours)

    # Step 4.4: Atomic close and race protection using shared revision CAS
    exp_rev = candidate.get("revision")
    and_conditions = [
        {"_id": candidate["_id"]},
        {"punch_out": None},
    ]
    if exp_rev is None:
        and_conditions.append({"$or": [{"revision": 0}, {"revision": {"$exists": False}}]})
    else:
        and_conditions.append({"revision": exp_rev})

    res = db.attendance_logs.find_one_and_update(
        {"$and": and_conditions},
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            },
            "$inc": {
                "revision": 1,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        # Recheck to distinguish already closed from other conflict
        recheck = db.attendance_logs.find_one({"_id": candidate["_id"]})
        if recheck and recheck.get("punch_out") is not None:
            raise HTTPException(409, "that record is already punched out")
        raise HTTPException(409, "Concurrent modification conflict on attendance record")

    return serialize_attendance(res)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    # Step 5.1: Path parameter calendar date validation
    validate_calendar_date(date, "date")

    # Lookup employee
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Lookup attendance record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    supplied = body.model_fields_set

    # Step 5.2 & 5.3: Determine target state
    target_status = (
        body.status.value
        if ("status" in supplied and body.status is not None)
        else record["status"]
    )

    if target_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                422,
                "Supplying punch_in or punch_out when setting ABSENT or LEAVE is not allowed",
            )
        final_punch_in = None
        final_punch_out = None
        final_late_minutes = 0
        final_work_hours = None
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Target status is a presence status: PRESENT, WFH, ON_DUTY
        if "punch_in" in supplied:
            if body.punch_in is None:
                raise HTTPException(422, "Presence status requires a punch_in")
            final_punch_in = to_utc_datetime(body.punch_in)
        else:
            final_punch_in = ensure_utc(record.get("punch_in"))

        if final_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        # R1 attendance date invariant: punch_in must stay on the record's attendance date
        punch_in_date = get_attendance_date(
            final_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if punch_in_date != date:
            raise HTTPException(
                422,
                f"punch_in belongs to attendance date {punch_in_date}, which does not match record date {date}",
            )

        # Target punch_out
        if "punch_out" in supplied:
            if body.punch_out is not None:
                final_punch_out = to_utc_datetime(body.punch_out)
            else:
                final_punch_out = None  # explicit null clears punch_out
        else:
            final_punch_out = ensure_utc(record.get("punch_out"))

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(422, "punch_out must be strictly later than punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 86400:
                raise HTTPException(
                    422, "punch_out cannot be more than 24 hours after punch_in"
                )

            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(
                final_punch_out, emp["shift_end"], emp["shift_start"], date
            )
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)

    # Step 5.4: Build exact audit diff
    orig_status = record.get("status")
    orig_punch_in_dt = ensure_utc(record.get("punch_in"))
    orig_punch_out_dt = ensure_utc(record.get("punch_out"))
    orig_late = record.get("late_minutes", 0)
    orig_work_hours = record.get("work_hours")
    orig_ot = record.get("overtime_minutes", 0)
    orig_half_day = record.get("half_day", False)

    changes_db = {}
    if orig_status != target_status:
        changes_db["status"] = {"from": orig_status, "to": target_status}
    if orig_punch_in_dt != final_punch_in:
        changes_db["punch_in"] = {"from": orig_punch_in_dt, "to": final_punch_in}
    if orig_punch_out_dt != final_punch_out:
        changes_db["punch_out"] = {"from": orig_punch_out_dt, "to": final_punch_out}
    if orig_late != final_late_minutes:
        changes_db["late_minutes"] = {"from": orig_late, "to": final_late_minutes}
    if orig_work_hours != final_work_hours:
        changes_db["work_hours"] = {"from": orig_work_hours, "to": final_work_hours}
    if orig_ot != final_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": orig_ot, "to": final_overtime_minutes}
    if orig_half_day != final_half_day:
        changes_db["half_day"] = {"from": orig_half_day, "to": final_half_day}

    # No-op check: if no business or derived field actually changed, reject with 422
    if not changes_db:
        raise HTTPException(422, "Correction must change at least one field (no-op request)")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Step 5.5: Prevent lost updates using optimistic concurrency (shared revision + history length)
    exp_rev = record.get("revision")
    curr_hist = record.get("history")
    curr_hist_len = len(curr_hist) if curr_hist is not None else 0

    and_conditions = [{"_id": record["_id"]}]
    if exp_rev is None:
        and_conditions.append({"$or": [{"revision": 0}, {"revision": {"$exists": False}}]})
    else:
        and_conditions.append({"revision": exp_rev})

    if curr_hist_len == 0:
        and_conditions.append({"$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}]})
    else:
        and_conditions.append({"history": {"$size": curr_hist_len}})

    res = db.attendance_logs.find_one_and_update(
        {"$and": and_conditions},
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "late_minutes": final_late_minutes,
                "work_hours": final_work_hours,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry,
            },
            "$inc": {
                "revision": 1,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(409, "Concurrent modification conflict on attendance record")

    return serialize_attendance(res)


# --------------------------------------------------------------------------- #
# Sprint 3 - Analytics Endpoints
# --------------------------------------------------------------------------- #

def build_employee_monthly_pipeline(
    emp_code: str,
    month: str,
    effective_start: Optional[str] = None,
) -> tuple[str, list[dict]]:
    _, _, month_start, month_end = validate_month_string(month)
    start_str = effective_start or month_start
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_str, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [1, 2, 3, 4, 5]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]
    return "attendance_logs", pipeline


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyResponse)
def get_employee_monthly(
    emp_code: str,
    month: str = Query(..., description="Calendar month, YYYY-MM"),
):
    """
    Endpoint A: Monthly attendance summary for one employee.
    Backed by a MongoDB aggregation.
    """
    year, month_num, _month_start, _month_end = validate_month_string(month)

    # 1. Locate employee first, returning 404 if unknown
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{emp_code}' not found")

    # 2. Determine working_days for Mon-Fri from max(joined_on, first day of month) through last day of month.
    emp_joined = date.fromisoformat(emp["joined_on"])
    m_start = date(year, month_num, 1)
    last_day = calendar.monthrange(year, month_num)[1]
    m_end = date(year, month_num, last_day)

    if emp_joined > m_end:
        working_days = 0
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0
        attendance_pct = None
    else:
        effective_start = max(emp_joined, m_start)
        effective_start_str = effective_start.isoformat()
        working_days = compute_working_days(effective_start, m_end)

        # 3. Aggregate matching employee attendance rows within month on or after joined_on
        col, pipeline = build_employee_monthly_pipeline(emp_code, month, effective_start_str)

        agg_res = list(db[col].aggregate(pipeline))
        if agg_res:
            doc = agg_res[0]
            present_days = float(doc.get("present_days", 0.0))
            leave_days = int(doc.get("leave_days", 0))
            late_count = int(doc.get("late_count", 0))
            total_late_minutes = int(doc.get("total_late_minutes", 0))
            total_overtime_minutes = int(doc.get("total_overtime_minutes", 0))
        else:
            present_days = 0.0
            leave_days = 0
            late_count = 0
            total_late_minutes = 0
            total_overtime_minutes = 0

        # 4. attendance_pct = present_days / working_days * 100, rounded HALF_UP to 2 decimals; return null when working_days = 0
        if working_days == 0:
            attendance_pct = None
        else:
            raw_pct = (present_days / working_days) * 100.0
            attendance_pct = round_half_up_2(raw_pct)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up_2(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(
    month: str,
    department: Optional[str] = None,
) -> tuple[str, list[dict]]:
    _, _, month_start, month_end = validate_month_string(month)

    match_stage = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code", "joined": "$joined_on"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$gte": ["$date", "$$joined"]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "day_of_week": {
                                "$isoDayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.day_of_week", [1, 2, 3, 4, 5]]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": "$logs.late_minutes",
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "emp_qualifying_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": 1,
                "emp_late_count": 1,
                "emp_total_late_minutes": 1,
                "emp_leave_count": 1,
                "emp_on_duty_count": 1,
                "emp_work_hours_sum": {"$sum": "$emp_qualifying_hours.work_hours"},
                "emp_work_hours_count": {"$size": "$emp_qualifying_hours"},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "total_work_hours_count": {"$sum": "$emp_work_hours_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]
    return "employees", pipeline


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryResponse)
def get_department_summary(
    month: str = Query(..., description="Calendar month, YYYY-MM"),
    department: Optional[str] = Query(None, description="Restrict to one department (exact match)"),
):
    """
    Endpoint B: Department summary for a month.
    One item per department, sorted by department ascending.
    """
    if department is not None and not department.strip():
        raise HTTPException(422, "Department filter cannot be empty or whitespace-only")
    col, pipeline = build_department_summary_pipeline(month, department)
    res = list(db[col].aggregate(pipeline))
    items = []
    for doc in res:
        avg_work_hours = None
        if doc.get("total_work_hours_count", 0) > 0:
            avg_work_hours = round_half_up_2(doc["total_work_hours"] / doc["total_work_hours_count"])

        items.append({
            "department": doc["_id"],
            "headcount": int(doc.get("headcount", 0)),
            "present_days": round_half_up_2(doc.get("present_days", 0.0)),
            "avg_work_hours": avg_work_hours,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> tuple[str, list[dict]]:
    _, _, month_start, month_end = validate_month_string(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "$expr": {
                    "$gte": ["$date", "$emp.joined_on"]
                }
            }
        },
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$group": {
                "_id": "$emp_code",
                "emp": {"$first": "$emp"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp.emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$emp.emp_code",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return "attendance_logs", pipeline


@app.get("/analytics/leaderboard/late", response_model=LeaderboardResponse)
def get_late_leaderboard(
    month: str = Query(..., description="Calendar month, YYYY-MM"),
    limit: int = Query(10, ge=1, le=50, description="Ranking cutoff limit (1..50)"),
    department: Optional[str] = Query(None, description="Restrict to one department"),
):
    """
    Endpoint C: Late-comers leaderboard for a month.
    Competition ranking with cutoff ties included.
    """
    if department is not None and not department.strip():
        raise HTTPException(422, "Department filter cannot be empty or whitespace-only")
    col, pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db[col].aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str,
    from_: str,
    to: str,
) -> tuple[str, list[dict]]:
    if not department or not department.strip():
        raise HTTPException(422, "Department cannot be empty or whitespace-only")
    validate_calendar_date(from_, "from")
    validate_calendar_date(to, "to")

    f_date = date.fromisoformat(from_)
    t_date = date.fromisoformat(to)

    if t_date < f_date:
        raise HTTPException(422, "'to' date must be on or after 'from' date")

    num_days = (t_date - f_date).days + 1
    if num_days > 92:
        raise HTTPException(422, f"Date range exceeds maximum 92 days inclusive: {num_days} days requested")

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"offset": {"$range": [0, num_days]}}},
        {"$unwind": "$offset"},
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {
                            "$dateAdd": {
                                "startDate": {"$dateFromString": {"dateString": from_}},
                                "unit": "day",
                                "amount": "$offset",
                            }
                        },
                    }
                },
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": ["$day_of_week", [1, 2, 3, 4, 5]]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"emp_code": 1}},
                ],
                "as": "dept_employees",
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$dept_employees"}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "cur_date": "$date",
                    "emp_codes": "$dept_employees.emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emp_codes"]},
                                    {"$eq": ["$date", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline


@app.get("/analytics/departments/{department}/trend", response_model=TrendResponse)
def get_department_trend(
    department: str,
    from_: str = Query(..., alias="from", description="Start date YYYY-MM-DD"),
    to: str = Query(..., alias="to", description="End date YYYY-MM-DD"),
):
    """
    Endpoint D: Daily attendance trend for a department.
    Generates exact chronological series from 'from' through 'to' inclusive within MongoDB.
    """
    if not department.strip():
        raise HTTPException(422, "Department cannot be empty or whitespace-only")
    # Return 404 if no employees belong to the specified department (even if they all joined later)
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, f"Department '{department}' not found")

    col, pipeline = build_department_trend_pipeline(department, from_, to)
    results = list(db[col].aggregate(pipeline))
    items = []
    for doc in results:
        rate = round_half_up_4(doc.get("attendance_rate"))
        m_avg = round_half_up_4(doc.get("moving_avg_7d"))
        p_count = round_half_up_2(doc.get("present_count", 0.0))
        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc.get("is_working_day", False)),
            "headcount": int(doc.get("headcount", 0)),
            "present_count": p_count,
            "late_count": int(doc.get("late_count", 0)),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Sprint 4 - Admin Explain Endpoints
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse)
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[StatusEnum] = Query(None),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Admin Explain Endpoint:
    Runs explain with executionStats verbosity for the main query or aggregation of the named endpoint,
    built exactly as that endpoint builds it (same filters, sort, pagination).
    Returns raw explain document as JSON object with {endpoint, collection, explain}.
    """
    if endpoint == ExplainEndpoint.ATTENDANCE_LIST:
        collection_name = "attendance_logs"
        q, sort_spec, skip, limit_val = build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=page,
            page_size=page_size,
        )
        command = {
            "explain": {
                "find": collection_name,
                "filter": q,
                "sort": dict(sort_spec),
                "skip": skip,
                "limit": limit_val,
            },
            "verbosity": "executionStats",
        }

    elif endpoint == ExplainEndpoint.EMPLOYEE_MONTHLY:
        if not emp_code:
            raise HTTPException(422, "Missing required query parameter: 'emp_code'")
        if not month:
            raise HTTPException(422, "Missing required query parameter: 'month'")
        validate_month_string(month)

        effective_start_str = None
        emp = db.employees.find_one({"emp_code": emp_code})
        if emp and "joined_on" in emp:
            year = int(month[:4])
            month_num = int(month[5:7])
            emp_joined = date.fromisoformat(emp["joined_on"])
            m_start = date(year, month_num, 1)
            effective_start_str = max(emp_joined, m_start).isoformat()

        collection_name, pipeline = build_employee_monthly_pipeline(emp_code, month, effective_start_str)
        command = {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    elif endpoint == ExplainEndpoint.DEPARTMENT_SUMMARY:
        if not month:
            raise HTTPException(422, "Missing required query parameter: 'month'")
        validate_month_string(month)

        collection_name, pipeline = build_department_summary_pipeline(month, department)
        command = {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    elif endpoint == ExplainEndpoint.LATE_LEADERBOARD:
        if not month:
            raise HTTPException(422, "Missing required query parameter: 'month'")
        validate_month_string(month)
        effective_limit = limit if limit is not None else 10

        collection_name, pipeline = build_late_leaderboard_pipeline(month, effective_limit, department)
        command = {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    elif endpoint == ExplainEndpoint.DEPARTMENT_TREND:
        if not department:
            raise HTTPException(422, "Missing required query parameter: 'department'")
        if not from_:
            raise HTTPException(422, "Missing required query parameter: 'from'")
        if not to:
            raise HTTPException(422, "Missing required query parameter: 'to'")

        collection_name, pipeline = build_department_trend_pipeline(department, from_, to)
        command = {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    else:
        raise HTTPException(422, f"Unknown explain endpoint: {endpoint}")

    try:
        explain_doc = db.command(command)
    except Exception as exc:
        logger.error("Database explain command failed: %s", exc)
        raise HTTPException(500, "Database explain command failed") from exc

    return {
        "endpoint": endpoint.value,
        "collection": collection_name,
        "explain": jsonable_encoder(explain_doc),
    }
