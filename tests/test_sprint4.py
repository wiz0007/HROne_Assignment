"""
Sprint 4 Tests: Admin Explain Endpoints, Query Index Verification & Query Parity
"""
from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException

from app.main import (
    ExplainEndpoint,
    StatusEnum,
    build_attendance_list_query,
    build_department_summary_pipeline,
    build_department_trend_pipeline,
    build_employee_monthly_pipeline,
    build_late_leaderboard_pipeline,
    create_required_indexes,
)


# --------------------------------------------------------------------------- #
# 1. Unit Tests: Query and Pipeline Builders
# --------------------------------------------------------------------------- #
def test_explain_endpoint_enum_members():
    assert ExplainEndpoint.ATTENDANCE_LIST.value == "attendance_list"
    assert ExplainEndpoint.EMPLOYEE_MONTHLY.value == "employee_monthly"
    assert ExplainEndpoint.DEPARTMENT_SUMMARY.value == "department_summary"
    assert ExplainEndpoint.LATE_LEADERBOARD.value == "late_leaderboard"
    assert ExplainEndpoint.DEPARTMENT_TREND.value == "department_trend"


def test_build_attendance_list_query_defaults():
    q, sort_spec, skip, limit_val = build_attendance_list_query()
    assert q == {}
    assert sort_spec == [("date", -1), ("emp_code", 1)]
    assert skip == 0
    assert limit_val == 20


def test_build_attendance_list_query_filters():
    q, sort_spec, skip, limit_val = build_attendance_list_query(
        emp_code="EMP0001",
        date_from="2026-03-01",
        date_to="2026-03-15",
        status=StatusEnum.PRESENT,
        page=3,
        page_size=15,
    )
    assert q["emp_code"] == "EMP0001"
    assert q["date"] == {"$gte": "2026-03-01", "$lte": "2026-03-15"}
    assert q["status"] == "PRESENT"
    assert sort_spec == [("date", -1), ("emp_code", 1)]
    assert skip == 30
    assert limit_val == 15


def test_build_attendance_list_query_validation():
    with pytest.raises(HTTPException) as exc:
        build_attendance_list_query(date_from="2026-03-20", date_to="2026-03-10")
    assert exc.value.status_code == 422
    assert "date_from must not be greater than date_to" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        build_attendance_list_query(date_from="invalid-date")
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        build_attendance_list_query(status="INVALID_STATUS")
    assert exc.value.status_code == 422


def test_build_employee_monthly_pipeline_structure():
    col, pipe = build_employee_monthly_pipeline("EMP0001", "2026-03")
    assert col == "attendance_logs"
    assert len(pipe) == 3
    match_stage = pipe[0]["$match"]
    assert match_stage["emp_code"] == "EMP0001"
    assert match_stage["date"] == {"$gte": "2026-03-01", "$lte": "2026-03-31"}

    # Effective start override
    col2, pipe2 = build_employee_monthly_pipeline("EMP0001", "2026-03", effective_start="2026-03-15")
    assert col2 == "attendance_logs"
    assert pipe2[0]["$match"]["date"]["$gte"] == "2026-03-15"


def test_build_department_summary_pipeline_structure():
    col, pipe_all = build_department_summary_pipeline("2026-03")
    assert col == "employees"
    assert pipe_all[0]["$match"] == {"joined_on": {"$lte": "2026-03-31"}}

    col, pipe_dept = build_department_summary_pipeline("2026-03", department="Engineering")
    assert pipe_dept[0]["$match"] == {"joined_on": {"$lte": "2026-03-31"}, "department": "Engineering"}


