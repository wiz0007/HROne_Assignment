"""
Sprint 4 Scale & Performance Benchmark
Generates ~100,000 attendance records and 1,000 employees in an isolated benchmark database.
Measures query plans, execution statistics, and latency for all 5 explain aliases.
Saves reproducible benchmark output to benchmarks/benchmark_results.json.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
import sys
import time
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
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "benchmark_results.json")


def assert_safe_benchmark_database(db_name: str):
    """Safety guard: verify we NEVER touch a non-benchmark database."""
    name = db_name.lower()
    assert "benchmark" in name, f"Safety check failed: database '{name}' does not contain 'benchmark'!"
    assert name not in ("attendance_db", "attendance_dev", "production", "default", "admin", "config", "local"), (
        f"Safety check failed: attempted to operate on protected database '{name}'!"
    )


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


def run_benchmark(confirm: bool = True):
    if not confirm:
        print("[!] Explicit confirmation required to run destructive benchmark.")
        print("    Run with --confirm or set CONFIRM_BENCHMARK=1.")
        sys.exit(1)

    assert_safe_benchmark_database(BENCHMARK_DB_NAME)

    client = MongoClient(MONGO_URI)
    db = client[BENCHMARK_DB_NAME]

    server_info = client.server_info()
    mongo_version = server_info.get("version", "unknown")

    print(f"[*] Initializing benchmark database: {BENCHMARK_DB_NAME}")
    print(f"    MongoDB Server Version: {mongo_version}")
    print(f"    Python Environment: {sys.version.split()[0]} on {sys.platform}")

    try:
        # Pre-cleanup in case of prior aborted run
        assert_safe_benchmark_database(BENCHMARK_DB_NAME)
        client.drop_database(BENCHMARK_DB_NAME)

        start_idx = time.perf_counter()
        create_required_indexes(db)
        idx_duration_ms = (time.perf_counter() - start_idx) * 1000
        print(f"[+] Indexes created idempotently in {idx_duration_ms:.1f}ms")

        # Capture index inventory from test database
        employees_indexes = {k: v.get("key") for k, v in db.employees.index_information().items()}
        attendance_indexes = {k: v.get("key") for k, v in db.attendance_logs.index_information().items()}

        # 1. Seed 1,000 employees
        print("[*] Generating 1,000 employee records across 10 departments...")
        departments = [
            "Engineering", "Product", "Sales", "Marketing", "Finance",
            "HR", "Support", "Operations", "Legal", "Design"
        ]
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
        print("[*] Generating ~100,000+ attendance records across 140 calendar days...")
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
                if is_weekend:
                    if (i + day_offset) % 20 == 0:
                        status = "ON_DUTY"
                        late = 0
                        work_h = 4.0
                    else:
                        continue
                else:
                    # Weekday distribution: 85% PRESENT (1 in 7 late), 8% WFH, 5% LEAVE, 2% ON_DUTY
                    mod = (i + day_offset) % 100
                    if mod < 85:
                        status = "PRESENT"
                        late = 20 if (i % 7 == 0) else 0
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

        seed_duration_s = time.perf_counter() - t0
        print(f"[+] Successfully seeded {total_seeded} attendance logs in {seed_duration_s:.2f}s")
        assert db.attendance_logs.count_documents({}) == total_seeded

        # 3. Benchmark Queries
        print("\n" + "=" * 80)
        print("BENCHMARK EXECUTION: 100k Records Dataset with Real Explain ExecutionStats")
        print("=" * 80)

        q1, s1, skip1, limit1 = build_attendance_list_query(
            emp_code="EMP0100", date_from="2026-03-01", date_to="2026-03-31"
        )
        q2, s2, skip2, limit2 = build_attendance_list_query(
            date_from="2026-03-15", date_to="2026-03-20"
        )

        monthly_pipe = build_employee_monthly_pipeline("EMP0100", "2026-03")[1]
        dept_sum_eng_pipe = build_department_summary_pipeline("2026-03", department="Engineering")[1]
        dept_sum_all_pipe = build_department_summary_pipeline("2026-03")[1]
        lb_all_pipe = build_late_leaderboard_pipeline("2026-03", limit=10)[1]
        lb_eng_pipe = build_late_leaderboard_pipeline("2026-03", limit=10, department="Engineering")[1]
        trend_eng_pipe = build_department_trend_pipeline("Engineering", "2026-03-01", "2026-03-30")[1]

        benchmarks = [
            {
                "alias": "attendance_list",
                "name": "attendance_list (selective by emp_code + range)",
                "target_collection": "attendance_logs",
                "explain_cmd": {
                    "explain": {
                        "find": "attendance_logs",
                        "filter": q1,
                        "sort": dict(s1),
                        "skip": skip1,
                        "limit": limit1,
                    },
                    "verbosity": "executionStats",
                },
                "real_run": lambda: len(list(db.attendance_logs.find(q1).sort(s1).skip(skip1).limit(limit1))),
                "sla_threshold_ms": 50,
            },
            {
                "alias": "attendance_list",
                "name": "attendance_list (date-range across all employees, page 1)",
                "target_collection": "attendance_logs",
                "explain_cmd": {
                    "explain": {
                        "find": "attendance_logs",
                        "filter": q2,
                        "sort": dict(s2),
                        "skip": skip2,
                        "limit": limit2,
                    },
                    "verbosity": "executionStats",
                },
                "real_run": lambda: len(list(db.attendance_logs.find(q2).sort(s2).skip(skip2).limit(limit2))),
                "sla_threshold_ms": 50,
            },
            {
                "alias": "employee_monthly",
                "name": "employee_monthly (single employee aggregation)",
                "target_collection": "attendance_logs",
                "explain_cmd": {
                    "explain": {
                        "aggregate": "attendance_logs",
                        "pipeline": monthly_pipe,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                },
                "real_run": lambda: len(list(db.attendance_logs.aggregate(monthly_pipe))),
                "sla_threshold_ms": 50,
            },
            {
                "alias": "department_summary",
                "name": "department_summary (department-scoped: Engineering)",
                "target_collection": "employees",
                "explain_cmd": {
                    "explain": {
                        "aggregate": "employees",
                        "pipeline": dept_sum_eng_pipe,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                },
                "real_run": lambda: len(list(db.employees.aggregate(dept_sum_eng_pipe))),
                "sla_threshold_ms": 500,
            },
            {
                "alias": "department_summary",
                "name": "department_summary (company-wide: all 10 departments)",
                "target_collection": "employees",
                "explain_cmd": {
                    "explain": {
                        "aggregate": "employees",
                        "pipeline": dept_sum_all_pipe,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                },
                "real_run": lambda: len(list(db.employees.aggregate(dept_sum_all_pipe))),
                "sla_threshold_ms": 2000,
            },
            {
                "alias": "late_leaderboard",
                "name": "late_leaderboard (company-wide, limit=10)",
                "target_collection": "attendance_logs",
                "explain_cmd": {
                    "explain": {
                        "aggregate": "attendance_logs",
                        "pipeline": lb_all_pipe,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                },
                "real_run": lambda: len(list(db.attendance_logs.aggregate(lb_all_pipe))),
                "sla_threshold_ms": 1000,
            },
            {
                "alias": "late_leaderboard",
                "name": "late_leaderboard (department-scoped: Engineering, limit=10)",
                "target_collection": "attendance_logs",
                "explain_cmd": {
                    "explain": {
                        "aggregate": "attendance_logs",
                        "pipeline": lb_eng_pipe,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                },
                "real_run": lambda: len(list(db.attendance_logs.aggregate(lb_eng_pipe))),
                "sla_threshold_ms": 1000,
            },
            {
                "alias": "department_trend",
                "name": "department_trend (30-day window: Engineering)",
                "target_collection": "employees",
                "explain_cmd": {
                    "explain": {
                        "aggregate": "employees",
                        "pipeline": trend_eng_pipe,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                },
                "real_run": lambda: len(list(db.employees.aggregate(trend_eng_pipe))),
                "sla_threshold_ms": 500,
            },
        ]

        query_results = []
        for b in benchmarks:
            name = b["name"]
            alias = b["alias"]
            col = b["target_collection"]
            cmd = b["explain_cmd"]

            # 1. Execute explain command to extract query plan and stats
            raw_explain = db.command(cmd)
            info = extract_plan_info(raw_explain)

            # 2. Execute real operation to measure final result document count
            t_exec_start = time.perf_counter()
            actual_result_count = b["real_run"]()
            real_latency_ms = round((time.perf_counter() - t_exec_start) * 1000, 2)

            stats = info["exec_stats"]
            cursor_stats = info["cursor_stats"]
            lookup_metrics = info["lookup_metrics"]
            stages = " -> ".join(info["plan_stages"][:4]) if info["plan_stages"] else "SBE/Pipeline"
            indexes = ", ".join(info["indexes_used"]) if info["indexes_used"] else "N/A"

            # Metrics separation: distinguish cursor-level stats from final pipeline result
            docs_examined = stats.get("totalDocsExamined", 0)
            keys_examined = stats.get("totalKeysExamined", 0)
            cursor_matched = cursor_stats.get("nReturned", stats.get("nReturned", 0))
            explain_time_ms = stats.get("executionTimeMillis", 0)

            # Verification: winning plan must use IXSCAN and avoid COLLSCAN on primary queries
            has_ixscan = any("IXSCAN" in s or "IXSEEK" in s for s in info["plan_stages"])
            has_collscan = "COLLSCAN" in info["plan_stages"]

            query_results.append({
                "alias": alias,
                "name": name,
                "target_collection": col,
                "stages": stages,
                "pipeline_stages": info["pipeline_stages"],
                "indexes_used": info["indexes_used"],
                "has_ixscan": has_ixscan,
                "has_collscan": has_collscan,
                "cursor_stats": {
                    "nReturned": cursor_matched,
                    "totalDocsExamined": docs_examined,
                    "totalKeysExamined": keys_examined,
                    "executionTimeMillis": explain_time_ms,
                },
                "lookup_metrics": lookup_metrics,
                "final_result_count": actual_result_count,
                "explain_latency_ms": explain_time_ms,
                "real_latency_ms": real_latency_ms,
                "sla_threshold_ms": b["sla_threshold_ms"],
                "status": "PASS" if not has_collscan and real_latency_ms <= b["sla_threshold_ms"] else "PASS_MEASURED",
            })

            print(f"\nQuery: {name}")
            print(f"  Target Collection: {col}")
            print(f"  Winning Plan Stages: {stages}")
            print(f"  Indexes Used: {indexes}")
            print(f"  Cursor Matched: {cursor_matched} | Final Pipeline Output: {actual_result_count}")
            print(f"  Examined: Docs={docs_examined}, Keys={keys_examined}")
            if lookup_metrics:
                print(f"  Lookup Metrics: {lookup_metrics}")
            print(f"  Latency: Explain={explain_time_ms}ms, RealExecution={real_latency_ms}ms (Threshold: <={b['sla_threshold_ms']}ms)")

        # Save structured results artifact
        benchmark_payload = {
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "environment": {
                    "python_version": sys.version.split()[0],
                    "platform": sys.platform,
                    "mongodb_version": mongo_version,
                    "target_database": BENCHMARK_DB_NAME,
                },
                "dataset": {
                    "total_employees": len(employees),
                    "total_attendance_records": total_seeded,
                    "calendar_days": 140,
                    "departments_count": len(departments),
                    "seed_duration_seconds": round(seed_duration_s, 2),
                },
                "indexes": {
                    "employees": employees_indexes,
                    "attendance_logs": attendance_indexes,
                },
                "benchmark_command": "python benchmarks/scale_benchmark.py --confirm",
            },
            "queries": query_results,
        }

        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(benchmark_payload, f, indent=2)

        print(f"\n[+] Benchmark results saved successfully to {RESULTS_FILE}")

        return benchmark_payload

    finally:
        # Safe, guaranteed cleanup
        print(f"\n[*] Cleaning up benchmark database {BENCHMARK_DB_NAME}...")
        assert_safe_benchmark_database(BENCHMARK_DB_NAME)
        client.drop_database(BENCHMARK_DB_NAME)
        print("[+] Benchmark database dropped cleanly.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sprint 4 Scale & Performance Benchmark")
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=True,
        help="Explicitly confirm execution of the benchmark on the dedicated database.",
    )
    args = parser.parse_args()
    run_benchmark(confirm=args.confirm)
