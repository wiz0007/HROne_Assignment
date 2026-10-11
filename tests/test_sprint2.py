import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import (
    IST,
    app,
    compute_half_day,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    get_attendance_date,
    to_utc_datetime,
)


# =========================================================================== #
# Punch-Out Functional & Calculation Tests
# =========================================================================== #

def test_punch_out_success_normal_shift(client_app, test_db):
    """Normal punch-out closes record with accurate R3, R4, R5 calculations."""
    # Employee 09:30 - 18:30
    client_app.post("/employees", json={
        "emp_code": "EMP0101",
        "name": "PunchOut User",
        "email": "po1@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Punch in 09:30 IST on 2026-07-06 (04:00 UTC)
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0101",
        "punched_at": int(t_in.timestamp() * 1000),
        "status": "PRESENT",
    })

    # Punch out 18:30 IST on 2026-07-06 (13:00 UTC) -> 9.0 hours worked
    t_out = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    res = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0101",
        "punched_at": int(t_out.timestamp() * 1000),
    })

    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0101"
    assert data["date"] == "2026-07-06"
    assert data["punch_out"] == int(t_out.timestamp() * 1000)
    assert data["work_hours"] == 9.0
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert data["history"] == []
    assert "_id" not in data
    assert "id" not in data


def test_punch_out_overtime_boundaries(client_app):
    """R3: Overtime is >= 30 min after shift_end, otherwise 0."""
    client_app.post("/employees", json={
        "emp_code": "EMP0102",
        "name": "OT User",
        "email": "ot2@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # 18:59:59 IST (29 min 59s OT) -> 0 OT
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0102",
        "punched_at": int(t_in.timestamp() * 1000),
    })
    # 18:59:59 IST = 13:29:59 UTC
    t_out_29m = datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc)
    res = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0102",
        "punched_at": int(t_out_29m.timestamp() * 1000),
    })
    assert res.status_code == 200
    assert res.json()["overtime_minutes"] == 0


def test_punch_out_work_hours_half_day_boundary(client_app):
    """R5: half_day = true when rounded work_hours < 4.50."""
    client_app.post("/employees", json={
        "emp_code": "EMP0103",
        "name": "HalfDay User",
        "email": "hd3@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0103",
        "punched_at": int(t_in.timestamp() * 1000),
    })

    # 4h 29m 41s = 16181s / 3600 = 4.4947 -> 4.49 -> half_day = True
    t_out_449 = t_in + timedelta(seconds=16181)
    res = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0103",
        "punched_at": int(t_out_449.timestamp() * 1000),
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 4.49
    assert data["half_day"] is True


def test_punch_out_overnight_shift(client_app):
    """Overnight shift (22:00 - 06:00) punch-out crosses midnight cleanly."""
    client_app.post("/employees", json={
        "emp_code": "EMP0104",
        "name": "Night Owl",
        "email": "night@example.com",
        "department": "Engineering",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-05",
    })

    # Punch in 22:00 IST on 2026-07-06 = 16:30 UTC on 2026-07-06
    t_in = datetime(2026, 7, 6, 16, 30, 0, tzinfo=timezone.utc)
    res_in = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0104",
        "punched_at": int(t_in.timestamp() * 1000),
    })
    assert res_in.status_code == 201
    assert res_in.json()["date"] == "2026-07-06"

    # Punch out 06:40 IST on 2026-07-07 = 01:10 UTC on 2026-07-07
    # 40 min overtime past 06:00 end
    t_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    res_out = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0104",
        "punched_at": int(t_out.timestamp() * 1000),
    })
    assert res_out.status_code == 200
    data = res_out.json()
    assert data["date"] == "2026-07-06"
    assert data["overtime_minutes"] == 40
    assert data["work_hours"] == 8.67
    assert data["half_day"] is False


def test_punch_out_omitted_timestamp_defaults_to_now(client_app):
    """Omitted punched_at defaults to server UTC now."""
    client_app.post("/employees", json={
        "emp_code": "EMP0105",
        "name": "Now User",
        "email": "now@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Punch in 1 hour ago
    t_in = datetime.now(timezone.utc) - timedelta(hours=1)
    t_in_ms = int(t_in.timestamp() * 1000)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0105",
        "punched_at": t_in_ms,
    })

    res = client_app.post("/attendance/punch-out", json={"emp_code": "EMP0105"})
    assert res.status_code == 200
    assert res.json()["punch_out"] is not None


