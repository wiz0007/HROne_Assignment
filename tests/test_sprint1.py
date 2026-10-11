from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

from app.main import (
    IST,
    app,
    compute_half_day,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    get_attendance_date,
)


# =========================================================================== #
# Phase 1: Review Findings Tests
# =========================================================================== #

def test_finding1_calendar_date_validation_attendance(client_app):
    """
    Finding 1: Verify calendar-date inputs on GET /attendance.
    Impossible or malformed dates must return 422.
    """
    # Impossible / malformed dates -> 422
    for bad_date in [
        "2026-02-30",
        "2026-13-01",
        "2026-00-10",
        "2026-04-31",
        "not-a-date",
        "2026-2-28",
        "2026/02/28",
    ]:
        res = client_app.get(f"/attendance?date_from={bad_date}")
        assert res.status_code == 422, f"Expected 422 for bad date_from: {bad_date}"

        res_to = client_app.get(f"/attendance?date_to={bad_date}")
        assert res_to.status_code == 422, f"Expected 422 for bad date_to: {bad_date}"

    # Valid dates -> 200
    res_valid = client_app.get("/attendance?date_from=2026-02-28&date_to=2026-02-28")
    assert res_valid.status_code == 200

    # Leap year valid date (2028 is leap year)
    res_leap = client_app.get("/attendance?date_from=2028-02-29&date_to=2028-02-29")
    assert res_leap.status_code == 200

    # date_from > date_to -> 422
    res_inv_range = client_app.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res_inv_range.status_code == 422


def test_finding1_calendar_date_validation_employee(client_app):
    """Finding 1: Verify joining date validation on POST /employees."""
    payload = {
        "emp_code": "EMP0101",
        "name": "Date Test",
        "email": "datetest@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-30",  # Impossible date
    }
    res = client_app.post("/employees", json=payload)
    assert res.status_code == 422

    # Valid leap year date
    payload["joined_on"] = "2028-02-29"
    res_valid = client_app.post("/employees", json=payload)
    assert res_valid.status_code == 201


def test_finding2_indexes_inspection(test_db):
    """Finding 2: Verify required indexes exist and enforce uniqueness."""
    emp_indexes = test_db.employees.index_information()
    assert "emp_code_1" in emp_indexes
    assert emp_indexes["emp_code_1"].get("unique") is True
    assert "department_1" in emp_indexes

    att_indexes = test_db.attendance_logs.index_information()
    assert "emp_code_1_date_1" in att_indexes
    assert att_indexes["emp_code_1_date_1"].get("unique") is True
    assert "date_-1_emp_code_1" in att_indexes


def test_finding2_simulate_index_failure_aborts_startup():
    """
    Finding 2: If critical index creation fails, application startup
    must fail cleanly with RuntimeError rather than proceeding silently.
    """
    with patch("app.main.create_required_indexes", side_effect=Exception("Disk full")):
        with pytest.raises(RuntimeError) as exc_info:
            with TestClient(app):
                pass
        assert "Critical error initializing MongoDB indexes" in str(exc_info.value)



def test_finding3_test_isolation_guard(test_db):
    """Finding 3: Ensure test runs in dedicated isolated database."""
    name = test_db.name.lower()
    assert "test" in name
    assert name not in ("attendance_db", "attendance_dev", "production")


def test_finding4_employee_creation_concurrency(client_app, test_db):
    """
    Finding 4: Simultaneous requests with identical emp_code must yield
    exactly one 201 and one 409, guaranteed by the unique index.
    """
    for i in range(5):
        emp_code = f"EMP{5000 + i}"
        payload = {
            "emp_code": emp_code,
            "name": f"Concurrent Tester {i}",
            "email": f"concurrent{i}@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-15",
        }

        def create():
            with TestClient(app) as tc:
                return tc.post("/employees", json=payload).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(create)
            f2 = executor.submit(create)
            codes = sorted([f1.result(), f2.result()])

        assert codes == [201, 409], f"Iteration {i} failed with codes {codes}"
        assert test_db.employees.count_documents({"emp_code": emp_code}) == 1


# =========================================================================== #
# Phase 2: Category A - Employee Validation
# =========================================================================== #

def test_employee_validation_valid_creation(client_app):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    res = client_app.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert "_id" not in data
    assert isinstance(data["created_at"], int)