def test_build_late_leaderboard_pipeline_structure():
    col, pipe = build_late_leaderboard_pipeline("2026-03", limit=5)
    assert col == "attendance_logs"
    assert pipe[0]["$match"] == {
        "date": {"$gte": "2026-03-01", "$lte": "2026-03-31"},
        "late_minutes": {"$gt": 0},
    }
    # Check limit stage inside pipeline
    rank_limit_stage = [s for s in pipe if "$match" in s and "rank" in s["$match"]]
    assert len(rank_limit_stage) == 1
    assert rank_limit_stage[0]["$match"]["rank"] == {"$lte": 5}

    col, pipe_dept = build_late_leaderboard_pipeline("2026-03", limit=10, department="Sales")
    dept_match = [s for s in pipe_dept if "$match" in s and "emp.department" in s["$match"]]
    assert len(dept_match) == 1
    assert dept_match[0]["$match"]["emp.department"] == "Sales"


def test_build_department_trend_pipeline_structure():
    col, pipe = build_department_trend_pipeline("Engineering", "2026-03-01", "2026-03-07")
    assert col == "employees"
    assert pipe[0]["$match"] == {"department": "Engineering"}
    assert pipe[1]["$limit"] == 1
    assert pipe[2]["$project"]["offset"]["$range"] == [0, 7]


def test_build_department_trend_pipeline_validation():
    with pytest.raises(HTTPException) as exc:
        build_department_trend_pipeline("Engineering", "2026-03-10", "2026-03-05")
    assert exc.value.status_code == 422
    assert "must be on or after" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        build_department_trend_pipeline("Engineering", "2026-01-01", "2026-04-15")  # > 92 days
    assert exc.value.status_code == 422
    assert "exceeds maximum 92 days" in exc.value.detail


# --------------------------------------------------------------------------- #
# 2. Endpoint Routing & Alias Validation
# --------------------------------------------------------------------------- #
def test_explain_all_valid_aliases(client_app):
    # 1. attendance_list
    r1 = client_app.get("/admin/explain/attendance_list")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["endpoint"] == "attendance_list"
    assert d1["collection"] == "attendance_logs"
    assert isinstance(d1["explain"], dict)

    # 2. employee_monthly
    r2 = client_app.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-03")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["endpoint"] == "employee_monthly"
    assert d2["collection"] == "attendance_logs"
    assert isinstance(d2["explain"], dict)

    # 3. department_summary
    r3 = client_app.get("/admin/explain/department_summary?month=2026-03")
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["endpoint"] == "department_summary"
    assert d3["collection"] == "employees"
    assert isinstance(d3["explain"], dict)

    # 4. late_leaderboard
    r4 = client_app.get("/admin/explain/late_leaderboard?month=2026-03")
    assert r4.status_code == 200
    d4 = r4.json()
    assert d4["endpoint"] == "late_leaderboard"
    assert d4["collection"] == "attendance_logs"
    assert isinstance(d4["explain"], dict)

    # 5. department_trend
    r5 = client_app.get("/admin/explain/department_trend?department=Engineering&from=2026-03-01&to=2026-03-07")
    assert r5.status_code == 200
    d5 = r5.json()
    assert d5["endpoint"] == "department_trend"
    assert d5["collection"] == "employees"
    assert isinstance(d5["explain"], dict)


def test_explain_unknown_endpoint_alias(client_app):
    for bad in ["unknown", "attendance", "monthly", "leaderboard", "trend", "users"]:
        res = client_app.get(f"/admin/explain/{bad}")
        assert res.status_code == 422


# --------------------------------------------------------------------------- #
# 3. Parameter Validation Parity across Endpoints
# --------------------------------------------------------------------------- #
def test_explain_attendance_list_parameter_validation(client_app):
    # Invalid date_from format
    res = client_app.get("/admin/explain/attendance_list?date_from=2026-13-01")
    assert res.status_code == 422

    # Reversed dates
    res = client_app.get("/admin/explain/attendance_list?date_from=2026-03-10&date_to=2026-03-01")
    assert res.status_code == 422

    # Invalid status
    res = client_app.get("/admin/explain/attendance_list?status=UNKNOWN_STATUS")
    assert res.status_code == 422

    # Invalid page / page_size
    assert client_app.get("/admin/explain/attendance_list?page=0").status_code == 422
    assert client_app.get("/admin/explain/attendance_list?page_size=0").status_code == 422
    assert client_app.get("/admin/explain/attendance_list?page_size=101").status_code == 422


