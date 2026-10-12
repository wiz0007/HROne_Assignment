"""
Sprint 4 Scale & Performance Benchmark
Generates ~100,000 attendance records and 1,000 employees in an isolated test database.
Measures query plans, execution statistics, and latency for all 5 explain aliases.
"""
import os
import sys
import time
from datetime import date, timedelta
from pymongo import MongoClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import (
    create_required_indexes,
    build_attendance_list_query,
    build_employee_monthly_pipeline,
    build_department_summary_pipeline,
    build_late_leaderboard_pipeline,
    build_department_trend_pipeline,
)

BENCHMARK_DB_NAME = "attendance_benchmark_100k"
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")


def extract_plan_info(explain_doc):
    winning_plan = {}
    rejected_plans = []
    exec_stats = {}
    lookup_indexes = []

    if "queryPlanner" in explain_doc:
        qp = explain_doc["queryPlanner"]
        wp = qp.get("winningPlan", {})
        winning_plan = wp.get("queryPlan", wp)
        rejected_plans = qp.get("rejectedPlans", [])

    if "executionStats" in explain_doc:
        es = explain_doc["executionStats"]
        exec_stats = {
            "nReturned": es.get("nReturned", 0),
            "totalKeysExamined": es.get("totalKeysExamined", 0),
            "totalDocsExamined": es.get("totalDocsExamined", 0),
            "executionTimeMillis": es.get("executionTimeMillis", 0),
        }

    if "stages" in explain_doc:
        for stg in explain_doc["stages"]:
            if "$cursor" in stg:
                c = stg["$cursor"]
                if not winning_plan and "queryPlanner" in c:
                    qp = c["queryPlanner"]
                    wp = qp.get("winningPlan", {})
                    winning_plan = wp.get("queryPlan", wp)
                    rejected_plans = qp.get("rejectedPlans", [])
                if not exec_stats and "executionStats" in c:
                    es = c["executionStats"]
                    exec_stats = {
                        "nReturned": es.get("nReturned", 0),
                        "totalKeysExamined": es.get("totalKeysExamined", 0),
                        "totalDocsExamined": es.get("totalDocsExamined", 0),
                        "executionTimeMillis": es.get("executionTimeMillis", 0),
                    }
            if "$lookup" in stg and "indexesUsed" in stg["$lookup"]:
                lookup_indexes.extend(stg["$lookup"]["indexesUsed"])

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
    indexes_used = list(set(get_indexes(winning_plan) + lookup_indexes))

    return {
        "winning_plan": winning_plan,
        "rejected_plans": rejected_plans,
        "plan_stages": plan_stages,
        "indexes_used": indexes_used,
        "exec_stats": exec_stats,
    }