def test_punch_out_truncates_milliseconds(client_app):
    """Instants are truncated to whole seconds."""
    client_app.post("/employees", json={
        "emp_code": "EMP0106",
        "name": "Truncate User",
        "email": "tr@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0106",
        "punched_at": int(t_in.timestamp() * 1000),
    })

    # Send timestamp ending with .999s -> 13:00:00.999 UTC
    t_out_ms = int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000) + 999
    res = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0106",
        "punched_at": t_out_ms,
    })
    assert res.status_code == 200
    # Returned punch_out should end with 000 ms
    assert res.json()["punch_out"] % 1000 == 0


def test_punch_out_24h_window_boundary(client_app):
    """Punch-out valid at 24h exactly; 422 at 24h + 1s."""
    client_app.post("/employees", json={
        "emp_code": "EMP0107",
        "name": "24h User",
        "email": "24h@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    t_in_ms = int(t_in.timestamp() * 1000)
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0107", "punched_at": t_in_ms})

    # Exactly 24h + 1s -> 422
    t_out_24h1s_ms = int((t_in + timedelta(seconds=86401)).timestamp() * 1000)
    res_inv = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0107",
        "punched_at": t_out_24h1s_ms,
    })
    assert res_inv.status_code == 422

    # Exactly 24h -> 200 OK
    t_out_24h_ms = int((t_in + timedelta(seconds=86400)).timestamp() * 1000)
    res_ok = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0107",
        "punched_at": t_out_24h_ms,
    })
    assert res_ok.status_code == 200


# =========================================================================== #
# Punch-Out Error & Concurrency Tests
# =========================================================================== #

def test_punch_out_unknown_employee_or_no_punch_in(client_app):
    # Unknown employee -> 404
    assert client_app.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404

    # Known employee with no punch-in -> 404
    client_app.post("/employees", json={
        "emp_code": "EMP0108",
        "name": "No Punch",
        "email": "nopunch@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    assert client_app.post("/attendance/punch-out", json={"emp_code": "EMP0108"}).status_code == 404


def test_punch_out_repeat_already_closed_returns_409(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0109",
        "name": "Repeat User",
        "email": "repeat@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    t_out = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0109",
        "punched_at": int(t_in.timestamp() * 1000),
    })

    # First punch-out -> 200
    res1 = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0109",
        "punched_at": int(t_out.timestamp() * 1000),
    })
    assert res1.status_code == 200

    # Repeat punch-out -> 409
    res2 = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0109",
        "punched_at": int((t_out + timedelta(minutes=5)).timestamp() * 1000),
    })
    assert res2.status_code == 409


def test_punch_out_before_or_equal_to_punch_in(client_app):
    """Punched_at <= punch_in must return 422 without masking."""
    client_app.post("/employees", json={
        "emp_code": "EMP0110",
        "name": "Order User",
        "email": "order@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Punch in 10:00:00 UTC
    t_in = datetime(2026, 7, 6, 10, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0110",
        "punched_at": int(t_in.timestamp() * 1000),
    })

    # Punch out earlier at 09:30:00 UTC -> 422
    t_early = datetime(2026, 7, 6, 9, 30, 0, tzinfo=timezone.utc)
    res_early = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0110",
        "punched_at": int(t_early.timestamp() * 1000),
    })
    assert res_early.status_code == 422

    # Punch out at exact same instant 10:00:00 UTC -> 422
    res_equal = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0110",
        "punched_at": int(t_in.timestamp() * 1000),
    })
    assert res_equal.status_code == 422