def test_explain_employee_monthly_parameter_validation(client_app):
    # Missing emp_code
    res = client_app.get("/admin/explain/employee_monthly?month=2026-03")
    assert res.status_code == 422

    # Missing month
    res = client_app.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert res.status_code == 422

    # Invalid month formats
    for bad_m in ["2026-1", "2026-13", "0000-01", "abc"]:
        res = client_app.get(f"/admin/explain/employee_monthly?emp_code=EMP0001&month={bad_m}")
        assert res.status_code == 422


def test_explain_department_summary_parameter_validation(client_app):
    # Missing month
    res = client_app.get("/admin/explain/department_summary")
    assert res.status_code == 422

    # Invalid month
    res = client_app.get("/admin/explain/department_summary?month=2026-00")
    assert res.status_code == 422


def test_explain_late_leaderboard_parameter_validation(client_app):
    # Missing month
    res = client_app.get("/admin/explain/late_leaderboard")
    assert res.status_code == 422

    # Invalid month
    res = client_app.get("/admin/explain/late_leaderboard?month=2026-14")
    assert res.status_code == 422

    # Invalid limits
    assert client_app.get("/admin/explain/late_leaderboard?month=2026-03&limit=0").status_code == 422
    assert client_app.get("/admin/explain/late_leaderboard?month=2026-03&limit=51").status_code == 422


def test_explain_department_trend_parameter_validation(client_app):
    # Missing department
    res = client_app.get("/admin/explain/department_trend?from=2026-03-01&to=2026-03-07")
    assert res.status_code == 422

    # Missing from
    res = client_app.get("/admin/explain/department_trend?department=Engineering&to=2026-03-07")
    assert res.status_code == 422

    # Missing to
    res = client_app.get("/admin/explain/department_trend?department=Engineering&from=2026-03-01")
    assert res.status_code == 422

    # Invalid calendar dates
    res = client_app.get("/admin/explain/department_trend?department=Engineering&from=2026-02-30&to=2026-03-05")
    assert res.status_code == 422

    # Reversed date range
    res = client_app.get("/admin/explain/department_trend?department=Engineering&from=2026-03-07&to=2026-03-01")
    assert res.status_code == 422

    # Exceeding 92 days
    res = client_app.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-04-15")
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# 4. Filter Reflection Parity in Explain Output
# --------------------------------------------------------------------------- #
def test_explain_filter_parity_attendance_list(client_app):
    res = client_app.get(
        "/admin/explain/attendance_list?emp_code=EMP1234&date_from=2026-03-01&date_to=2026-03-10&status=WFH&page=2&page_size=10"
    )
    assert res.status_code == 200
    data = res.json()
    command = data["explain"].get("command", {})
    # Verify filter and pagination are reflected in the explain command
    assert command.get("filter", {}).get("emp_code") == "EMP1234"
    assert command.get("filter", {}).get("status") == "WFH"
    assert command.get("filter", {}).get("date") == {"$gte": "2026-03-01", "$lte": "2026-03-10"}
    assert command.get("skip") == 10
    assert command.get("limit") == 10


def test_explain_filter_parity_department_summary(client_app):
    # Without department
    r1 = client_app.get("/admin/explain/department_summary?month=2026-03")
    assert r1.status_code == 200
    p1 = r1.json()["explain"].get("command", {}).get("pipeline", [])
    assert "department" not in p1[0]["$match"]

    # With department
    r2 = client_app.get("/admin/explain/department_summary?month=2026-03&department=Finance")
    assert r2.status_code == 200
    p2 = r2.json()["explain"].get("command", {}).get("pipeline", [])
    assert p2[0]["$match"].get("department") == "Finance"


