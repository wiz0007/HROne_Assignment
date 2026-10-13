from datetime import date
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import (
    db,
    compute_working_days,
    round_half_up_2,
    round_half_up_4,
    validate_month_string,
)


# --------------------------------------------------------------------------- #
# Unit Tests: Helpers, Calendar & Rounding
# --------------------------------------------------------------------------- #


def test_month_validation_unit():
    # Valid month
    y, m, start, end = validate_month_string("2026-07")
    assert y == 2026
    assert m == 7
    assert start == "2026-07-01"
    assert end == "2026-07-31"

    # Leap year February
    y, m, start, end = validate_month_string("2024-02")
    assert end == "2024-02-29"

    # Non-leap year February
    y, m, start, end = validate_month_string("2025-02")
    assert end == "2025-02-28"

    # Valid regular February
    y, m, start, end = validate_month_string("2026-02")
    assert end == "2026-02-28"

    # Valid boundary years (0001-01 and 9999-12)
    y, m, start, end = validate_month_string("0001-01")
    assert y == 1
    assert m == 1
    assert start == "0001-01-01"
    assert end == "0001-01-31"

    y, m, start, end = validate_month_string("9999-12")
    assert y == 9999
    assert m == 12
    assert start == "9999-12-01"
    assert end == "9999-12-31"

    # Year zero must be rejected with 422 before calling calendar.monthrange
    with pytest.raises(HTTPException) as exc_info:
        validate_month_string("0000-01")
    assert exc_info.value.status_code == 422
    assert "Year zero" in exc_info.value.detail

    # Invalid months outside 01-12
    for inv in ["2026-00", "2026-13"]:
        with pytest.raises(HTTPException) as exc:
            validate_month_string(inv)
        assert exc.value.status_code == 422

    # Malformed strings
    for malformed in ["2026-2", "26-02", "2026/07", "invalid", ""]:
        with pytest.raises(HTTPException) as exc:
            validate_month_string(malformed)
        assert exc.value.status_code == 422

    # Non-string input
    for non_str in [None, 123, [], {}]:
        with pytest.raises(HTTPException) as exc:
            validate_month_string(non_str)
        assert exc.value.status_code == 422


def test_round_half_up_rules_unit():
    # 2 decimal places HALF_UP
    assert round_half_up_2(4.494) == 4.49
    assert round_half_up_2(4.495) == 4.50
    assert round_half_up_2(4.496) == 4.50
    assert round_half_up_2(None) is None

    # 4 decimal places HALF_UP
    assert round_half_up_4(0.12344) == 0.1234
    assert round_half_up_4(0.12345) == 0.1235
    assert round_half_up_4(0.12346) == 0.1235
    assert round_half_up_4(None) is None


def test_compute_working_days_unit():
    # July 2026: 2026-07-01 is Wednesday. July has 31 days -> 23 weekdays
    w_days = compute_working_days(date(2026, 7, 1), date(2026, 7, 31))
    assert w_days == 23

    # Weekend only: 2026-07-04 (Sat) to 2026-07-05 (Sun) -> 0 weekdays
    assert compute_working_days(date(2026, 7, 4), date(2026, 7, 5)) == 0

    # Boundary years: 0001-01 (Jan 0001: 31 days, starts Monday -> 23 weekdays)
    assert compute_working_days(date(1, 1, 1), date(1, 1, 31)) == 23

    # Boundary years: 9999-12 (Dec 9999: 31 days, starts Wednesday -> 23 weekdays)
    assert compute_working_days(date(9999, 12, 1), date(9999, 12, 31)) == 23

    # Start date after end date returns 0
    assert compute_working_days(date(2026, 7, 31), date(2026, 7, 1)) == 0


