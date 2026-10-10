from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from app.main import (
    app,
    db,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    get_attendance_date,
    IST,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    # Keep baseline seeded data or clean up test-created docs
    yield
    db.employees.delete_many({"emp_code": {"$regex": "^EMP9"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP9"}})


# --------------------------------------------------------------------------- #
# Helper tests
# --------------------------------------------------------------------------- #
def test_late_minutes_grace_boundary():
    # Shift 09:30 on 2026-07-06
    d_str = "2026-07-06"
    t_inside_grace = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    t_1sec_late = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    t_45min_late = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    t_early = datetime(2026, 7, 6, 9, 15, 0, tzinfo=IST)

    assert compute_late_minutes(t_inside_grace, "09:30", d_str) == 0
    assert compute_late_minutes(t_1sec_late, "09:30", d_str) == 10
    assert compute_late_minutes(t_45min_late, "09:30", d_str) == 45
    assert compute_late_minutes(t_early, "09:30", d_str) == 0


def test_work_hours_half_up_rounding():
    # 9h 7m = 32820s / 3600 = 9.11666... -> 9.12
    t1 = datetime(2026, 7, 6, 9, 28, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=timezone.utc)
    assert compute_work_hours(t1, t2) == 9.12

    # Boundary: exactly .005 hours = 18 seconds
    # 1 hour + 18 seconds = 3618 seconds -> 3618 / 3600 = 1.005 -> half-up rounds to 1.01
    t3 = datetime(2026, 7, 6, 9, 0, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 7, 6, 10, 0, 18, tzinfo=timezone.utc)
    assert compute_work_hours(t3, t4) == 1.01


def test_overtime_threshold_and_overnight():
    # Shift 09:30 - 18:30 on 2026-07-06
    # Out at 18:59 (29 min OT) -> below 30 -> 0
    t_out_29 = datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST)
    assert compute_overtime(t_out_29, "18:30", "09:30", "2026-07-06") == 0

    # Out at 19:00 (30 min OT) -> exactly 30 -> 30
    t_out_30 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(t_out_30, "18:30", "09:30", "2026-07-06") == 30

    # Overnight shift: 22:00 - 06:00
    # Out at 06:40 on 2026-07-07 for 2026-07-06 attendance date -> 40 min OT
    t_out_over = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(t_out_over, "06:00", "22:00", "2026-07-06") == 40


def test_overnight_attendance_date_derivation():
    # Shift 22:00 - 06:00
    # Punch-in at 03:00 on 2026-07-07 -> belongs to 2026-07-06
    t_am = datetime(2026, 7, 7, 3, 0, 0, tzinfo=IST)
    assert get_attendance_date(t_am, "22:00", "06:00") == "2026-07-06"

    # Punch-in at 22:30 on 2026-07-06 -> belongs to 2026-07-06
    t_pm = datetime(2026, 7, 6, 22, 30, 0, tzinfo=IST)
    assert get_attendance_date(t_pm, "22:00", "06:00") == "2026-07-06"

    # Punch-in at 06:00 on 2026-07-07 -> not strictly before 06:00 -> belongs to 2026-07-07
    t_exact = datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)
    assert get_attendance_date(t_exact, "22:00", "06:00") == "2026-07-07"


# --------------------------------------------------------------------------- #
# Endpoint tests
# --------------------------------------------------------------------------- #
def test_health_check():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_employee_creation_and_duplicate():
    emp_payload = {
        "emp_code": "EMP9001",
        "name": "Test Employee",
        "email": "test9001@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
    }
    # Success 201
    res = client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9001"
    assert "_id" not in data
    assert isinstance(data["created_at"], int)

    # Duplicate 409
    res_dup = client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409


def test_employee_validation_errors():
    # Bad emp_code pattern
    res = client.post("/employees", json={
        "emp_code": "BADCODE",
        "name": "Bad",
        "email": "bad@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
    })
    assert res.status_code == 422

    # Same shift_start and shift_end
    res = client.post("/employees", json={
        "emp_code": "EMP9002",
        "name": "Same Shift",
        "email": "same@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-15",
    })
    assert res.status_code == 422


def test_employee_listing_pagination_and_filter():
    # Insert two test employees
    db.employees.insert_many([
        {
            "emp_code": "EMP9010",
            "name": "Test Alpha",
            "email": "alpha@example.com",
            "department": "Finance",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-15",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "emp_code": "EMP9011",
            "name": "Test Beta",
            "email": "beta@example.com",
            "department": "Finance",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-15",
            "created_at": datetime.now(timezone.utc),
        },
    ])

    # Filter Finance
    res = client.get("/employees?department=Finance")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2

    # Pagination skip = (page - 1) * page_size
    res_p1 = client.get("/employees?department=Finance&page=1&page_size=1")
    assert res_p1.status_code == 200
    assert len(res_p1.json()["items"]) == 1
    assert res_p1.json()["items"][0]["emp_code"] == "EMP9010"

    res_p2 = client.get("/employees?department=Finance&page=2&page_size=1")
    assert res_p2.status_code == 200
    assert len(res_p2.json()["items"]) == 1
    assert res_p2.json()["items"][0]["emp_code"] == "EMP9011"

    # page < 1 -> 422
    assert client.get("/employees?page=0").status_code == 422


def test_punch_in_nonexistent_employee():
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404


def test_punch_in_invalid_status():
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "LEAVE"})
    assert res.status_code == 422


def test_punch_in_seconds_timestamp_rejected():
    # Value looking like seconds (< 100000000000)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
    assert res.status_code == 422


def test_punch_in_success_and_duplicate():
    # Create employee with 09:30 shift
    db.employees.insert_one({
        "emp_code": "EMP9020",
        "name": "Punch Tester",
        "email": "punch@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
        "created_at": datetime.now(timezone.utc),
    })

    # 2026-08-10 09:40:01 IST
    # 09:40:01 IST = 04:10:01 UTC
    dt_late = datetime(2026, 8, 10, 4, 10, 1, tzinfo=timezone.utc)
    ts_ms = int(dt_late.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9020",
        "punched_at": ts_ms,
        "status": "PRESENT",
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9020"
    assert data["date"] == "2026-08-10"
    assert data["late_minutes"] == 10
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert "_id" not in data
    assert "id" not in data

    # Concurrent / repeated punch in for same day -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9020",
        "punched_at": ts_ms,
        "status": "PRESENT",
    })
    assert res_dup.status_code == 409


def test_attendance_listing_filters_and_validation():
    # date_from > date_to -> 422
    res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res.status_code == 422

    # Listing works
    res_list = client.get("/attendance?page=1&page_size=5")
    assert res_list.status_code == 200
    data = res_list.json()
    assert "items" in data
    assert "total" in data
    for item in data["items"]:
        assert "_id" not in item
        assert "id" not in item


def test_punch_in_concurrency():
    from concurrent.futures import ThreadPoolExecutor

    db.employees.insert_one({
        "emp_code": "EMP9030",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
        "created_at": datetime.now(timezone.utc),
    })

    dt = datetime(2026, 8, 11, 4, 0, 0, tzinfo=timezone.utc)
    ts_ms = int(dt.timestamp() * 1000)
    payload = {
        "emp_code": "EMP9030",
        "punched_at": ts_ms,
        "status": "PRESENT",
    }

    def do_punch():
        with TestClient(app) as tc:
            return tc.post("/attendance/punch-in", json=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_punch)
        f2 = executor.submit(do_punch)
        codes = sorted([f1.result(), f2.result()])

    assert codes == [201, 409]