def test_explain_filter_parity_late_leaderboard(client_app):
    # With limit and department
    res = client_app.get("/admin/explain/late_leaderboard?month=2026-03&limit=7&department=Marketing")
    assert res.status_code == 200
    pipeline = res.json()["explain"].get("command", {}).get("pipeline", [])
    # Check department filter present in pipeline
    dept_matches = [s for s in pipeline if "$match" in s and "emp.department" in s["$match"]]
    assert len(dept_matches) == 1
    assert dept_matches[0]["$match"]["emp.department"] == "Marketing"

    # Check rank limit present
    rank_matches = [s for s in pipeline if "$match" in s and "rank" in s["$match"]]
    assert len(rank_matches) == 1
    assert rank_matches[0]["$match"]["rank"] == {"$lte": 7}


def test_explain_filter_parity_department_trend(client_app):
    res = client_app.get("/admin/explain/department_trend?department=Support&from=2026-03-01&to=2026-03-10")
    assert res.status_code == 200
    pipeline = res.json()["explain"].get("command", {}).get("pipeline", [])
    assert pipeline[0]["$match"]["department"] == "Support"
    assert pipeline[2]["$project"]["offset"]["$range"] == [0, 10]


# --------------------------------------------------------------------------- #
# 5. Required Indexes & Idempotent Startup
# --------------------------------------------------------------------------- #
def test_required_indexes_presence(test_db):
    emp_indexes = test_db.employees.index_information()
    att_indexes = test_db.attendance_logs.index_information()

    # Employees collection indexes
    assert "emp_code_1" in emp_indexes
    assert emp_indexes["emp_code_1"].get("unique") is True
    assert "department_1" in emp_indexes
    assert "department_1_emp_code_1" in emp_indexes
    assert "department_1_joined_on_1" in emp_indexes
    assert "joined_on_1_department_1" in emp_indexes

    # Attendance logs collection indexes
    assert "emp_code_1_date_1" in att_indexes
    assert att_indexes["emp_code_1_date_1"].get("unique") is True
    assert "date_-1_emp_code_1" in att_indexes
    assert "date_1_late_minutes_1" in att_indexes
    assert "date_1_emp_code_1" in att_indexes


def test_create_required_indexes_idempotency(test_db):
    # Calling multiple times must succeed without error
    create_required_indexes(test_db)
    create_required_indexes(test_db)


def test_create_required_indexes_fail_fast():
    mock_db = MagicMock()
    mock_db.employees.create_index.side_effect = RuntimeError("Index build simulated failure")
    with pytest.raises(RuntimeError) as exc:
        create_required_indexes(mock_db)
    assert "Index build simulated failure" in str(exc.value)