def run_benchmark():
    client = MongoClient(MONGO_URI)
    db = client[BENCHMARK_DB_NAME]
    
    print(f"[*] Initializing benchmark database: {BENCHMARK_DB_NAME}")
    client.drop_database(BENCHMARK_DB_NAME)
    
    start_idx = time.perf_counter()
    create_required_indexes(db)
    print(f"[+] Indexes created in {(time.perf_counter() - start_idx)*1000:.1f}ms")

    # 1. Seed 1,000 employees
    print("[*] Generating 1,000 employee records...")
    departments = ["Engineering", "Product", "Sales", "Marketing", "Finance", "HR", "Support", "Operations", "Legal", "Design"]
    employees = []
    base_join = date(2025, 1, 1)
    for i in range(1, 1001):
        dept = departments[i % len(departments)]
        join_date = base_join + timedelta(days=(i % 365))
        employees.append({
            "emp_code": f"EMP{i:04d}",
            "name": f"Employee {i}",
            "email": f"emp{i}@hronetest.internal",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": join_date.isoformat(),
        })
    db.employees.insert_many(employees)
    print(f"[+] Inserted {len(employees)} employees.")

    # 2. Seed >100,000 attendance records across 140 days
    print("[*] Generating ~100,000+ attendance records across 140 days...")
    start_date = date(2026, 1, 1)
    batch = []
    total_seeded = 0
    t0 = time.perf_counter()

    for day_offset in range(140):
        cur_date = start_date + timedelta(days=day_offset)
        date_str = cur_date.isoformat()
        is_weekend = cur_date.weekday() >= 5

        for i in range(1, 1001):
            emp_code = f"EMP{i:04d}"
            # Distribution:
            # Weekends: mostly no logs, occasional overtime/on-duty
            if is_weekend:
                if (i + day_offset) % 20 == 0:
                    status = "ON_DUTY"
                    late = 0
                    work_h = 4.0
                else:
                    continue
            else:
                # Weekdays: 85% PRESENT, 8% WFH, 5% LEAVE, 2% ON_DUTY
                mod = (i + day_offset) % 100
                if mod < 85:
                    status = "PRESENT"
                    late = 20 if (i % 7 == 0) else 0  # 1 in 7 employees late
                    work_h = 9.0
                elif mod < 93:
                    status = "WFH"
                    late = 0
                    work_h = 8.5
                elif mod < 98:
                    status = "LEAVE"
                    late = 0
                    work_h = None
                else:
                    status = "ON_DUTY"
                    late = 0
                    work_h = 7.0

            batch.append({
                "emp_code": emp_code,
                "date": date_str,
                "status": status,
                "late_minutes": late,
                "overtime_minutes": 0,
                "half_day": False,
                "work_hours": work_h,
                "punch_in": None,
                "punch_out": None,
                "revision": 0,
                "history": [],
            })

            if len(batch) >= 10000:
                db.attendance_logs.insert_many(batch)
                total_seeded += len(batch)
                batch = []

    if batch:
        db.attendance_logs.insert_many(batch)
        total_seeded += len(batch)

    print(f"[+] Successfully seeded {total_seeded} attendance logs in {(time.perf_counter() - t0):.2f}s")
    assert db.attendance_logs.count_documents({}) == total_seeded

    # 3. Benchmark Queries
    print("\n" + "="*80)
    print("BENCHMARK EXECUTION: 100k Records Dataset with Real Explain ExecutionStats")
    print("="*80)

    q1, s1, skip1, limit1 = build_attendance_list_query(emp_code="EMP0100", date_from="2026-03-01", date_to="2026-03-31")
    q2, s2, skip2, limit2 = build_attendance_list_query(date_from="2026-03-15", date_to="2026-03-20")

    benchmarks = [
        (
            "attendance_list (selective by emp_code + range)",
            {"explain": {"find": "attendance_logs", "filter": q1, "sort": dict(s1), "skip": skip1, "limit": limit1}, "verbosity": "executionStats"}
        ),
        (
            "attendance_list (date-range across all employees, page 1)",
            {"explain": {"find": "attendance_logs", "filter": q2, "sort": dict(s2), "skip": skip2, "limit": limit2}, "verbosity": "executionStats"}
        ),
        (
            "employee_monthly (single employee aggregation)",
            {"explain": {"aggregate": "attendance_logs", "pipeline": build_employee_monthly_pipeline("EMP0100", "2026-03")[1], "cursor": {}}, "verbosity": "executionStats"}
        ),
        (
            "department_summary (department-scoped: Engineering)",
            {"explain": {"aggregate": "employees", "pipeline": build_department_summary_pipeline("2026-03", department="Engineering")[1], "cursor": {}}, "verbosity": "executionStats"}
        ),
        (
            "department_summary (company-wide: all 10 departments)",
            {"explain": {"aggregate": "employees", "pipeline": build_department_summary_pipeline("2026-03")[1], "cursor": {}}, "verbosity": "executionStats"}
        ),
        (
            "late_leaderboard (company-wide, limit=10)",
            {"explain": {"aggregate": "attendance_logs", "pipeline": build_late_leaderboard_pipeline("2026-03", limit=10)[1], "cursor": {}}, "verbosity": "executionStats"}
        ),
        (
            "late_leaderboard (department-scoped: Engineering, limit=10)",
            {"explain": {"aggregate": "attendance_logs", "pipeline": build_late_leaderboard_pipeline("2026-03", limit=10, department="Engineering")[1], "cursor": {}}, "verbosity": "executionStats"}
        ),
        (
            "department_trend (30-day window: Engineering)",
            {"explain": {"aggregate": "employees", "pipeline": build_department_trend_pipeline("Engineering", "2026-03-01", "2026-03-30")[1], "cursor": {}}, "verbosity": "executionStats"}
        ),
    ]

    results = []
    for name, cmd in benchmarks:
        raw_explain = db.command(cmd)
        info = extract_plan_info(raw_explain)
        stats = info["exec_stats"]
        stages = " -> ".join(info["plan_stages"][:4]) if info["plan_stages"] else "SBE/Pipeline"
        indexes = ", ".join(info["indexes_used"]) if info["indexes_used"] else "N/A"
        
        results.append({
            "name": name,
            "stages": stages,
            "indexes": indexes,
            "nReturned": stats.get("nReturned", 0),
            "totalDocsExamined": stats.get("totalDocsExamined", 0),
            "totalKeysExamined": stats.get("totalKeysExamined", 0),
            "executionTimeMillis": stats.get("executionTimeMillis", 0),
        })

        print(f"\nQuery: {name}")
        print(f"  Stages: {stages}")
        print(f"  Indexes Used: {indexes}")
        print(f"  Execution Stats: nReturned={stats.get('nReturned')}, totalDocsExamined={stats.get('totalDocsExamined')}, totalKeysExamined={stats.get('totalKeysExamined')}, time={stats.get('executionTimeMillis')}ms")

    # Cleanup benchmark database
    print(f"\n[*] Cleaning up benchmark database {BENCHMARK_DB_NAME}...")
    client.drop_database(BENCHMARK_DB_NAME)
    print("[+] Benchmark database dropped cleanly.")

    return results

if __name__ == "__main__":
    run_benchmark()