# --------------------------------------------------------------------------- #
# Endpoint A: GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
def test_monthly_unknown_employee_returns_404(client_app: TestClient):
    resp = client_app.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_monthly_existing_employee_zero_logs(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Alice",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    resp = client_app.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 0.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["total_overtime_minutes"] == 0
    assert data["attendance_pct"] == 0.0


def test_monthly_mid_month_joiner(client_app: TestClient):
    # Joined on Wednesday, 2026-07-15.
    # Weekdays from 2026-07-15 to 2026-07-31:
    # 15,16,17 (3) + 20..24 (5) + 27..31 (5) = 13 weekdays
    db.employees.insert_one({
        "emp_code": "EMP0002",
        "name": "Bob",
        "email": "bob@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-15",
    })

    # Log 13 full present days
    for d in [15, 16, 17, 20, 21, 22, 23, 24, 27, 28, 29, 30, 31]:
        db.attendance_logs.insert_one({
            "emp_code": "EMP0002",
            "date": f"2026-07-{d:02d}",
            "status": "PRESENT",
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        })

    resp = client_app.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["working_days"] == 13
    assert data["present_days"] == 13.0
    assert data["attendance_pct"] == 100.0


def test_monthly_joined_after_month_end(client_app: TestClient):
    # Joined 2026-08-01, queried for 2026-07
    db.employees.insert_one({
        "emp_code": "EMP0003",
        "name": "Charlie",
        "email": "charlie@example.com",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
    })

    resp = client_app.get("/analytics/employees/EMP0003/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["working_days"] == 0
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] is None  # Never divide by zero


def test_monthly_weekend_present_and_overtime(client_app: TestClient):
    # Weekend records (Saturday 2026-07-04):
    # Do NOT count towards monthly present_days, but DO count toward late and overtime!
    db.employees.insert_one({
        "emp_code": "EMP0004",
        "name": "Dana",
        "email": "dana@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    # Friday 2026-07-03: PRESENT
    db.attendance_logs.insert_one({
        "emp_code": "EMP0004",
        "date": "2026-07-03",
        "status": "PRESENT",
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # Saturday 2026-07-04: PRESENT, late 20, overtime 60
    db.attendance_logs.insert_one({
        "emp_code": "EMP0004",
        "date": "2026-07-04",
        "status": "PRESENT",
        "work_hours": 10.0,
        "late_minutes": 20,
        "overtime_minutes": 60,
        "half_day": False,
    })

    resp = client_app.get("/analytics/employees/EMP0004/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["present_days"] == 1.0  # Weekend Saturday did NOT add present_days!
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 20  # Weekend late counted!
    assert data["total_overtime_minutes"] == 60  # Weekend overtime counted!


def test_monthly_half_day_on_friday(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP0005",
        "name": "Eve",
        "email": "eve@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    # Friday 2026-07-03: half_day = True
    db.attendance_logs.insert_one({
        "emp_code": "EMP0005",
        "date": "2026-07-03",
        "status": "PRESENT",
        "work_hours": 4.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": True,
    })

    resp = client_app.get("/analytics/employees/EMP0005/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["present_days"] == 0.5


def test_monthly_leave_vs_missing_weekday(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP0006",
        "name": "Frank",
        "email": "frank@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    # Monday 2026-07-06: LEAVE
    db.attendance_logs.insert_one({
        "emp_code": "EMP0006",
        "date": "2026-07-06",
        "status": "LEAVE",
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # Tuesday 2026-07-07 has NO record at all

    resp = client_app.get("/analytics/employees/EMP0006/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["leave_days"] == 1  # Only explicit LEAVE record counted


def test_monthly_legacy_missing_half_day(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP0007",
        "name": "Grace",
        "email": "grace@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    # Document missing half_day field
    db.attendance_logs.insert_one({
        "emp_code": "EMP0007",
        "date": "2026-07-01",
        "status": "PRESENT",
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
    })

    resp = client_app.get("/analytics/employees/EMP0007/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["present_days"] == 1.0  # missing half_day treated as false (full day)


def test_monthly_presence_statuses(client_app: TestClient):
    # WFH, ON_DUTY count as present; ABSENT does not
    db.employees.insert_one({
        "emp_code": "EMP0008",
        "name": "Heidi",
        "email": "heidi@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    db.attendance_logs.insert_many([
        {"emp_code": "EMP0008", "date": "2026-07-01", "status": "WFH", "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
        {"emp_code": "EMP0008", "date": "2026-07-02", "status": "ON_DUTY", "work_hours": 8.5, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
        {"emp_code": "EMP0008", "date": "2026-07-03", "status": "ABSENT", "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
    ])

    resp = client_app.get("/analytics/employees/EMP0008/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["present_days"] == 2.0  # WFH + ON_DUTY


def test_monthly_month_validation_and_boundary_cases(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP_BOUND",
        "name": "BoundTest",
        "email": "bound@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    # Year 0: must return 422
    resp = client_app.get("/analytics/employees/EMP_BOUND/monthly?month=0000-01")
    assert resp.status_code == 422
    assert "Year zero" in resp.json()["detail"]

    # Month outside 01-12
    assert client_app.get("/analytics/employees/EMP_BOUND/monthly?month=2026-00").status_code == 422
    assert client_app.get("/analytics/employees/EMP_BOUND/monthly?month=2026-13").status_code == 422

    # Malformed strings: 2026-2, 26-02, 2026/07, invalid, empty
    for bad in ["2026-2", "26-02", "2026/07", "invalid", ""]:
        assert client_app.get(f"/analytics/employees/EMP_BOUND/monthly?month={bad}").status_code == 422

    # Boundary years: 0001-01 and 9999-12
    # 0001-01: joined on 2026-07-01 > 0001-01-31 -> working_days = 0, attendance_pct = None
    resp_0001 = client_app.get("/analytics/employees/EMP_BOUND/monthly?month=0001-01")
    assert resp_0001.status_code == 200
    assert resp_0001.json()["working_days"] == 0
    assert resp_0001.json()["attendance_pct"] is None

    # 9999-12: joined on 2026-07-01 < 9999-12-01 -> working_days = 23, 0 logs -> attendance_pct = 0.0
    resp_9999 = client_app.get("/analytics/employees/EMP_BOUND/monthly?month=9999-12")
    assert resp_9999.status_code == 200
    assert resp_9999.json()["working_days"] == 23
    assert resp_9999.json()["attendance_pct"] == 0.0

    # 2026-02: regular month (joined in July -> joined after month end -> working_days = 0)
    resp_feb = client_app.get("/analytics/employees/EMP_BOUND/monthly?month=2026-02")
    assert resp_feb.status_code == 200
    assert resp_feb.json()["working_days"] == 0
    assert resp_feb.json()["attendance_pct"] is None


def test_monthly_pre_joining_records_excluded(client_app: TestClient):
    # Employee joined mid-month on 2026-07-15
    db.employees.insert_one({
        "emp_code": "EMP_MID",
        "name": "MidJoiner",
        "email": "mid@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-15",
    })

    # Record before joining date (2026-07-02, Thursday)
    db.attendance_logs.insert_one({
        "emp_code": "EMP_MID",
        "date": "2026-07-02",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 25,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # Record on joining date (2026-07-15, Wednesday)
    db.attendance_logs.insert_one({
        "emp_code": "EMP_MID",
        "date": "2026-07-15",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 15,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # Record after joining date (2026-07-16, Thursday)
    db.attendance_logs.insert_one({
        "emp_code": "EMP_MID",
        "date": "2026-07-16",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })

    resp = client_app.get("/analytics/employees/EMP_MID/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    # Working days: 2026-07-15 to 2026-07-31 -> 13 working days
    assert data["working_days"] == 13
    # Only records on or after joined_on count: 2026-07-15 and 2026-07-16 -> 2.0
    assert data["present_days"] == 2.0
    # Only 2026-07-15 was late (15 mins); pre-joining 2026-07-02 (25 mins) is excluded
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 15
    # Attendance percentage = 2.0 / 13 * 100 = 15.38%
    assert data["attendance_pct"] == 15.38


def test_monthly_employee_joined_after_month_end(client_app: TestClient):
    # Employee joined in August 2026
    db.employees.insert_one({
        "emp_code": "EMP_POST_MONTH",
        "name": "PostMonth",
        "email": "post@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
    })
    # Stray attendance record in July
    db.attendance_logs.insert_one({
        "emp_code": "EMP_POST_MONTH",
        "date": "2026-07-20",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 30,
        "overtime_minutes": 0,
        "half_day": False,
    })

    resp = client_app.get("/analytics/employees/EMP_POST_MONTH/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["working_days"] == 0
    assert data["present_days"] == 0.0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["attendance_pct"] is None


# --------------------------------------------------------------------------- #
# Endpoint B: GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
def test_dept_summary_employee_with_zero_logs_included_in_headcount(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP01",
        "name": "ZeroLog",
        "email": "zerolog@example.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    resp = client_app.get("/analytics/departments/summary?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["department"] == "Finance"
    assert item["headcount"] == 1
    assert item["present_days"] == 0.0
    assert item["avg_work_hours"] is None
    assert item["late_count"] == 0
    assert item["total_late_minutes"] == 0
    assert item["leave_count"] == 0
    assert item["on_duty_count"] == 0


def test_dept_summary_future_joiner_excluded(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP02",
        "name": "Future",
        "email": "future@example.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
    })

    resp = client_app.get("/analytics/departments/summary?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    # Finance has no eligible employees as of 2026-07-31 -> omitted!
    assert len(data["items"]) == 0


def test_dept_summary_record_weighted_average(client_app: TestClient):
    # Employee 1: 3 logs (8.0, 9.0, 10.0) -> sum 27.0, count 3
    # Employee 2: 1 log (5.0) -> sum 5.0, count 1
    # Total work_hours = 32.0, total qualifying records = 4 -> record-weighted avg = 8.00!
    # (If it were employee-weighted: (9.0 + 5.0)/2 = 7.00 - NOT what we want)
    db.employees.insert_many([
        {"emp_code": "EMP10", "name": "E1", "email": "e1@test.com", "department": "Dev", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
        {"emp_code": "EMP11", "name": "E2", "email": "e2@test.com", "department": "Dev", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
    ])
    db.attendance_logs.insert_many([
        {"emp_code": "EMP10", "date": "2026-07-01", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
        {"emp_code": "EMP10", "date": "2026-07-02", "status": "PRESENT", "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
        {"emp_code": "EMP10", "date": "2026-07-03", "status": "PRESENT", "work_hours": 10.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
        {"emp_code": "EMP11", "date": "2026-07-01", "status": "PRESENT", "work_hours": 5.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
    ])

    resp = client_app.get("/analytics/departments/summary?month=2026-07&department=Dev")
    assert resp.status_code == 200
    data = resp.json()
    item = data["items"][0]
    assert item["headcount"] == 2
    assert item["present_days"] == 4.0
    assert item["avg_work_hours"] == 8.00


def test_dept_summary_null_work_hours_excluded_from_denominator(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP20", "name": "E20", "email": "e20@test.com", "department": "QA", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"
    })
    db.attendance_logs.insert_many([
        {"emp_code": "EMP20", "date": "2026-07-01", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
        {"emp_code": "EMP20", "date": "2026-07-02", "status": "PRESENT", "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},  # Still open
    ])

    resp = client_app.get("/analytics/departments/summary?month=2026-07&department=QA")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["avg_work_hours"] == 8.00


def test_dept_summary_weekend_late_and_on_duty(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP30", "name": "E30", "email": "e30@test.com", "department": "Ops", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"
    })
    # Saturday 2026-07-04: ON_DUTY, late 30
    db.attendance_logs.insert_one({
        "emp_code": "EMP30", "date": "2026-07-04", "status": "ON_DUTY", "work_hours": 8.0, "late_minutes": 30, "overtime_minutes": 0, "half_day": False
    })

    resp = client_app.get("/analytics/departments/summary?month=2026-07&department=Ops")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["present_days"] == 0.0  # Weekend -> no present days credit
    assert item["late_count"] == 1
    assert item["total_late_minutes"] == 30
    assert item["on_duty_count"] == 1


def test_dept_summary_unknown_department_returns_empty_items(client_app: TestClient):
    resp = client_app.get("/analytics/departments/summary?month=2026-07&department=NonexistentDept")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []


def test_dept_summary_case_sensitive(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP40", "name": "E40", "email": "e40@test.com", "department": "Engineering", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"
    })
    # Search lowercase "engineering" -> should return []
    resp = client_app.get("/analytics/departments/summary?month=2026-07&department=engineering")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_dept_summary_pre_joining_records_excluded(client_app: TestClient):
    # Employee joined mid-month on 2026-07-15 in Sales dept
    db.employees.insert_one({
        "emp_code": "EMP_DEPT_MID",
        "name": "DeptMid",
        "email": "deptmid@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-15",
    })

    # Record before joining date (2026-07-02)
    db.attendance_logs.insert_one({
        "emp_code": "EMP_DEPT_MID",
        "date": "2026-07-02",
        "status": "PRESENT",
        "work_hours": 9.0,
        "late_minutes": 40,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # Record on joining date (2026-07-15)
    db.attendance_logs.insert_one({
        "emp_code": "EMP_DEPT_MID",
        "date": "2026-07-15",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 20,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # Record after joining date (2026-07-16)
    db.attendance_logs.insert_one({
        "emp_code": "EMP_DEPT_MID",
        "date": "2026-07-16",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })

    resp = client_app.get("/analytics/departments/summary?month=2026-07&department=Sales")
    assert resp.status_code == 200
    data = resp.json()["items"][0]

    assert data["headcount"] == 1
    # Pre-joining record excluded: only 2026-07-15 and 2026-07-16 included -> 2.0
    assert data["present_days"] == 2.0
    # Pre-joining record excluded: only 2026-07-15 late -> 1
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 20
    # avg_work_hours = (8.0 + 8.0) / 2 = 8.00 (9.0 from 2026-07-02 is excluded)
    assert data["avg_work_hours"] == 8.0


def test_dept_summary_month_validation_and_boundaries(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP_DS_VAL",
        "name": "ValTest",
        "email": "val@example.com",
        "department": "Legal",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    # Year 0 returns 422
    assert client_app.get("/analytics/departments/summary?month=0000-01").status_code == 422
    # Month outside 01-12 returns 422
    assert client_app.get("/analytics/departments/summary?month=2026-00").status_code == 422
    assert client_app.get("/analytics/departments/summary?month=2026-13").status_code == 422
    # Boundary years
    resp_0001 = client_app.get("/analytics/departments/summary?month=0001-01")
    assert resp_0001.status_code == 200
    assert resp_0001.json()["items"] == []

    resp_9999 = client_app.get("/analytics/departments/summary?month=9999-12&department=Legal")
    assert resp_9999.status_code == 200
    assert len(resp_9999.json()["items"]) == 1
    assert resp_9999.json()["items"][0]["headcount"] == 1


# --------------------------------------------------------------------------- #
# Endpoint C: GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
def test_leaderboard_competition_ranking_and_ties(client_app: TestClient):
    # Employee A: 85 min (rank 1)
    # Employee B: 60 min (rank 2)
    # Employee C: 60 min (rank 2)
    # Employee D: 35 min (rank 4)
    db.employees.insert_many([
        {"emp_code": "EMP_B", "name": "Bob", "email": "b@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
        {"emp_code": "EMP_A", "name": "Alice", "email": "a@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
        {"emp_code": "EMP_C", "name": "Charlie", "email": "c@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
        {"emp_code": "EMP_D", "name": "Dave", "email": "d@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
    ])
    db.attendance_logs.insert_many([
        {"emp_code": "EMP_A", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 85},
        {"emp_code": "EMP_B", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 60},
        {"emp_code": "EMP_C", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 60},
        {"emp_code": "EMP_D", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 35},
    ])

    # limit=2: Should return rank 1 (A), rank 2 (B), rank 2 (C) -> 3 rows!
    resp = client_app.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP_A"
    assert items[0]["rank"] == 1
    # Tied group B & C sorted by emp_code ascending: EMP_B then EMP_C
    assert items[1]["emp_code"] == "EMP_B"
    assert items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP_C"
    assert items[2]["rank"] == 2

    # limit=3: Still returns A, B, C (rank 1, 2, 2)
    resp3 = client_app.get("/analytics/leaderboard/late?month=2026-07&limit=3")
    assert resp3.status_code == 200
    assert len(resp3.json()["items"]) == 3

    # limit=4: Returns A, B, C, and Dave at rank 4 (rank 3 is skipped!)
    resp4 = client_app.get("/analytics/leaderboard/late?month=2026-07&limit=4")
    assert resp4.status_code == 200
    items4 = resp4.json()["items"]
    assert len(items4) == 4
    assert items4[3]["emp_code"] == "EMP_D"
    assert items4[3]["rank"] == 4


def test_leaderboard_tie_at_rank_1_with_limit_1(client_app: TestClient):
    db.employees.insert_many([
        {"emp_code": "EMP_X", "name": "Xavier", "email": "x@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
        {"emp_code": "EMP_Y", "name": "Yvonne", "email": "y@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
    ])
    db.attendance_logs.insert_many([
        {"emp_code": "EMP_X", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 50},
        {"emp_code": "EMP_Y", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 50},
    ])

    resp = client_app.get("/analytics/leaderboard/late?month=2026-07&limit=1")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1


def test_leaderboard_orphan_and_zero_late_excluded(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP_VALID", "name": "Valid", "email": "v@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"
    })
    db.attendance_logs.insert_many([
        {"emp_code": "ORPHAN", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 100},  # No employee in employees coll
        {"emp_code": "EMP_VALID", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 0},  # 0 late minutes
    ])

    resp = client_app.get("/analytics/leaderboard/late?month=2026-07")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_leaderboard_department_filter_recomputes_ranking(client_app: TestClient):
    db.employees.insert_many([
        {"emp_code": "EMP_ENG", "name": "EngGuy", "email": "eng@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
        {"emp_code": "EMP_HR", "name": "HRGuy", "email": "hr@test.com", "department": "HR", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
    ])
    db.attendance_logs.insert_many([
        {"emp_code": "EMP_ENG", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 100},
        {"emp_code": "EMP_HR", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 20},
    ])

    # Filter HR -> HRGuy must be rank 1 in HR, not rank 2!
    resp = client_app.get("/analytics/leaderboard/late?month=2026-07&department=HR")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["emp_code"] == "EMP_HR"
    assert items[0]["rank"] == 1


def test_leaderboard_validation(client_app: TestClient):
    # Limit bounds: 1 to 50
    resp0 = client_app.get("/analytics/leaderboard/late?month=2026-07&limit=0")
    assert resp0.status_code == 422
    resp51 = client_app.get("/analytics/leaderboard/late?month=2026-07&limit=51")
    assert resp51.status_code == 422

    # Year 0 and invalid months
    assert client_app.get("/analytics/leaderboard/late?month=0000-01").status_code == 422
    assert client_app.get("/analytics/leaderboard/late?month=2026-00").status_code == 422
    assert client_app.get("/analytics/leaderboard/late?month=2026-13").status_code == 422

    # Boundary years
    resp_0001 = client_app.get("/analytics/leaderboard/late?month=0001-01")
    assert resp_0001.status_code == 200
    assert resp_0001.json()["items"] == []

    resp_9999 = client_app.get("/analytics/leaderboard/late?month=9999-12")
    assert resp_9999.status_code == 200
    assert resp_9999.json()["items"] == []


def test_leaderboard_pre_joining_records_excluded(client_app: TestClient):
    # Mid-month joiner: joined 2026-07-15
    db.employees.insert_one({
        "emp_code": "EMP_LEAD_MID",
        "name": "LeadMid",
        "email": "leadmid@test.com",
        "department": "Engineering",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-07-15",
    })
    # Future joiner: joined 2026-08-01
    db.employees.insert_one({
        "emp_code": "EMP_LEAD_FUT",
        "name": "LeadFut",
        "email": "leadfut@test.com",
        "department": "Engineering",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-08-01",
    })

    db.attendance_logs.insert_many([
        # EMP_LEAD_MID: pre-joining record (2026-07-02) -> 60 min late
        {"emp_code": "EMP_LEAD_MID", "date": "2026-07-02", "status": "PRESENT", "late_minutes": 60},
        # EMP_LEAD_MID: post-joining record (2026-07-16) -> 20 min late
        {"emp_code": "EMP_LEAD_MID", "date": "2026-07-16", "status": "PRESENT", "late_minutes": 20},
        # EMP_LEAD_FUT: stray record before joining (2026-07-20) -> 90 min late
        {"emp_code": "EMP_LEAD_FUT", "date": "2026-07-20", "status": "PRESENT", "late_minutes": 90},
    ])

    resp = client_app.get("/analytics/leaderboard/late?month=2026-07")
    assert resp.status_code == 200
    items = resp.json()["items"]
    # EMP_LEAD_FUT is excluded completely because they joined after July!
    # EMP_LEAD_MID is included with only the post-joining late log (20 min, count 1)
    assert len(items) == 1
    assert items[0]["emp_code"] == "EMP_LEAD_MID"
    assert items[0]["total_late_minutes"] == 20
    assert items[0]["late_count"] == 1
    assert items[0]["rank"] == 1


# --------------------------------------------------------------------------- #
# Endpoint D: GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
def test_trend_unknown_department_returns_404(client_app: TestClient):
    resp = client_app.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-05")
    assert resp.status_code == 404


def test_trend_range_validation(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP01", "name": "Alice", "email": "a@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"
    })
    # to < from
    resp_inv = client_app.get("/analytics/departments/Eng/trend?from=2026-07-10&to=2026-07-05")
    assert resp_inv.status_code == 422

    # 92 days accepted: 2026-07-01 to 2026-09-30 is 31 + 31 + 30 = 92 days!
    resp_92 = client_app.get("/analytics/departments/Eng/trend?from=2026-07-01&to=2026-09-30")
    assert resp_92.status_code == 200
    assert len(resp_92.json()["items"]) == 92

    # 93 days rejected: 2026-07-01 to 2026-10-01 is 93 days!
    resp_93 = client_app.get("/analytics/departments/Eng/trend?from=2026-07-01&to=2026-10-01")
    assert resp_93.status_code == 422


def test_trend_single_day(client_app: TestClient):
    db.employees.insert_one({
        "emp_code": "EMP01", "name": "Alice", "email": "a@test.com", "department": "Eng", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"
    })
    resp = client_app.get("/analytics/departments/Eng/trend?from=2026-07-01&to=2026-07-01")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["date"] == "2026-07-01"


def test_trend_comprehensive_moving_avg_and_denominators(client_app: TestClient):
    # 2026-07-01 (Wed) - Alice joined 2026-07-01, Bob joins 2026-07-03 (Fri)
    db.employees.insert_many([
        {"emp_code": "EMP_A", "name": "Alice", "email": "a@test.com", "department": "Dev", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
        {"emp_code": "EMP_B", "name": "Bob", "email": "b@test.com", "department": "Dev", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-03"},
    ])

    # 2026-07-01 (Wed): Alice PRESENT -> present_count=1.0, headcount=1 -> rate=1.0000
    # 2026-07-02 (Thu): No logs -> present_count=0, headcount=1 -> rate=0.0000
    # 2026-07-03 (Fri): Bob joined -> headcount=2. Alice PRESENT (1.0), Bob half_day (0.5) -> present_count=1.5 -> rate = 1.5/2 = 0.7500
    # 2026-07-04 (Sat): Weekend -> is_working_day=False, rate=None
    # 2026-07-05 (Sun): Weekend -> is_working_day=False, rate=None
    # 2026-07-06 (Mon): Both PRESENT -> present_count=2.0, headcount=2 -> rate=1.0000
    # 2026-07-07 (Tue): Both PRESENT -> present_count=2.0, headcount=2 -> rate=1.0000
    db.attendance_logs.insert_many([
        {"emp_code": "EMP_A", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP_A", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP_B", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 0, "half_day": True},
        {"emp_code": "EMP_A", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP_B", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP_A", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP_B", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 0, "half_day": False},
    ])

    resp = client_app.get("/analytics/departments/Dev/trend?from=2026-07-01&to=2026-07-07")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 7

    # Wed 07-01: first row moving avg is rate itself
    assert items[0]["date"] == "2026-07-01"
    assert items[0]["is_working_day"] is True
    assert items[0]["headcount"] == 1
    assert items[0]["present_count"] == 1.0
    assert items[0]["attendance_rate"] == 1.0000
    assert items[0]["moving_avg_7d"] == 1.0000

    # Thu 07-02: rate=0.0000, moving avg = (1.0 + 0.0)/2 = 0.5000
    assert items[1]["date"] == "2026-07-02"
    assert items[1]["headcount"] == 1
    assert items[1]["attendance_rate"] == 0.0000
    assert items[1]["moving_avg_7d"] == 0.5000

    # Fri 07-03: headcount steps up to 2. present_count=1.5, rate=0.7500.
    # moving avg = (1.0 + 0.0 + 0.75)/3 = 0.5833
    assert items[2]["date"] == "2026-07-03"
    assert items[2]["headcount"] == 2
    assert items[2]["present_count"] == 1.5
    assert items[2]["attendance_rate"] == 0.7500
    assert items[2]["moving_avg_7d"] == 0.5833

    # Sat 07-04: weekend rate is None, moving avg remains 0.5833 (skips null)
    assert items[3]["date"] == "2026-07-04"
    assert items[3]["is_working_day"] is False
    assert items[3]["attendance_rate"] is None
    assert items[3]["moving_avg_7d"] == 0.5833

    # Sun 07-05: weekend rate is None, moving avg remains 0.5833
    assert items[4]["date"] == "2026-07-05"
    assert items[4]["is_working_day"] is False
    assert items[4]["attendance_rate"] is None
    assert items[4]["moving_avg_7d"] == 0.5833

    # Mon 07-06: rate=1.0000. Window has [1.0, 0.0, 0.75, None, None, 1.0].
    # Avg over non-nulls: (1.0 + 0.0 + 0.75 + 1.0) / 4 = 2.75 / 4 = 0.6875
    assert items[5]["date"] == "2026-07-06"
    assert items[5]["attendance_rate"] == 1.0000
    assert items[5]["moving_avg_7d"] == 0.6875


def test_trend_working_day_before_anyone_joins(client_app: TestClient):
    # Employee joins 2026-07-05
    db.employees.insert_one({
        "emp_code": "EMP01", "name": "Late", "email": "l@test.com", "department": "Design", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-05"
    })
    # Query 2026-07-01 to 2026-07-03 (before anyone joins)
    resp = client_app.get("/analytics/departments/Design/trend?from=2026-07-01&to=2026-07-03")
    assert resp.status_code == 200
    items = resp.json()["items"]
    for row in items:
        assert row["headcount"] == 0
        assert row["attendance_rate"] is None
        assert row["moving_avg_7d"] is None


# --------------------------------------------------------------------------- #
# Cross-Endpoint Consistency Tests
# --------------------------------------------------------------------------- #
def test_cross_endpoint_consistency(client_app: TestClient):
    # 2 employees in Engineering
    db.employees.insert_many([
        {"emp_code": "EMP_C1", "name": "C1", "email": "c1@test.com", "department": "Engineering", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
        {"emp_code": "EMP_C2", "name": "C2", "email": "c2@test.com", "department": "Engineering", "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-07-01"},
    ])
    db.attendance_logs.insert_many([
        {"emp_code": "EMP_C1", "date": "2026-07-01", "status": "PRESENT", "work_hours": 9.0, "late_minutes": 15, "overtime_minutes": 0, "half_day": False},
        {"emp_code": "EMP_C1", "date": "2026-07-02", "status": "LEAVE", "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
        {"emp_code": "EMP_C2", "date": "2026-07-01", "status": "PRESENT", "work_hours": 4.5, "late_minutes": 0, "overtime_minutes": 0, "half_day": True},
        {"emp_code": "EMP_C2", "date": "2026-07-03", "status": "ON_DUTY", "work_hours": 8.0, "late_minutes": 25, "overtime_minutes": 30, "half_day": False},
    ])

    m1 = client_app.get("/analytics/employees/EMP_C1/monthly?month=2026-07").json()
    m2 = client_app.get("/analytics/employees/EMP_C2/monthly?month=2026-07").json()
    dept = client_app.get("/analytics/departments/summary?month=2026-07&department=Engineering").json()["items"][0]

    # Department present_days == sum of employee present_days
    assert dept["present_days"] == m1["present_days"] + m2["present_days"]
    # Department late_count == sum of employee late_count
    assert dept["late_count"] == m1["late_count"] + m2["late_count"]
    # Department total_late_minutes == sum of employee total_late_minutes
    assert dept["total_late_minutes"] == m1["total_late_minutes"] + m2["total_late_minutes"]
    # Department leave_count == sum of employee leave_days
    assert dept["leave_count"] == m1["leave_days"] + m2["leave_days"]