# --------------------------------------------------------------------------- #
# 6. Robust Query Plan & ExecutionStats Traversal Helper
# --------------------------------------------------------------------------- #
def extract_plan_info(explain_doc: dict) -> dict:
    """
    Traverses explain output across find queries and aggregation pipelines,
    identifying the winning plan, extracting its stage hierarchy, isolating
    rejected candidate plans, and recording executionStats, cursor-level metrics,
    and lookup stage statistics.
    """
    winning_plan = {}
    rejected_plans = []
    exec_stats = {}
    cursor_stats = {}
    lookup_metrics = []
    lookup_indexes = []
    pipeline_stages = []
    pipeline_returned = None

    # Case A: Top-level queryPlanner (find queries or SBE aggregations)
    if "queryPlanner" in explain_doc:
        qp = explain_doc["queryPlanner"]
        wp = qp.get("winningPlan", {})
        winning_plan = wp.get("queryPlan", wp)
        rejected_plans = qp.get("rejectedPlans", [])

    # Case B: Top-level executionStats
    if "executionStats" in explain_doc:
        es = explain_doc["executionStats"]
        exec_stats = {
            "nReturned": es.get("nReturned", 0),
            "totalKeysExamined": es.get("totalKeysExamined", 0),
            "totalDocsExamined": es.get("totalDocsExamined", 0),
            "executionTimeMillis": es.get("executionTimeMillis", 0),
        }
        pipeline_returned = exec_stats["nReturned"]

    # Case C: Aggregation pipeline stages
    if "stages" in explain_doc:
        for stg in explain_doc["stages"]:
            stage_keys = [k for k in stg.keys() if k.startswith("$")]
            stage_name = stage_keys[0] if stage_keys else list(stg.keys())[0]
            pipeline_stages.append(stage_name)
            if "nReturned" in stg:
                pipeline_returned = stg["nReturned"]

            if "$cursor" in stg:
                c = stg["$cursor"]
                if not winning_plan and "queryPlanner" in c:
                    qp = c["queryPlanner"]
                    wp = qp.get("winningPlan", {})
                    winning_plan = wp.get("queryPlan", wp)
                    rejected_plans = qp.get("rejectedPlans", [])
                c_es = c.get("executionStats", {})
                cursor_stats = {
                    "nReturned": c_es.get("nReturned", stg.get("nReturned", 0)),
                    "totalKeysExamined": c_es.get("totalKeysExamined", 0),
                    "totalDocsExamined": c_es.get("totalDocsExamined", 0),
                    "executionTimeMillis": c_es.get("executionTimeMillis", stg.get("executionTimeMillisEstimate", 0)),
                }
                if not exec_stats:
                    exec_stats = dict(cursor_stats)

            if "$lookup" in stg:
                lookup_payload = stg.get("$lookup", {})
                idx_used = stg.get("indexesUsed") or lookup_payload.get("indexesUsed") or []
                lookup_info = {
                    "indexesUsed": idx_used,
                    "totalDocsExamined": stg.get("totalDocsExamined", lookup_payload.get("totalDocsExamined", 0)),
                    "totalKeysExamined": stg.get("totalKeysExamined", lookup_payload.get("totalKeysExamined", 0)),
                    "collectionScans": stg.get("collectionScans", lookup_payload.get("collectionScans", 0)),
                }
                lookup_metrics.append(lookup_info)
                lookup_indexes.extend(lookup_info["indexesUsed"])

    def get_stages(node):
        stages = []
        if isinstance(node, dict):
            if "stage" in node and node["stage"]:
                stages.append(str(node["stage"]).upper())
            for k in ("inputStage", "outerStage", "innerStage"):
                if k in node:
                    stages.extend(get_stages(node[k]))
            if "inputStages" in node:
                for s in node["inputStages"]:
                    stages.extend(get_stages(s))
        return stages

    def get_indexes(node):
        idx = []
        if isinstance(node, dict):
            if "indexName" in node and node["indexName"]:
                idx.append(node["indexName"])
            for k in ("inputStage", "outerStage", "innerStage"):
                if k in node:
                    idx.extend(get_indexes(node[k]))
            if "inputStages" in node:
                for s in node["inputStages"]:
                    idx.extend(get_indexes(s))
        return idx

    plan_stages = get_stages(winning_plan)
    indexes_used = sorted(list(set(get_indexes(winning_plan) + lookup_indexes)))

    return {
        "winning_plan": winning_plan,
        "rejected_plans": rejected_plans,
        "plan_stages": plan_stages,
        "pipeline_stages": pipeline_stages,
        "indexes_used": indexes_used,
        "exec_stats": exec_stats,
        "cursor_stats": cursor_stats,
        "lookup_metrics": lookup_metrics,
        "pipeline_returned": pipeline_returned,
    }