def test_employee_validation_duplicate_code(client_app):
    payload = {
        "emp_code": "EMP0002",
        "name": "Rohan Shah",
        "email": "rohan@example.com",
        "department": "Marketing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    assert client_app.post("/employees", json=payload).status_code == 201
    assert client_app.post("/employees", json=payload).status_code == 409


def test_employee_validation_invalid_code_formats(client_app):
    base = {
        "name": "Invalid Code",
        "email": "inv@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    # Pattern: ^EMP\d{4,6}$
    for bad_code in ["EMP123", "EMP1234567", "emp0001", "12345", "EMP_0001"]:
        payload = {**base, "emp_code": bad_code}
        assert client_app.post("/employees", json=payload).status_code == 422


def test_employee_validation_empty_name(client_app):
    payload = {
        "emp_code": "EMP0003",
        "name": "",
        "email": "emp3@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    assert client_app.post("/employees", json=payload).status_code == 422


def test_employee_validation_missing_required_fields(client_app):
    fields = ["emp_code", "name", "email", "department", "joined_on"]
    base = {
        "emp_code": "EMP0004",
        "name": "Test Missing",
        "email": "missing@example.com",
        "department": "Finance",
        "joined_on": "2026-01-05",
    }
    for field in fields:
        bad_payload = {k: v for k, v in base.items() if k != field}
        assert client_app.post("/employees", json=bad_payload).status_code == 422


def test_employee_validation_invalid_emails(client_app):
    base = {
        "emp_code": "EMP0005",
        "name": "Test Email",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    for bad_email in ["not-an-email", "user@", "@domain.com", "user@domain", "user @domain.com"]:
        assert client_app.post("/employees", json={**base, "email": bad_email}).status_code == 422


def test_employee_validation_shift_time_formats(client_app):
    base = {
        "emp_code": "EMP0006",
        "name": "Shift Tester",
        "email": "shift@example.com",
        "department": "Operations",
        "joined_on": "2026-01-05",
    }
    # Invalid formats
    for bad_shift in ["9:30", "25:00", "12:60", "invalid", "12-30"]:
        assert client_app.post("/employees", json={**base, "shift_start": bad_shift}).status_code == 422

    # Shift start == shift end
    assert client_app.post(
        "/employees", json={**base, "shift_start": "09:30", "shift_end": "09:30"}
    ).status_code == 422

    # Boundary valid shift times: 00:00 and 23:59
    valid_res = client_app.post(
        "/employees", json={**base, "shift_start": "00:00", "shift_end": "23:59"}
    )
    assert valid_res.status_code == 201


def test_employee_validation_extra_fields_ignored(client_app):
    payload = {
        "emp_code": "EMP0007",
        "name": "Extra Fields",
        "email": "extra@example.com",
        "department": "Security",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
        "work_hours": 10.5,
        "unexpected": "field",
    }
    res = client_app.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert "work_hours" not in data
    assert "unexpected" not in data


# =========================================================================== #
# Phase 2: Category B - Employee Pagination
# =========================================================================== #

def test_employee_pagination(client_app, test_db):
    # Insert 5 test employees
    for i in range(5):
        client_app.post("/employees", json={
            "emp_code": f"EMP001{i}",
            "name": f"Page Emp {i}",
            "email": f"p{i}@example.com",
            "department": "Engineering" if i < 3 else "HR",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        })

    # Page 1, size 2
    p1 = client_app.get("/employees?page=1&page_size=2").json()
    assert len(p1["items"]) == 2
    assert p1["total"] == 5
    assert p1["items"][0]["emp_code"] == "EMP0010"
    assert p1["items"][1]["emp_code"] == "EMP0011"

    # Page 2, size 2
    p2 = client_app.get("/employees?page=2&page_size=2").json()
    assert len(p2["items"]) == 2
    assert p2["items"][0]["emp_code"] == "EMP0012"
    assert p2["items"][1]["emp_code"] == "EMP0013"

    # No duplicates between page 1 and page 2
    codes_p1 = {d["emp_code"] for d in p1["items"]}
    codes_p2 = {d["emp_code"] for d in p2["items"]}
    assert codes_p1.isdisjoint(codes_p2)

    # Invalid page bounds: page 0, page -1
    assert client_app.get("/employees?page=0").status_code == 422
    assert client_app.get("/employees?page=-1").status_code == 422

    # Invalid page size bounds: size 0, size 101
    assert client_app.get("/employees?page_size=0").status_code == 422
    assert client_app.get("/employees?page_size=101").status_code == 422

    # Boundary valid page sizes: 1 and 100
    assert client_app.get("/employees?page_size=1").status_code == 200
    assert client_app.get("/employees?page_size=100").status_code == 200

    # Page beyond last record
    p_beyond = client_app.get("/employees?page=999").json()
    assert p_beyond["items"] == []
    assert p_beyond["total"] == 5

    # Department filter with pagination
    dept_res = client_app.get("/employees?department=HR").json()
    assert dept_res["total"] == 2
    assert len(dept_res["items"]) == 2
    for emp in dept_res["items"]:
        assert emp["department"] == "HR"


# =========================================================================== #
# Phase 2: Category C - Attendance Punch-In Validation
# =========================================================================== #

def test_punch_in_valid_and_nonexistent_emp(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0020",
        "name": "Punch Tester",
        "email": "punch20@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Unknown employee -> 404
    assert client_app.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404

    # Valid punch-in -> 201
    res = client_app.post("/attendance/punch-in", json={"emp_code": "EMP0020"})
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0020"
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert "_id" not in data
    assert "id" not in data

    # Duplicate punch-in for same employee and date -> 409
    res_dup = client_app.post("/attendance/punch-in", json={"emp_code": "EMP0020"})
    assert res_dup.status_code == 409


def test_punch_in_timestamp_validation(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0021",
        "name": "Timestamp Tester",
        "email": "ts21@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Timestamp 0 (< 100000000000) -> 422
    assert client_app.post(
        "/attendance/punch-in", json={"emp_code": "EMP0021", "punched_at": 0}
    ).status_code == 422

    # Negative timestamp -> 422
    assert client_app.post(
        "/attendance/punch-in", json={"emp_code": "EMP0021", "punched_at": -100}
    ).status_code == 422

    # Floating point timestamp -> 422
    assert client_app.post(
        "/attendance/punch-in", json={"emp_code": "EMP0021", "punched_at": 1783312500000.5}
    ).status_code == 422

    # String timestamp -> 422
    assert client_app.post(
        "/attendance/punch-in", json={"emp_code": "EMP0021", "punched_at": "1783312500000"}
    ).status_code == 422


def test_punch_in_status_validation(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0022",
        "name": "Status Tester",
        "email": "st22@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Unsupported / malformed statuses -> 422
    for bad_status in ["LEAVE", "ABSENT", "VACATION", "INVALID"]:
        assert client_app.post(
            "/attendance/punch-in", json={"emp_code": "EMP0022", "status": bad_status}
        ).status_code == 422


def test_failed_punch_in_creates_no_document(client_app, test_db):
    assert test_db.attendance_logs.count_documents({}) == 0
    # Failed request with unknown employee
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert test_db.attendance_logs.count_documents({}) == 0


# =========================================================================== #
# Phase 2: Category D & E - Timezone, Overnight & Late Arrival Boundary
# =========================================================================== #

def test_late_minutes_exact_seconds_boundary_table():
    """
    Phase 2 Category E: Shift 09:30, grace period 10 minutes.
    Verify exact seconds-based threshold.
    """
    d_str = "2026-07-06"
    test_cases = [
        (time(9, 29, 59), 0),
        (time(9, 30, 0), 0),
        (time(9, 39, 59), 0),
        (time(9, 40, 0), 0),
        (time(9, 40, 1), 10),
        (time(9, 40, 59), 10),
        (time(9, 41, 0), 11),
    ]
    for t_val, expected in test_cases:
        dt = datetime.combine(date(2026, 7, 6), t_val, tzinfo=IST)
        actual = compute_late_minutes(dt, "09:30", d_str)
        assert actual == expected, f"Time {t_val} expected {expected} min late, got {actual}"


def test_overnight_shift_lateness_and_date_attribution():
    """
    Phase 2 Category D & E: Overnight shift (22:00 - 06:00).
    """
    # Lateness rules on overnight shift
    # Shift starts at 22:00 on 2026-07-06.
    # 22:10:00 -> 0 min late
    t_grace = datetime(2026, 7, 6, 22, 10, 0, tzinfo=IST)
    assert compute_late_minutes(t_grace, "22:00", "2026-07-06") == 0

    # 22:10:01 -> 10 min late
    t_late = datetime(2026, 7, 6, 22, 10, 1, tzinfo=IST)
    assert compute_late_minutes(t_late, "22:00", "2026-07-06") == 10

    # Punch in next morning at 02:00 (4 hours = 240 min late)
    t_next_am = datetime(2026, 7, 7, 2, 0, 0, tzinfo=IST)
    assert get_attendance_date(t_next_am, "22:00", "06:00") == "2026-07-06"
    assert compute_late_minutes(t_next_am, "22:00", "2026-07-06") == 240

    # Punch in at 06:00:00 (not < 06:00) -> belongs to 2026-07-07
    t_exact_end = datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)
    assert get_attendance_date(t_exact_end, "22:00", "06:00") == "2026-07-07"


def test_utc_to_ist_midnight_crossover():
    """Phase 2 Category D: UTC timestamp conversion around IST midnight."""
    # 2026-07-06 18:29:59 UTC = 2026-07-06 23:59:59 IST
    dt_before_midnight = datetime(2026, 7, 6, 18, 29, 59, tzinfo=timezone.utc)
    assert get_attendance_date(dt_before_midnight, "09:30", "18:30") == "2026-07-06"

    # 2026-07-06 18:30:01 UTC = 2026-07-07 00:00:01 IST
    dt_after_midnight = datetime(2026, 7, 6, 18, 30, 1, tzinfo=timezone.utc)
    assert get_attendance_date(dt_after_midnight, "09:30", "18:30") == "2026-07-07"


def test_instant_truncation_to_whole_seconds():
    """Phase 2 Category D: Instants truncated to whole seconds."""
    from app.main import to_utc_datetime
    # 1783312500900 ms -> should truncate to 1783312500000 ms
    dt = to_utc_datetime(1783312500900)
    assert dt.microsecond == 0
    assert int(dt.timestamp() * 1000) == 1783312500000


# =========================================================================== #
# Phase 2: Category F & G - Work Hours Rounding & Overtime Threshold
# =========================================================================== #

def test_work_hours_half_up_and_half_day_boundary():
    """
    Phase 2 Category F: Work hours round half-up and half-day boundary.
    """
    t_in = datetime(2026, 7, 6, 9, 30, 0, tzinfo=timezone.utc)

    # Exactly 4.50 hours = 16200 seconds -> work_hours = 4.50, half_day = False
    t_450 = t_in + timedelta(seconds=16200)
    wh_450 = compute_work_hours(t_in, t_450)
    assert wh_450 == 4.50
    assert compute_half_day(wh_450) is False

    # Immediately below 4.50 hours: 4.49 hours (16164 seconds = 4.4900 hours)
    t_below = t_in + timedelta(seconds=16164)
    wh_below = compute_work_hours(t_in, t_below)
    assert wh_below == 4.49
    assert compute_half_day(wh_below) is True

    # Immediately above 4.50 hours: 4.51 hours (16236 seconds = 4.5100 hours)
    t_above = t_in + timedelta(seconds=16236)
    wh_above = compute_work_hours(t_in, t_above)
    assert wh_above == 4.51
    assert compute_half_day(wh_above) is False

    # Decimal half-up rounding:
    # 3618 seconds / 3600 = 1.005 -> half-up rounds to 1.01
    t_1005 = t_in + timedelta(seconds=3618)
    assert compute_work_hours(t_in, t_1005) == 1.01

    # 3654 seconds / 3600 = 1.015 -> half-up rounds to 1.02
    t_1015 = t_in + timedelta(seconds=3654)
    assert compute_work_hours(t_in, t_1015) == 1.02


def test_overtime_threshold_boundaries():
    """
    Phase 2 Category G: Overtime >= 30 min threshold.
    Shift: 09:30 - 18:30 on 2026-07-06.
    """
    # Exactly at shift end (18:30:00) -> 0
    t_end = datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST)
    assert compute_overtime(t_end, "18:30", "09:30", "2026-07-06") == 0

    # 29 min 59 sec after shift end (18:59:59) -> 0
    t_2959 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
    assert compute_overtime(t_2959, "18:30", "09:30", "2026-07-06") == 0

    # Exactly 30 min after shift end (19:00:00) -> 30
    t_3000 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(t_3000, "18:30", "09:30", "2026-07-06") == 30

    # 30 min 1 sec after shift end (19:00:01) -> 30
    t_3001 = datetime(2026, 7, 6, 19, 0, 1, tzinfo=IST)
    assert compute_overtime(t_3001, "18:30", "09:30", "2026-07-06") == 30

    # 45 min after shift end (19:15:00) -> 45
    t_4500 = datetime(2026, 7, 6, 19, 15, 0, tzinfo=IST)
    assert compute_overtime(t_4500, "18:30", "09:30", "2026-07-06") == 45

    # Early punch out (18:00:00) -> 0 (no negative overtime)
    t_early = datetime(2026, 7, 6, 18, 0, 0, tzinfo=IST)
    assert compute_overtime(t_early, "18:30", "09:30", "2026-07-06") == 0

    # Overnight shift (22:00 - 06:00), out at 06:40:00 next day -> 40
    t_over = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(t_over, "06:00", "22:00", "2026-07-06") == 40


# =========================================================================== #
# Phase 2: Category H - Attendance Listing
# =========================================================================== #

def test_attendance_listing_comprehensive(client_app, test_db):
    # Insert employee and 3 attendance records
    client_app.post("/employees", json={
        "emp_code": "EMP0030",
        "name": "List Tester",
        "email": "list30@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    test_db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0030",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        {
            "emp_code": "EMP0030",
            "date": "2026-07-07",
            "status": "WFH",
            "punch_in": datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        {
            "emp_code": "EMP0030",
            "date": "2026-07-08",
            "status": "ON_DUTY",
            "punch_in": datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
    ])

    # Unfiltered listing
    res = client_app.get("/attendance")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    # Sorted by date desc
    assert data["items"][0]["date"] == "2026-07-08"
    assert data["items"][1]["date"] == "2026-07-07"
    assert data["items"][2]["date"] == "2026-07-06"

    # Filter by status
    res_status = client_app.get("/attendance?status=WFH").json()
    assert res_status["total"] == 1
    assert res_status["items"][0]["status"] == "WFH"

    # Filter by date range
    res_date = client_app.get("/attendance?date_from=2026-07-07&date_to=2026-07-08").json()
    assert res_date["total"] == 2

    # Filter by emp_code
    res_emp = client_app.get("/attendance?emp_code=EMP0030").json()
    assert res_emp["total"] == 3

    # Empty result set
    res_empty = client_app.get("/attendance?emp_code=EMP9999").json()
    assert res_empty["total"] == 0
    assert res_empty["items"] == []

    # Pagination bounds
    assert client_app.get("/attendance?page_size=100").status_code == 200
    assert client_app.get("/attendance?page_size=101").status_code == 422

    # Verify no _id or id leakage
    for item in data["items"]:
        assert "_id" not in item
        assert "id" not in item
        assert isinstance(item["punch_in"], int)


# =========================================================================== #
# Phase 2: Category I - Health Endpoint
# =========================================================================== #

def test_health_endpoint_available(client_app):
    res = client_app.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_health_endpoint_unavailable_returns_503(client_app):
    with patch("app.main.ping_database", side_effect=Exception("Database down")):
        res = client_app.get("/health")
        assert res.status_code == 503


def test_health_endpoint_timeout_returns_503(client_app):
    with patch(
        "app.main.ping_database",
        side_effect=ServerSelectionTimeoutError("Timeout contacting server"),
    ):
        res = client_app.get("/health")
        assert res.status_code == 503


def test_health_endpoint_auth_failure_returns_503(client_app):
    with patch("app.main.ping_database", side_effect=OperationFailure("Authentication failed")):
        res = client_app.get("/health")
        assert res.status_code == 503



# =========================================================================== #
# Phase 2: Category J - Database Performance & Index Query Plan
# =========================================================================== #

def test_attendance_listing_uses_index_scan(test_db):
    """
    Phase 2 Category J: Ensure MongoDB uses index scan (IXSCAN)
    for attendance listing sort and pagination.
    """
    explain = test_db.attendance_logs.find().sort([("date", -1), ("emp_code", 1)]).explain()
    query_plan = str(explain)
    assert "IXSCAN" in query_plan or "date_-1_emp_code_1" in query_plan