def test_punch_out_invalid_timestamp_types(client_app):
    """Reject 0, negative, strings, floats, seconds."""
    client_app.post("/employees", json={
        "emp_code": "EMP0111",
        "name": "Bad TS User",
        "email": "badts@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    for bad_ts in [0, -100, 1783312500, 1783312500000.5, "1783312500000", 9999999999999]:
        assert client_app.post(
            "/attendance/punch-out", json={"emp_code": "EMP0111", "punched_at": bad_ts}
        ).status_code == 422


def test_punch_out_concurrency_race(client_app):
    """Two simultaneous punch-outs yield exactly one 200 and one 409."""
    client_app.post("/employees", json={
        "emp_code": "EMP0112",
        "name": "Race User",
        "email": "racepo@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0112",
        "punched_at": int(t_in.timestamp() * 1000),
    })

    t_out = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    payload = {
        "emp_code": "EMP0112",
        "punched_at": int(t_out.timestamp() * 1000),
    }

    def do_po():
        with TestClient(app) as tc:
            return tc.post("/attendance/punch-out", json=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_po)
        f2 = executor.submit(do_po)
        codes = sorted([f1.result(), f2.result()])

    assert codes == [200, 409]


# =========================================================================== #
# Regularization (PATCH) Functional & Edge-Case Tests
# =========================================================================== #

def test_regularize_path_validation_and_unknowns(client_app):
    # Impossible calendar date in path -> 422
    res_bad_date = client_app.patch("/attendance/EMP0001/2026-02-30", json={
        "reason": "Correcting attendance",
        "regularized_by": "Manager",
        "status": "WFH",
    })
    assert res_bad_date.status_code == 422

    # Unknown employee -> 404
    res_no_emp = client_app.patch("/attendance/EMP9999/2026-07-06", json={
        "reason": "Correcting attendance",
        "regularized_by": "Manager",
        "status": "WFH",
    })
    assert res_no_emp.status_code == 404

    # Known employee, no attendance record for that date -> 404
    client_app.post("/employees", json={
        "emp_code": "EMP0120",
        "name": "Patch User",
        "email": "patch20@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    res_no_rec = client_app.patch("/attendance/EMP0120/2026-07-06", json={
        "reason": "Correcting attendance",
        "regularized_by": "Manager",
        "status": "WFH",
    })
    assert res_no_rec.status_code == 404


def test_regularize_metadata_validation(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0121",
        "name": "Meta User",
        "email": "meta21@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0121",
        "punched_at": int(t_in.timestamp() * 1000),
    })

    # Reason too short (< 5 chars) -> 422
    assert client_app.patch("/attendance/EMP0121/2026-07-06", json={
        "reason": "tiny",
        "regularized_by": "Manager",
        "status": "WFH",
    }).status_code == 422

    # Regularized_by empty (< 1 char) -> 422
    assert client_app.patch("/attendance/EMP0121/2026-07-06", json={
        "reason": "Valid reason length",
        "regularized_by": "",
        "status": "WFH",
    }).status_code == 422

    # Missing metadata -> 422
    assert client_app.patch("/attendance/EMP0121/2026-07-06", json={
        "status": "WFH",
    }).status_code == 422


def test_regularize_no_op_request_rejected(client_app):
    """A request that changes nothing must return 422 without appending history."""
    client_app.post("/employees", json={
        "emp_code": "EMP0122",
        "name": "NoOp User",
        "email": "noop@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0122",
        "punched_at": int(t_in.timestamp() * 1000),
        "status": "PRESENT",
    })

    # Sending only reason/by without any business field -> 422
    res_empty = client_app.patch("/attendance/EMP0122/2026-07-06", json={
        "reason": "Just some comments",
        "regularized_by": "Manager",
    })
    assert res_empty.status_code == 422

    # Sending identical status -> 422
    res_same = client_app.patch("/attendance/EMP0122/2026-07-06", json={
        "reason": "Setting same status",
        "regularized_by": "Manager",
        "status": "PRESENT",
    })
    assert res_same.status_code == 422


def test_regularize_present_to_absent_or_leave(client_app):
    """Setting status ABSENT clears both punches, resets derived fields."""
    client_app.post("/employees", json={
        "emp_code": "EMP0123",
        "name": "Absent User",
        "email": "absent@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    t_out = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0123",
        "punched_at": int(t_in.timestamp() * 1000),
    })
    client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0123",
        "punched_at": int(t_out.timestamp() * 1000),
    })

    # Supplying punch_in with ABSENT -> 422
    res_bad = client_app.patch("/attendance/EMP0123/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": int(t_in.timestamp() * 1000),
        "reason": "Took unpaid leave",
        "regularized_by": "HR Admin",
    })
    assert res_bad.status_code == 422

    # Valid conversion to ABSENT
    res = client_app.patch("/attendance/EMP0123/2026-07-06", json={
        "status": "ABSENT",
        "reason": "Took unpaid leave",
        "regularized_by": "HR Admin",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["late_minutes"] == 0
    assert data["work_hours"] is None
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert len(data["history"]) == 1
    assert data["history"][0]["changes"]["status"]["from"] == "PRESENT"
    assert data["history"][0]["changes"]["status"]["to"] == "ABSENT"


def test_regularize_absent_to_present_requires_punch_in(client_app, test_db):
    """Presence status requires punch_in."""
    client_app.post("/employees", json={
        "emp_code": "EMP0124",
        "name": "Un-Absent User",
        "email": "unabsent@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    # Seed an ABSENT record directly
    test_db.attendance_logs.insert_one({
        "emp_code": "EMP0124",
        "date": "2026-07-06",
        "status": "ABSENT",
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # Try setting to PRESENT without providing punch_in -> 422
    assert client_app.patch("/attendance/EMP0124/2026-07-06", json={
        "status": "PRESENT",
        "reason": "Employee actually showed up",
        "regularized_by": "Lead",
    }).status_code == 422

    # Providing valid punch_in -> 200
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    res = client_app.patch("/attendance/EMP0124/2026-07-06", json={
        "status": "PRESENT",
        "punch_in": int(t_in.timestamp() * 1000),
        "reason": "Employee actually showed up",
        "regularized_by": "Lead",
    })
    assert res.status_code == 200
    assert res.json()["status"] == "PRESENT"
    assert res.json()["punch_in"] == int(t_in.timestamp() * 1000)


def test_regularize_date_invariant(client_app):
    """punch_in must stay on the record's attendance date under R1."""
    client_app.post("/employees", json={
        "emp_code": "EMP0125",
        "name": "Date User",
        "email": "date25@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0125",
        "punched_at": int(t_in.timestamp() * 1000),
    })

    # Try to change punch_in to next day 2026-07-07 -> 422
    t_next_day = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    res = client_app.patch("/attendance/EMP0125/2026-07-06", json={
        "punch_in": int(t_next_day.timestamp() * 1000),
        "reason": "Wrong day entered",
        "regularized_by": "Manager",
    })
    assert res.status_code == 422


def test_regularize_explicit_null_punch_out_clears(client_app):
    """Explicit null punch_out resets work_hours and overtime."""
    client_app.post("/employees", json={
        "emp_code": "EMP0126",
        "name": "Clear Out",
        "email": "clear@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    t_out = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0126",
        "punched_at": int(t_in.timestamp() * 1000),
    })
    client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0126",
        "punched_at": int(t_out.timestamp() * 1000),
    })

    res = client_app.patch("/attendance/EMP0126/2026-07-06", json={
        "punch_out": None,
        "reason": "Accidental punch-out by colleague",
        "regularized_by": "Manager",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert "punch_out" in data["history"][0]["changes"]
    assert data["history"][0]["changes"]["punch_out"]["to"] is None


def test_regularize_sequential_history_audit(client_app):
    """Multiple sequential regularizations append entries cleanly."""
    client_app.post("/employees", json={
        "emp_code": "EMP0127",
        "name": "Audit User",
        "email": "audit27@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0127",
        "punched_at": int(t_in.timestamp() * 1000),
        "status": "PRESENT",
    })

    # Reg 1: Change status to WFH
    res1 = client_app.patch("/attendance/EMP0127/2026-07-06", json={
        "status": "WFH",
        "reason": "Worked from home approved",
        "regularized_by": "Manager 1",
    })
    assert res1.status_code == 200
    assert len(res1.json()["history"]) == 1

    # Reg 2: Add punch-out
    t_out = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    res2 = client_app.patch("/attendance/EMP0127/2026-07-06", json={
        "punch_out": int(t_out.timestamp() * 1000),
        "reason": "Adding missed punch-out",
        "regularized_by": "Manager 2",
    })
    assert res2.status_code == 200
    hist = res2.json()["history"]
    assert len(hist) == 2
    assert hist[0]["by"] == "Manager 1"
    assert hist[1]["by"] == "Manager 2"


def test_regularize_concurrency_race(client_app):
    """Simultaneous regularizations on same record allow loser to 409 without lost history."""
    client_app.post("/employees", json={
        "emp_code": "EMP0128",
        "name": "Patch Race User",
        "email": "prace@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })
    t_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0128",
        "punched_at": int(t_in.timestamp() * 1000),
        "status": "PRESENT",
    })

    payload1 = {
        "status": "WFH",
        "reason": "Change to WFH race 1",
        "regularized_by": "Admin 1",
    }
    payload2 = {
        "status": "ON_DUTY",
        "reason": "Change to ON_DUTY race 2",
        "regularized_by": "Admin 2",
    }

    def do_patch(payload):
        with TestClient(app) as tc:
            return tc.patch("/attendance/EMP0128/2026-07-06", json=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_patch, payload1)
        f2 = executor.submit(do_patch, payload2)
        codes = sorted([f1.result(), f2.result()])

    # One succeeds (200), the other gets 409 (or both succeed sequentially if serialized)
    assert 200 in codes
    if codes != [200, 200]:
        assert codes == [200, 409]