def test_extract_plan_info_find_structure():
    mock_explain = {
        "queryPlanner": {
            "winningPlan": {
                "stage": "FETCH",
                "inputStage": {
                    "stage": "IXSCAN",
                    "indexName": "emp_code_1_date_1",
                },
            },
            "rejectedPlans": [
                {"stage": "COLLSCAN"},
            ],
        },
        "executionStats": {
            "nReturned": 5,
            "totalKeysExamined": 5,
            "totalDocsExamined": 5,
            "executionTimeMillis": 1,
        },
    }
    info = extract_plan_info(mock_explain)
    assert info["plan_stages"] == ["FETCH", "IXSCAN"]
    assert info["indexes_used"] == ["emp_code_1_date_1"]
    assert info["exec_stats"]["nReturned"] == 5
    assert len(info["rejected_plans"]) == 1
    # Ensure rejected plan's COLLSCAN is isolated from winning plan stages
    assert "COLLSCAN" not in info["plan_stages"]


def test_extract_plan_info_sbe_nested_structure():
    mock_sbe_explain = {
        "queryPlanner": {
            "winningPlan": {
                "queryPlan": {
                    "stage": "GROUP",
                    "inputStage": {
                        "stage": "FETCH",
                        "inputStage": {
                            "stage": "IXSCAN",
                            "indexName": "date_1_late_minutes_1",
                        },
                    },
                },
            },
        },
        "executionStats": {
            "nReturned": 1,
            "totalKeysExamined": 10,
            "totalDocsExamined": 10,
            "executionTimeMillis": 3,
        },
    }
    info = extract_plan_info(mock_sbe_explain)
    assert info["plan_stages"] == ["GROUP", "FETCH", "IXSCAN"]
    assert info["indexes_used"] == ["date_1_late_minutes_1"]


def test_extract_plan_info_aggregation_stages_and_lookup():
    mock_agg_explain = {
        "stages": [
            {
                "$cursor": {
                    "queryPlanner": {
                        "winningPlan": {
                            "stage": "PROJECTION_SIMPLE",
                            "inputStage": {
                                "stage": "FETCH",
                                "inputStage": {
                                    "stage": "IXSCAN",
                                    "indexName": "department_1_joined_on_1",
                                },
                            },
                        },
                    },
                    "executionStats": {
                        "nReturned": 3,
                        "totalKeysExamined": 3,
                        "totalDocsExamined": 3,
                        "executionTimeMillis": 4,
                    },
                },
            },
            {
                "$lookup": {
                    "indexesUsed": ["emp_code_1_date_1"],
                },
            },
        ],
    }
    info = extract_plan_info(mock_agg_explain)
    assert info["plan_stages"] == ["PROJECTION_SIMPLE", "FETCH", "IXSCAN"]
    assert "department_1_joined_on_1" in info["indexes_used"]
    assert "emp_code_1_date_1" in info["indexes_used"]
    assert info["exec_stats"]["nReturned"] == 3
    assert info["cursor_stats"]["nReturned"] == 3
    assert info["cursor_stats"]["totalDocsExamined"] == 3
    assert len(info["lookup_metrics"]) == 1
    assert info["lookup_metrics"][0]["indexesUsed"] == ["emp_code_1_date_1"]


# --------------------------------------------------------------------------- #
# 7. Index Scanning (IXSCAN) & ExecutionStats Verification on Real Database
# --------------------------------------------------------------------------- #
def test_explain_uses_ixscan_with_plan_traversal(client_app, test_db):
    # Populate a representative test dataset (20 employees across 2 departments, 14 days of logs)
    employees = [
        {
            "emp_code": f"EMP{i:04d}",
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": "Engineering" if i % 2 == 0 else "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        for i in range(1, 21)
    ]
    test_db.employees.insert_many(employees)

    attendance = []
    for i in range(1, 21):
        for day in range(1, 15):
            date_str = f"2026-03-{day:02d}"
            attendance.append({
                "emp_code": f"EMP{i:04d}",
                "date": date_str,
                "status": "PRESENT",
                "late_minutes": 10 if day % 3 == 0 else 0,
                "overtime_minutes": 0,
                "half_day": False,
                "work_hours": 9.0,
                "punch_in": None,
                "punch_out": None,
            })
    test_db.attendance_logs.insert_many(attendance)

    # 1. attendance_list: query on emp_code and date range must use IXSCAN on emp_code_1_date_1
    res1 = client_app.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-03-01&date_to=2026-03-10")
    assert res1.status_code == 200
    info1 = extract_plan_info(res1.json()["explain"])
    assert any("IXSCAN" in s or "IXSEEK" in s for s in info1["plan_stages"])
    assert "COLLSCAN" not in info1["plan_stages"]
    assert "emp_code_1_date_1" in info1["indexes_used"]
    assert info1["exec_stats"]["totalDocsExamined"] <= 10
    assert info1["exec_stats"]["nReturned"] == 10

    # 2. employee_monthly: aggregation match on emp_code and date range must use IXSCAN on emp_code_1_date_1
    res2 = client_app.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-03")
    assert res2.status_code == 200
    info2 = extract_plan_info(res2.json()["explain"])
    assert any("IXSCAN" in s or "IXSEEK" in s for s in info2["plan_stages"])
    assert "COLLSCAN" not in info2["plan_stages"]
    assert "emp_code_1_date_1" in info2["indexes_used"]
    assert info2["exec_stats"]["totalDocsExamined"] <= 14

    # 3. department_summary: match on department and joined_on must use IXSCAN on department_1_joined_on_1
    res3 = client_app.get("/admin/explain/department_summary?month=2026-03&department=Engineering")
    assert res3.status_code == 200
    info3 = extract_plan_info(res3.json()["explain"])
    assert any("IXSCAN" in s or "IXSEEK" in s for s in info3["plan_stages"])
    assert "COLLSCAN" not in info3["plan_stages"]
    assert any("department" in idx for idx in info3["indexes_used"])
    assert info3["exec_stats"]["totalDocsExamined"] <= 10
    # Lookup stage scan verification: zero collection scans during joins
    assert all(lm.get("collectionScans", 0) == 0 for lm in info3["lookup_metrics"])

    # 4. late_leaderboard: match on date and late_minutes must use IXSCAN on date_1_late_minutes_1
    res4 = client_app.get("/admin/explain/late_leaderboard?month=2026-03&department=Engineering")
    assert res4.status_code == 200
    info4 = extract_plan_info(res4.json()["explain"])
    assert any("IXSCAN" in s or "IXSEEK" in s for s in info4["plan_stages"])
    assert "COLLSCAN" not in info4["plan_stages"]
    assert "date_1_late_minutes_1" in info4["indexes_used"]
    assert all(lm.get("collectionScans", 0) == 0 for lm in info4["lookup_metrics"])

    # 5. department_trend: initial match on department must use indexed scan on department
    res5 = client_app.get("/admin/explain/department_trend?department=Engineering&from=2026-03-01&to=2026-03-07")
    assert res5.status_code == 200
    info5 = extract_plan_info(res5.json()["explain"])
    assert any("IXSCAN" in s or "IXSEEK" in s for s in info5["plan_stages"])
    assert "COLLSCAN" not in info5["plan_stages"]
    assert any("department" in idx for idx in info5["indexes_used"])
    assert all(lm.get("collectionScans", 0) == 0 for lm in info5["lookup_metrics"])


def test_explain_database_error_handling(client_app, monkeypatch):
    from app.main import db
    simulated_error_msg = "Simulated database timeout or connection string leak mongodb://secret:pass@cluster"
    def mock_command_fail(*args, **kwargs):
        raise RuntimeError(simulated_error_msg)

    monkeypatch.setattr(db, "command", mock_command_fail)
    res = client_app.get("/admin/explain/attendance_list")
    assert res.status_code == 500
    assert res.json()["detail"] == "Database explain command failed"
    # Ensure internal exception details and sensitive strings are NOT leaked in response
    assert simulated_error_msg not in res.text
    assert "secret:pass" not in res.text
