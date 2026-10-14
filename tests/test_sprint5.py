"""
Sprint 5 Comprehensive Verification & Acceptance Test Suite
Employee Attendance & Analytics API - Sprint 5 Final Engineering Hardening

Exhaustively verifies:
- S5-SAFE-01: Strict authorization before any client/database action; zero import-time side effects.
- S5-SAFE-02: Fail-closed database guard (exact allowlist) and server target validation; python -O safety.
- S5-BENCH-01: Strict explain-metric validation schema (observed 0 vs missing vs null vs malformed vs negative).
- S5-BENCH-02: Pure status aggregation function testing (FAIL > INCONCLUSIVE > PASS precedence).
- S5-BENCH-03: Scenario-specific index policies and result correctness invariants.
- S5-REL-01: Release packaging fixture testing with secret patterns, cache exclusions, and missing mandatory files.
"""
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from benchmarks.scale_benchmark import (
    AUTHORITATIVE_SCENARIOS,
    BENCHMARK_DB_NAME,
    aggregate_benchmark_status,
    check_benchmark_opt_in,
    evaluate_benchmark_scenario,
    sanitize_error,
    sanitize_mongo_uri,
    validate_index_list,
    validate_non_negative_int,
    validate_safe_benchmark_database,
    validate_safe_benchmark_target,
    verify_server_target,
)


# ==============================================================================
# S5-SAFE-01: Strict Authorization Before Benchmark Initialization
# ==============================================================================
def test_s5_safe_01_check_opt_in_all_variants(monkeypatch):
    """Verify check_benchmark_opt_in strictly accepts only authorized values and rejects all others."""
    # Authorized CLI flag
    assert check_benchmark_opt_in(confirm=True) is True

    # Authorized environment values
    for val in ("1", "true", "True", "TRUE", "yes", "Yes", "YES"):
        monkeypatch.setenv("CONFIRM_BENCHMARK", val)
        assert check_benchmark_opt_in(confirm=False) is True

    # Unauthorized / false / empty / malformed environment values
    for invalid in ("", "0", "false", "False", "no", "NO", "none", "maybe", "2", "-1", "undefined"):
        monkeypatch.setenv("CONFIRM_BENCHMARK", invalid)
        assert check_benchmark_opt_in(confirm=False) is False

    # Unset environment variable
    monkeypatch.delenv("CONFIRM_BENCHMARK", raising=False)
    assert check_benchmark_opt_in(confirm=False) is False


def test_s5_safe_01_subprocess_verifies_zero_client_creation_without_opt_in():
    """
    Subprocess test: Monitors MongoClient constructor calls from process start.
    Proves that executing scale_benchmark.py without opt-in performs ZERO MongoClient calls.
    """
    code = """
import sys
import pymongo

# Instrument MongoClient constructor before running benchmark entrypoint
real_init = pymongo.MongoClient.__init__
call_count = 0

def spy_init(self, *args, **kwargs):
    global call_count
    call_count += 1
    return real_init(self, *args, **kwargs)

pymongo.MongoClient.__init__ = spy_init

import benchmarks.scale_benchmark as bench

# 1. Importing the module must not construct MongoClient
if call_count > 0:
    print(f"FAILED: MongoClient instantiated during import! call_count={call_count}", file=sys.stderr)
    sys.exit(2)

# 2. Calling run_benchmark without confirmation must exit with code 1 and 0 calls
try:
    bench.run_benchmark(confirm=False)
except SystemExit as e:
    if e.code != 1:
        print(f"FAILED: Expected exit code 1, got {e.code}", file=sys.stderr)
        sys.exit(3)
    if call_count > 0:
        print(f"FAILED: MongoClient instantiated during run_benchmark without opt-in! count={call_count}", file=sys.stderr)
        sys.exit(4)
    print("SUCCESS: 0 MongoClient calls observed.")
    sys.exit(0)

print("FAILED: run_benchmark did not raise SystemExit", file=sys.stderr)
sys.exit(5)
"""
    env = os.environ.copy()
    env.pop("CONFIRM_BENCHMARK", None)

    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"Subprocess failed with stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert "SUCCESS: 0 MongoClient calls observed." in proc.stdout


def test_s5_safe_01_cli_without_confirm_exits_nonzero():
    """Verify scale_benchmark CLI invocation without --confirm exits nonzero before database work."""
    env = os.environ.copy()
    env.pop("CONFIRM_BENCHMARK", None)

    proc = subprocess.run(
        [sys.executable, "benchmarks/scale_benchmark.py"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode != 0
    assert "SAFETY ABORT" in proc.stdout or "SAFETY ABORT" in proc.stderr
    assert "--confirm" in proc.stdout or "--confirm" in proc.stderr


# ==============================================================================
# S5-SAFE-02: Fail-Closed Database and Server Target Validation
# ==============================================================================
def test_s5_safe_02_database_guard_exact_allowlist():
    """Verify validate_safe_benchmark_database strictly requires exact allowlist matches."""
    # Approved DB passes
    validate_safe_benchmark_database("attendance_benchmark_100k")
    validate_safe_benchmark_database(BENCHMARK_DB_NAME)

    # Substring names merely containing 'benchmark' must be rejected
    for unsafe in ("benchmark", "my_benchmark_hack", "prod_benchmark", "benchmark_100k", "company_benchmark"):
        with pytest.raises(ValueError, match="Safety check failed.*approved benchmark allowlist"):
            validate_safe_benchmark_database(unsafe)

    # Protected, default, empty, or wrong-type databases must be rejected
    for invalid in ("attendance_db", "attendance_dev", "production", "admin", "config", "", None, 123, []):
        with pytest.raises(ValueError, match="Safety check failed"):
            validate_safe_benchmark_database(invalid)  # type: ignore


def test_s5_safe_02_target_validation_approved_and_unapproved():
    """Verify validate_safe_benchmark_target rejects remote, production, or unapproved targets."""
    # Approved targets pass
    validate_safe_benchmark_target("mongodb://localhost:27017")
    validate_safe_benchmark_target("mongodb://127.0.0.1:27017")
    validate_safe_benchmark_target("mongodb://localhost:27017/attendance_benchmark_100k")

    # Production indicators must be rejected
    for prod_uri in (
        "mongodb+srv://user:pass@cluster0.mongodb.net",
        "mongodb://prod-cluster.internal:27017",
        "mongodb://production-server:27017",
        "mongodb://localhost:27017/production",
        "mongodb://localhost:27017/attendance_dev",
        "mongodb://localhost:27017/attendance_db",
    ):
        with pytest.raises(ValueError, match="Safety check failed"):
            validate_safe_benchmark_target(prod_uri)

    # Unapproved hosts must be rejected
    with pytest.raises(ValueError, match="not in approved benchmark hosts"):
        validate_safe_benchmark_target("mongodb://remote-host:27017")

    # Invalid URIs or schemes must be rejected
    for malformed in ("", None, "http://localhost:27017", "invalid_uri"):
        with pytest.raises(ValueError, match="Safety check failed"):
            validate_safe_benchmark_target(malformed)  # type: ignore


def test_s5_safe_02_guard_works_under_python_optimized_mode():
    """Verify that guards use explicit exceptions that persist under python -O."""
    code = """
from benchmarks.scale_benchmark import validate_safe_benchmark_database, validate_safe_benchmark_target

# Safe DB must pass
validate_safe_benchmark_database('attendance_benchmark_100k')
validate_safe_benchmark_target('mongodb://localhost:27017')

# Unsafe DB must raise ValueError
try:
    validate_safe_benchmark_database('prod_benchmark')
    print('FAILED: validate_safe_benchmark_database did not raise on unsafe name')
    exit(1)
except ValueError:
    pass

# Unsafe target must raise ValueError
try:
    validate_safe_benchmark_target('mongodb+srv://cluster.mongodb.net')
    print('FAILED: validate_safe_benchmark_target did not raise on production cluster')
    exit(2)
except ValueError:
    pass

print('OPTIMIZED_MODE_PASSED')
exit(0)
"""
    proc = subprocess.run([sys.executable, "-O", "-c", code], check=False, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "OPTIMIZED_MODE_PASSED" in proc.stdout


# ==============================================================================
# S5-BENCH-01: Explain-Metric Validation Schema
# ==============================================================================
def test_s5_bench_01_metric_parsers_distinguish_zero_missing_null_malformed_negative():
    """Verify metric validation distinguishes valid 0 from missing, null, wrong type, and negative values."""
    # 1. Non-negative int parser
    assert validate_non_negative_int({"docs": 0}, "docs") == (0, None)  # Observed zero
    assert validate_non_negative_int({"docs": 42}, "docs") == (42, None)  # Observed positive
    assert validate_non_negative_int({}, "docs") == (None, "MISSING")
    assert validate_non_negative_int({"docs": None}, "docs") == (None, "NULL")
    assert validate_non_negative_int({"docs": "42"}, "docs") == (None, "WRONG_TYPE")
    assert validate_non_negative_int({"docs": True}, "docs") == (None, "WRONG_TYPE")  # bool is not int
    assert validate_non_negative_int({"docs": 3.14}, "docs") == (None, "WRONG_TYPE")
    assert validate_non_negative_int({"docs": -5}, "docs") == (None, "NEGATIVE")

    # 2. Index list parser
    assert validate_index_list({"idx": ["date_1"]}, "idx") == (["date_1"], None)
    assert validate_index_list({"idx": []}, "idx") == ([], None)
    assert validate_index_list({}, "idx") == (None, "MISSING")
    assert validate_index_list({"idx": None}, "idx") == (None, "NULL")
    assert validate_index_list({"idx": "date_1"}, "idx") == (None, "WRONG_TYPE")
    assert validate_index_list({"idx": [123]}, "idx") == (None, "WRONG_TYPE")


def test_s5_bench_01_explain_fixtures_evaluated_correctly():
    """Verify evaluate_benchmark_scenario correctly evaluates valid, missing, malformed, and negative metrics."""
    policy_selective = {
        "name": "selective_test",
        "expect_primary_ixscan": True,
        "prohibit_primary_collscan": True,
        "expect_lookup": False,
        "prohibit_lookup_collscan": False,
        "sla_threshold_ms": 50,
        "expected_min_count": 1,
        "decision_reason": "Selective index required",
    }

    # Complete valid plan with observed zero executionTimeMillis (sub-millisecond)
    plan_valid = {
        "plan_stages": ["IXSCAN", "FETCH"],
        "exec_stats": {
            "totalDocsExamined": 10,
            "totalDocsExamined_status": None,
            "totalKeysExamined": 10,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 0,  # Observed 0
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [],
    }
    st, _reason, _ = evaluate_benchmark_scenario(policy_selective, plan_valid, 2.0, actual_result_count=1)
    assert st == "PASS"

    # Missing totalDocsExamined
    plan_missing = {
        "plan_stages": ["IXSCAN"],
        "exec_stats": {
            "totalDocsExamined": None,
            "totalDocsExamined_status": "MISSING",
            "totalKeysExamined": 10,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 1,
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [],
    }
    st, _reason, _ = evaluate_benchmark_scenario(policy_selective, plan_missing, 2.0, actual_result_count=1)
    assert st == "INCONCLUSIVE_MISSING_METRICS"

    # Malformed metric (negative docs examined)
    plan_negative = {
        "plan_stages": ["IXSCAN"],
        "exec_stats": {
            "totalDocsExamined": None,
            "totalDocsExamined_status": "NEGATIVE",
            "totalKeysExamined": 10,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 1,
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [],
    }
    st, _reason, _ = evaluate_benchmark_scenario(policy_selective, plan_negative, 2.0, actual_result_count=1)
    assert st == "INCONCLUSIVE_MALFORMED_METRICS"


def test_s5_bench_01_multiple_lookup_stages_with_different_outcomes():
    """Verify scenario with multiple lookup stages: one clean, one scanning fails with FAIL_COLLSCAN."""
    policy_lookup = {
        "name": "multi_lookup_test",
        "expect_primary_ixscan": True,
        "prohibit_primary_collscan": True,
        "expect_lookup": True,
        "prohibit_lookup_collscan": True,
        "sla_threshold_ms": 500,
        "expected_min_count": 1,
        "decision_reason": "All lookups must be indexed",
    }

    plan_multi_lookup = {
        "plan_stages": ["IXSCAN"],
        "exec_stats": {
            "totalDocsExamined": 10,
            "totalDocsExamined_status": None,
            "totalKeysExamined": 10,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 5,
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [
            {
                "indexesUsed": ["emp_code_1"],
                "indexesUsed_status": None,
                "collectionScans": 0,
                "collectionScans_status": None,
                "totalDocsExamined": 10,
                "totalKeysExamined": 10,
                "is_malformed": False,
                "is_missing": False,
            },
            {
                "indexesUsed": [],
                "indexesUsed_status": None,
                "collectionScans": 2,  # Second lookup stage scanned collection!
                "collectionScans_status": None,
                "totalDocsExamined": 200,
                "totalKeysExamined": 0,
                "is_malformed": False,
                "is_missing": False,
            },
        ],
    }
    st, reason, summary = evaluate_benchmark_scenario(policy_lookup, plan_multi_lookup, 25.0, actual_result_count=1)
    assert st == "FAIL_COLLSCAN"
    assert "Lookup stage executed 2 collection scan(s)" in reason
    assert summary["has_lookup_collscan"] is True
    assert summary["lookup_collscans"] == 2


# ==============================================================================
# S5-BENCH-02: Pure Status Aggregation Function Testing
# ==============================================================================
def test_s5_bench_02_aggregate_benchmark_status_all_pass():
    """Verify aggregate_benchmark_status returns PASS and exit code 0 when all 8 scenarios pass with valid evidence."""
    valid_cursor = {
        "nReturned": 10,
        "totalDocsExamined": 10,
        "totalKeysExamined": 10,
        "executionTimeMillis": 5,
    }
    results = [{"name": f"q{i}", "status": "PASS", "status_reason": "ok", "cursor_stats": valid_cursor} for i in range(8)]
    status, code, issues = aggregate_benchmark_status(results, expected_scenario_count=8)
    assert status == "PASS"
    assert code == 0
    assert len(issues) == 0


def test_s5_bench_02_aggregate_benchmark_status_precedence_fail_over_inconclusive():
    """Verify that FAIL takes precedence over INCONCLUSIVE (definite failure is never masked)."""
    # 1 FAIL, 1 INCONCLUSIVE, 6 PASS
    results = [
        {"name": "q0", "status": "FAIL_COLLSCAN", "status_reason": "primary scan"},
        {"name": "q1", "status": "INCONCLUSIVE_MISSING_LOOKUP_METRICS", "status_reason": "missing stats"},
    ] + [{"name": f"q{i}", "status": "PASS", "status_reason": "ok"} for i in range(2, 8)]

    status, code, issues = aggregate_benchmark_status(results, expected_scenario_count=8)
    assert status == "FAIL", "Definite failure must take precedence over inconclusive"
    assert code == 1
    assert any("FAILED" in iss for iss in issues)
    assert any("INCONCLUSIVE" in iss for iss in issues)


def test_s5_bench_02_aggregate_benchmark_status_inconclusive_when_no_failures():
    """Verify that when evidence is incomplete and there are no failures, status is INCONCLUSIVE."""
    results = [
        {"name": "q0", "status": "INCONCLUSIVE_MISSING_LOOKUP_METRICS", "status_reason": "missing stats"},
    ] + [{"name": f"q{i}", "status": "PASS", "status_reason": "ok"} for i in range(1, 8)]

    status, code, _issues = aggregate_benchmark_status(results, expected_scenario_count=8)
    assert status == "INCONCLUSIVE"
    assert code == 1


def test_s5_bench_02_aggregate_benchmark_status_edge_cases():
    """Verify aggregation handles empty results, scenario count mismatch, and duplicates."""
    # Empty
    st, code, issues = aggregate_benchmark_status([], expected_scenario_count=8)
    assert st == "FAIL"
    assert code == 1

    # Less than expected count (e.g. only 6 ran)
    results_incomplete = [{"name": f"q{i}", "status": "PASS"} for i in range(6)]
    st, code, issues = aggregate_benchmark_status(results_incomplete, expected_scenario_count=8)
    assert st == "FAIL"
    assert code == 1
    assert any("Expected 8 scenarios" in iss for iss in issues)

    # Duplicate scenario names
    results_dup = [{"name": "q0", "status": "PASS"}] + [{"name": "q0", "status": "PASS"}] + [
        {"name": f"q{i}", "status": "PASS"} for i in range(2, 8)
    ]
    st, code, issues = aggregate_benchmark_status(results_dup, expected_scenario_count=8)
    assert st == "FAIL"
    assert code == 1
    assert any("Duplicate scenario identifier" in iss for iss in issues)


# ==============================================================================
# S5-BENCH-03: Scenario-Specific Policies & Result Invariants
# ==============================================================================
def test_s5_bench_03_result_invariant_failure():
    """Verify scenario fails with FAIL_RESULT_INVARIANT if output document count violates invariant."""
    policy = {
        "name": "department_summary_all",
        "expect_primary_ixscan": False,
        "prohibit_primary_collscan": False,
        "expect_lookup": True,
        "prohibit_lookup_collscan": True,
        "sla_threshold_ms": 3000,
        "expected_min_count": 10,  # Expected 10 departments
        "decision_reason": "10 departments required",
    }
    plan = {
        "plan_stages": ["COLLSCAN"],
        "exec_stats": {
            "totalDocsExamined": 1000,
            "totalDocsExamined_status": None,
            "totalKeysExamined": 0,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 50,
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [
            {
                "indexesUsed": ["emp_code_1_date_1"],
                "indexesUsed_status": None,
                "collectionScans": 0,
                "collectionScans_status": None,
                "totalDocsExamined": 100,
                "totalKeysExamined": 100,
                "is_malformed": False,
                "is_missing": False,
            }
        ],
    }
    # If returned count is only 2 instead of 10:
    st, reason, _ = evaluate_benchmark_scenario(policy, plan, 50.0, actual_result_count=2)
    assert st == "FAIL_RESULT_INVARIANT"
    assert "below expected minimum 10" in reason


# ==============================================================================
# S5-REL-01: Release Package Integrity & Exclusion Validation
# ==============================================================================
def test_s5_rel_01_fixture_repo_packaging_and_exclusions(tmp_path):
    """
    Builds a temporary mock repository containing valid files, .env variants,
    secrets, caches, and test artifacts to verify build_release_zip enforcement.
    """
    from scripts.build_release import build_release_zip

    mock_repo = tmp_path / "mock_project"
    mock_repo.mkdir()

    # 1. Create valid mock required files
    mock_required = ["app/main.py", "README.md", "requirements.txt"]
    for req in mock_required:
        f = mock_repo / req
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("valid content", encoding="utf-8")

    # 2. Add forbidden files that MUST be excluded
    forbidden_files = [
        ".env",
        ".env.local",
        ".env.production",
        "secret.key",
        "private.pem",
        ".git/config",
        ".venv/lib/dummy.py",
        "__pycache__/dummy.pyc",
        ".pytest_cache/v/cache",
        ".ruff_cache/cache",
        ".vscode/settings.json",
        "database.dump",
        "test.sqlite3",
    ]
    for forb in forbidden_files:
        f = mock_repo / forb
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("SECRET OR CACHE CONTENT", encoding="utf-8")

    # 3. Build release from mock repo
    zip_out = str(tmp_path / "test_out.zip")
    built_path = build_release_zip(
        output_path=zip_out,
        source_dir=str(mock_repo),
        required_files=mock_required,
    )
    assert os.path.exists(built_path)

    # 4. Inspect ZIP entries
    with zipfile.ZipFile(built_path, "r") as zf:
        assert zf.testzip() is None, "ZIP integrity failed"
        names = zf.namelist()

        # All valid required files must be present
        for req in mock_required:
            assert req in names

        # Zero forbidden files must be present
        for forb in forbidden_files:
            norm_forb = forb.replace("\\", "/")
            assert norm_forb not in names
            assert not any(n.endswith(norm_forb) for n in names)


def test_s5_rel_01_missing_mandatory_file_fails_build(tmp_path):
    """Verify that build_release_zip raises ValueError when a mandatory required file is missing."""
    from scripts.build_release import build_release_zip

    mock_repo = tmp_path / "mock_incomplete"
    mock_repo.mkdir()

    # Create only README.md, but require app/main.py
    (mock_repo / "README.md").write_text("readme", encoding="utf-8")
    mock_required = ["README.md", "app/main.py"]

    with pytest.raises(ValueError, match="Mandatory release file.*missing"):
        build_release_zip(
            output_path=str(tmp_path / "fail.zip"),
            source_dir=str(mock_repo),
            required_files=mock_required,
        )


# ==============================================================================
# S5-REG-01: Explain-Plan Validation Must Never Accept Missing Evidence as PASS (Part 2.A)
# ==============================================================================
def test_s5_reg_01_ixscan_with_empty_or_missing_exec_stats_returns_inconclusive():
    """Verify winning plan containing IXSCAN but lacking required execution statistics returns INCONCLUSIVE."""
    policy = {
        "name": "selective_test",
        "expect_primary_ixscan": True,
        "prohibit_primary_collscan": True,
        "expect_lookup": False,
        "prohibit_lookup_collscan": False,
        "sla_threshold_ms": 50,
        "expected_min_count": 1,
        "decision_reason": "IXSCAN required",
    }

    # Case A: exec_stats is empty dict
    plan_empty_stats = {
        "plan_stages": ["IXSCAN", "FETCH"],
        "exec_stats": {},
        "lookup_metrics": [],
    }
    st, reason, _ = evaluate_benchmark_scenario(policy, plan_empty_stats, 2.0, actual_result_count=1)
    assert st == "INCONCLUSIVE_MISSING_METRICS"
    assert "missing or empty" in reason

    # Case B: exec_stats is None
    plan_none_stats = {
        "plan_stages": ["IXSCAN", "FETCH"],
        "exec_stats": None,
        "lookup_metrics": [],
    }
    st, reason, _ = evaluate_benchmark_scenario(policy, plan_none_stats, 2.0, actual_result_count=1)
    assert st == "INCONCLUSIVE_MISSING_METRICS"

    # Case C: Unsupported plan structure (no stages extracted)
    plan_unsupported = {
        "plan_stages": [],
        "pipeline_stages": [],
        "exec_stats": {},
        "lookup_metrics": [],
        "is_unsupported_structure": True,
    }
    st, reason, _ = evaluate_benchmark_scenario(policy, plan_unsupported, 2.0, actual_result_count=1)
    assert st == "INCONCLUSIVE_UNSUPPORTED_STRUCTURE"


def test_s5_reg_01_metric_validation_types_and_values():
    """Verify boolean, string, float, negative, and legitimate zero values."""
    policy = {
        "name": "selective_test",
        "expect_primary_ixscan": True,
        "prohibit_primary_collscan": True,
        "expect_lookup": False,
        "prohibit_lookup_collscan": False,
        "sla_threshold_ms": 50,
        "expected_min_count": 1,
        "decision_reason": "IXSCAN required",
    }

    # Boolean supplied where int required -> malformed
    plan_bool = {
        "plan_stages": ["IXSCAN"],
        "exec_stats": {
            "totalDocsExamined": True,  # Bool!
            "totalDocsExamined_status": "WRONG_TYPE",
            "totalKeysExamined": 10,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 5,
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [],
    }
    st, _, _ = evaluate_benchmark_scenario(policy, plan_bool, 2.0, actual_result_count=1)
    assert st == "INCONCLUSIVE_MALFORMED_METRICS"

    # String supplied -> malformed
    plan_str = {
        "plan_stages": ["IXSCAN"],
        "exec_stats": {
            "totalDocsExamined": "100",  # String!
            "totalDocsExamined_status": "WRONG_TYPE",
            "totalKeysExamined": 10,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 5,
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [],
    }
    st, _, _ = evaluate_benchmark_scenario(policy, plan_str, 2.0, actual_result_count=1)
    assert st == "INCONCLUSIVE_MALFORMED_METRICS"


# ==============================================================================
# S5-REG-02: Enforce Indexed-Lookup Requirements Independently of Doc Counts (Part 2.B)
# ==============================================================================
def test_s5_reg_02_indexed_lookup_zero_docs_with_empty_indexes_fails():
    """Verify scenario requiring indexed lookup returns FAIL_UNINDEXED_LOOKUP with indexesUsed: [] even if docs=0."""
    policy_lookup = {
        "name": "dept_summary_test",
        "expect_primary_ixscan": False,
        "prohibit_primary_collscan": False,
        "expect_lookup": True,
        "prohibit_lookup_collscan": True,
        "expect_indexed_lookup": True,
        "sla_threshold_ms": 500,
        "expected_min_count": 1,
        "decision_reason": "Lookup must be indexed",
    }

    plan_zero_docs_no_index = {
        "plan_stages": ["COLLSCAN"],
        "exec_stats": {
            "totalDocsExamined": 10,
            "totalDocsExamined_status": None,
            "totalKeysExamined": 0,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 5,
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [
            {
                "indexesUsed": [],  # Empty!
                "indexesUsed_status": None,
                "collectionScans": 0,
                "collectionScans_status": None,
                "totalDocsExamined": 0,  # Zero docs examined!
                "totalKeysExamined": 0,
                "is_malformed": False,
                "is_missing": False,
            }
        ],
    }
    st, reason, _ = evaluate_benchmark_scenario(policy_lookup, plan_zero_docs_no_index, 5.0, actual_result_count=1)
    assert st == "FAIL_UNINDEXED_LOOKUP"
    assert "failed indexed-lookup requirement" in reason


def test_s5_reg_02_multiple_lookups_passing_does_not_hide_failing_or_missing():
    """Verify multiple lookups: passing lookup cannot hide failing or unindexed lookup."""
    policy_lookup = {
        "name": "multi_lookup",
        "expect_primary_ixscan": True,
        "prohibit_primary_collscan": True,
        "expect_lookup": True,
        "prohibit_lookup_collscan": True,
        "expect_indexed_lookup": True,
        "sla_threshold_ms": 500,
        "expected_min_count": 1,
        "decision_reason": "All lookups must be indexed",
    }

    # Case A: One indexed, one unindexed -> FAIL_UNINDEXED_LOOKUP
    plan_mixed = {
        "plan_stages": ["IXSCAN"],
        "exec_stats": {
            "totalDocsExamined": 10,
            "totalDocsExamined_status": None,
            "totalKeysExamined": 10,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 5,
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [
            {
                "indexesUsed": ["emp_code_1"],
                "indexesUsed_status": None,
                "collectionScans": 0,
                "collectionScans_status": None,
                "totalDocsExamined": 10,
                "totalKeysExamined": 10,
                "is_malformed": False,
                "is_missing": False,
            },
            {
                "indexesUsed": [],  # Unindexed!
                "indexesUsed_status": None,
                "collectionScans": 0,
                "collectionScans_status": None,
                "totalDocsExamined": 0,
                "totalKeysExamined": 0,
                "is_malformed": False,
                "is_missing": False,
            },
        ],
    }
    st, _reason, _ = evaluate_benchmark_scenario(policy_lookup, plan_mixed, 5.0, actual_result_count=1)
    assert st == "FAIL_UNINDEXED_LOOKUP"

    # Case B: One indexed, one missing metric -> INCONCLUSIVE_MISSING_LOOKUP_METRICS
    plan_missing_stat = {
        "plan_stages": ["IXSCAN"],
        "exec_stats": {
            "totalDocsExamined": 10,
            "totalDocsExamined_status": None,
            "totalKeysExamined": 10,
            "totalKeysExamined_status": None,
            "executionTimeMillis": 5,
            "executionTimeMillis_status": None,
        },
        "lookup_metrics": [
            {
                "indexesUsed": ["emp_code_1"],
                "indexesUsed_status": None,
                "collectionScans": 0,
                "collectionScans_status": None,
                "totalDocsExamined": 10,
                "totalKeysExamined": 10,
                "is_malformed": False,
                "is_missing": False,
            },
            {
                "indexesUsed": ["date_1"],
                "indexesUsed_status": None,
                "collectionScans": None,
                "collectionScans_status": "MISSING",
                "totalDocsExamined": 10,
                "totalKeysExamined": 10,
                "is_malformed": False,
                "is_missing": True,  # Missing metric!
            },
        ],
    }
    st, _reason, _ = evaluate_benchmark_scenario(policy_lookup, plan_missing_stat, 5.0, actual_result_count=1)
    assert st == "INCONCLUSIVE_MISSING_LOOKUP_METRICS"


# ==============================================================================
# S5-REG-03: Validate Exact Benchmark Scenario Set (Part 2.C)
# ==============================================================================
def test_s5_reg_03_nine_scenarios_when_eight_expected_fails():
    """Verify aggregate_benchmark_status returns FAIL for 9 unique PASS results when 8 are expected."""
    results_9 = [{"name": f"q{i}", "status": "PASS", "status_reason": "ok"} for i in range(9)]
    st, code, issues = aggregate_benchmark_status(results_9, expected_scenario_count=8)
    assert st == "FAIL"
    assert code == 1
    assert any("unexpected extra scenarios" in iss for iss in issues)


def test_s5_reg_03_exact_scenario_set_missing_and_unexpected_rejected():
    """Verify exact authoritative scenario set matching: extra or missing scenario rejected."""
    # 7 valid authoritative + 1 missing
    names_missing = list(AUTHORITATIVE_SCENARIOS)[:7]
    results_missing = [{"name": n, "status": "PASS", "status_reason": "ok"} for n in names_missing]
    st, code, issues = aggregate_benchmark_status(results_missing, expected_scenarios=AUTHORITATIVE_SCENARIOS)
    assert st == "FAIL"
    assert code == 1
    assert any("Missing required scenario" in iss for iss in issues)

    # 8 valid authoritative + 1 extra unexpected scenario
    names_extra = list(AUTHORITATIVE_SCENARIOS) + ["unexpected_scenario"]
    results_extra = [{"name": n, "status": "PASS", "status_reason": "ok"} for n in names_extra]
    st, code, issues = aggregate_benchmark_status(results_extra, expected_scenarios=AUTHORITATIVE_SCENARIOS)
    assert st == "FAIL"
    assert code == 1
    assert any("unexpected extra" in iss.lower() for iss in issues)


# ==============================================================================
# S5-REG-04: Fail-Closed Server Target Verification (Part 2.D)
# ==============================================================================
def test_s5_reg_04_verify_server_target_fails_closed():
    """Verify verify_server_target raises ValueError on ping failure, list_database_names failure, or None client."""
    class MockAdmin:
        def command(self, cmd):
            raise ConnectionError("Connection reset by peer")

    class MockClientPingFail:
        admin = MockAdmin()
        def server_info(self):
            return {"version": "8.3.9"}
        def list_database_names(self):
            return ["attendance_benchmark_100k"]

    with pytest.raises(ValueError, match="Safety check failed: unable to ping"):
        verify_server_target(MockClientPingFail(), BENCHMARK_DB_NAME)

    class MockClientListDbsFail:
        class AdminOk:
            def command(self, cmd):
                return {"ok": 1}
        admin = AdminOk()
        def server_info(self):
            return {"version": "8.3.9"}
        def list_database_names(self):
            raise RuntimeError("Database listing forbidden by network policy")

    # MUST NOT SWALLOW list_database_names exception!
    with pytest.raises(ValueError, match="Safety check failed: unable to list database names"):
        verify_server_target(MockClientListDbsFail(), BENCHMARK_DB_NAME)

    # Must reject production databases
    class MockClientProdDbs:
        class AdminOk:
            def command(self, cmd):
                return {"ok": 1}
        admin = AdminOk()
        def server_info(self):
            return {"version": "8.3.9"}
        def list_database_names(self):
            return ["production", "attendance_benchmark_100k"]

    with pytest.raises(ValueError, match="Safety check failed: target MongoDB server contains production databases"):
        verify_server_target(MockClientProdDbs(), BENCHMARK_DB_NAME)


# ==============================================================================
# S5-REG-05: Credential Sanitization (Part 2.F)
# ==============================================================================
def test_s5_reg_05_credential_sanitization():
    """Verify sanitize_mongo_uri and sanitize_error redact username and password credentials."""
    raw_uri = "mongodb://benchmark_user:super_secret_password@127.0.0.1:27017/attendance_benchmark_100k?authSource=admin"
    clean_uri = sanitize_mongo_uri(raw_uri)
    assert "super_secret_password" not in clean_uri
    assert "benchmark_user" not in clean_uri
    assert clean_uri.startswith("mongodb://***:***@127.0.0.1:27017")

    raw_err = f"Authentication failed connecting to {raw_uri}: invalid password"
    clean_err = sanitize_error(raw_err)
    assert "super_secret_password" not in clean_err
    assert "benchmark_user" not in clean_err


# ==============================================================================
# S5-REG-06: Release Builder Symlink Traversal & Integrity (Part 3)
# ==============================================================================
def test_s5_reg_06_release_builder_rejects_external_symlink(tmp_path, monkeypatch):
    """Verify build_release_zip rejects symbolic link pointing outside approved repository tree."""
    from scripts.build_release import build_release_zip

    mock_repo = tmp_path / "mock_symlink_repo"
    mock_repo.mkdir()

    # External file outside repo
    external_secret = tmp_path / "external_secret.txt"
    external_secret.write_text("CONFIDENTIAL_OUTSIDE_DATA", encoding="utf-8")

    # File inside repo simulated as symlink to external secret
    symlink_file = mock_repo / "leaked_symlink.txt"
    symlink_file.write_text("dummy", encoding="utf-8")

    (mock_repo / "README.md").write_text("readme", encoding="utf-8")
    (mock_repo / "app" / "main.py").parent.mkdir(parents=True, exist_ok=True)
    (mock_repo / "app" / "main.py").write_text("code", encoding="utf-8")

    real_islink = os.path.islink
    def fake_islink(p):
        if Path(p).name == "leaked_symlink.txt":
            return True
        return real_islink(p)

    real_resolve = Path.resolve
    def fake_resolve(self, *args, **kwargs):
        if self.name == "leaked_symlink.txt":
            return external_secret.resolve()
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(os.path, "islink", fake_islink)
    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(ValueError, match="External file symlink rejected"):
        build_release_zip(
            output_path=str(tmp_path / "symlink_test.zip"),
            source_dir=str(mock_repo),
            required_files=["README.md", "app/main.py"],
        )


def test_s5_reg_06_release_builder_atomic_replacement_on_failure(tmp_path):
    """Verify that an existing valid release archive is preserved if a build fails midway."""
    from scripts.build_release import build_release_zip

    mock_repo = tmp_path / "mock_atomic_repo"
    mock_repo.mkdir()
    (mock_repo / "README.md").write_text("valid", encoding="utf-8")
    (mock_repo / "app" / "main.py").parent.mkdir(parents=True, exist_ok=True)
    (mock_repo / "app" / "main.py").write_text("code", encoding="utf-8")

    out_zip = tmp_path / "release_submission.zip"

    # 1. Build initial valid release
    build_release_zip(
        output_path=str(out_zip),
        source_dir=str(mock_repo),
        required_files=["README.md", "app/main.py"],
    )
    assert out_zip.exists()
    initial_bytes = out_zip.read_bytes()

    # 2. Attempt build that fails due to missing mandatory file
    with pytest.raises(ValueError, match="Mandatory release file"):
        build_release_zip(
            output_path=str(out_zip),
            source_dir=str(mock_repo),
            required_files=["README.md", "app/main.py", "nonexistent_mandatory_file.py"],
        )

    # 3. Existing release must remain intact and identical
    assert out_zip.exists()
    assert out_zip.read_bytes() == initial_bytes
    # Temporary zip must be cleaned up
    assert not (tmp_path / "release_submission.tmp.zip").exists()


# ==============================================================================
# S5-REG-07: Employee API Input Validation & Business Boundaries (Part 4)
# ==============================================================================
def test_s5_reg_07_employee_validation_whitespace_and_shift_equality(client_app):
    """Verify rejection of whitespace-only names, departments, and equal shift start/end."""
    # Equal shift start/end -> 422
    res = client_app.post(
        "/employees",
        json={
            "emp_code": "EMP8801",
            "name": "Valid Name",
            "email": "shift@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01",
        },
    )
    assert res.status_code == 422

    # Whitespace-only name -> 422
    res = client_app.post(
        "/employees",
        json={
            "emp_code": "EMP8802",
            "name": "   ",
            "email": "name@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    assert res.status_code == 422

    # Whitespace-only department -> 422
    res = client_app.post(
        "/employees",
        json={
            "emp_code": "EMP8803",
            "name": "Valid Name",
            "email": "dept@example.com",
            "department": "   ",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    assert res.status_code == 422


def test_s5_reg_07_department_filter_whitespace_rejected(client_app):
    """Verify department filter with whitespace-only returns 422 across endpoints."""
    # GET /employees
    res = client_app.get("/employees?department=%20%20")
    assert res.status_code == 422

    # GET /attendance with whitespace emp_code
    res = client_app.get("/attendance?emp_code=%20%20")
    assert res.status_code == 422

    # GET /analytics/departments/summary
    res = client_app.get("/analytics/departments/summary?month=2026-03&department=%20%20")
    assert res.status_code == 422

    # GET /analytics/leaderboard/late
    res = client_app.get("/analytics/leaderboard/late?month=2026-03&department=%20%20")
    assert res.status_code == 422

    # GET /analytics/departments/{department}/trend
    res = client_app.get("/analytics/departments/%20%20/trend?from=2026-03-01&to=2026-03-10")
    assert res.status_code == 422


# ==============================================================================
# S5-REG-08: Status Aggregator Mandatory Evidence Validation (Sprint 5 Closure)
# ==============================================================================
def test_s5_reg_08_aggregate_benchmark_status_evidence_completeness():
    """
    Verify aggregate_benchmark_status strictly requires cursor_stats evidence for PASS:
    - Missing cursor_stats entirely -> INCONCLUSIVE (exit code 1)
    - Empty cursor_stats dict -> INCONCLUSIVE (exit code 1)
    - Only 1 metric present -> INCONCLUSIVE (exit code 1)
    - Null metric -> INCONCLUSIVE (exit code 1)
    - Malformed metric (boolean, string, negative) -> FAIL (exit code 1)
    - Complete valid evidence -> PASS (exit code 0)
    - FAIL scenario + missing evidence scenario -> FAIL (FAIL dominates)
    """
    # 1. Missing cursor_stats field entirely across all 8 scenarios
    results_missing = [{"name": f"q{i}", "status": "PASS", "status_reason": "ok"} for i in range(8)]
    st, code, issues = aggregate_benchmark_status(results_missing, expected_scenario_count=8)
    assert st == "INCONCLUSIVE", f"Expected INCONCLUSIVE but got {st}"
    assert code == 1
    assert any("cursor_stats' field is omitted" in iss for iss in issues)

    # 2. Empty cursor_stats dict
    results_empty = [{"name": f"q{i}", "status": "PASS", "status_reason": "ok", "cursor_stats": {}} for i in range(8)]
    st, code, issues = aggregate_benchmark_status(results_empty, expected_scenario_count=8)
    assert st == "INCONCLUSIVE"
    assert code == 1
    assert any("cursor_stats' is empty" in iss for iss in issues)

    # 3. Only 1 metric present (missing others)
    results_partial = [
        {"name": f"q{i}", "status": "PASS", "status_reason": "ok", "cursor_stats": {"totalDocsExamined": 10}}
        for i in range(8)
    ]
    st, code, issues = aggregate_benchmark_status(results_partial, expected_scenario_count=8)
    assert st == "INCONCLUSIVE"
    assert code == 1
    assert any("missing" in iss for iss in issues)

    # 4. Null metric present
    null_stats = {
        "nReturned": 10,
        "totalDocsExamined": None,
        "totalKeysExamined": 10,
        "executionTimeMillis": 5,
    }
    results_null = [
        {"name": f"q{i}", "status": "PASS", "status_reason": "ok", "cursor_stats": null_stats}
        for i in range(8)
    ]
    st, code, issues = aggregate_benchmark_status(results_null, expected_scenario_count=8)
    assert st == "INCONCLUSIVE"
    assert code == 1
    assert any("null" in iss for iss in issues)

    # 5. Malformed metric values (boolean, negative, string)
    bool_stats = {
        "nReturned": 10,
        "totalDocsExamined": True,  # boolean is invalid
        "totalKeysExamined": 10,
        "executionTimeMillis": 5,
    }
    results_bool = [
        {"name": f"q{i}", "status": "PASS", "status_reason": "ok", "cursor_stats": bool_stats}
        for i in range(8)
    ]
    st, code, issues = aggregate_benchmark_status(results_bool, expected_scenario_count=8)
    assert st == "FAIL"
    assert code == 1
    assert any("invalid evidence" in iss for iss in issues)

    neg_stats = {
        "nReturned": 10,
        "totalDocsExamined": -5,
        "totalKeysExamined": 10,
        "executionTimeMillis": 5,
    }
    results_neg = [
        {"name": f"q{i}", "status": "PASS", "status_reason": "ok", "cursor_stats": neg_stats}
        for i in range(8)
    ]
    st, code, issues = aggregate_benchmark_status(results_neg, expected_scenario_count=8)
    assert st == "FAIL"
    assert code == 1

    str_stats = {
        "nReturned": 10,
        "totalDocsExamined": "100",
        "totalKeysExamined": 10,
        "executionTimeMillis": 5,
    }
    results_str = [
        {"name": f"q{i}", "status": "PASS", "status_reason": "ok", "cursor_stats": str_stats}
        for i in range(8)
    ]
    st, code, issues = aggregate_benchmark_status(results_str, expected_scenario_count=8)
    assert st == "FAIL"
    assert code == 1

    # 6. Complete valid evidence -> PASS
    valid_stats = {
        "nReturned": 10,
        "totalDocsExamined": 10,
        "totalKeysExamined": 10,
        "executionTimeMillis": 5,
    }
    results_valid = [
        {"name": f"q{i}", "status": "PASS", "status_reason": "ok", "cursor_stats": valid_stats}
        for i in range(8)
    ]
    st, code, issues = aggregate_benchmark_status(results_valid, expected_scenario_count=8)
    assert st == "PASS"
    assert code == 0
    assert len(issues) == 0

    # 7. Definite FAIL dominates over missing evidence
    results_fail_mixed = [
        {"name": "q0", "status": "FAIL_COLLSCAN", "status_reason": "unindexed scan"},
    ] + [{"name": f"q{i}", "status": "PASS", "status_reason": "ok"} for i in range(1, 8)]
    st, code, issues = aggregate_benchmark_status(results_fail_mixed, expected_scenario_count=8)
    assert st == "FAIL"
    assert code == 1


# ==============================================================================
# S5-REG-09: URI and Exception Sanitization Comprehensive (Sprint 5 Closure)
# ==============================================================================
def test_s5_reg_09_uri_and_error_sanitization_comprehensive():
    """
    Verify sanitize_mongo_uri and sanitize_error redact:
    - Passwords and tokens in query parameters
    - tlsCertificateKeyFilePassword
    - authMechanismProperties with passwords
    - Encoded credentials in authority
    - Exception strings containing connection URIs and raw credential parameters
    - Preserves non-sensitive query parameters (authSource, replicaSet, readPreference)
    """
    # 1. Password and token in query string
    uri_query = "mongodb://localhost:27017/benchmark?password=supersecretpassword&token=tok_abc123&authSource=admin&replicaSet=rs0"
    cleaned = sanitize_mongo_uri(uri_query)
    assert "supersecretpassword" not in cleaned
    assert "tok_abc123" not in cleaned
    assert "password=***" in cleaned
    assert "token=***" in cleaned
    assert "authSource=admin" in cleaned
    assert "replicaSet=rs0" in cleaned

    # 2. tlsCertificateKeyFilePassword
    uri_cert = "mongodb://127.0.0.1:27017/?tlsCertificateKeyFilePassword=cert_pass_999&tls=true"
    cleaned_cert = sanitize_mongo_uri(uri_cert)
    assert "cert_pass_999" not in cleaned_cert
    assert "tlsCertificateKeyFilePassword=***" in cleaned_cert
    assert "tls=true" in cleaned_cert

    # 3. authMechanismProperties with PASSWORD
    uri_auth = "mongodb://cluster.local:27017/db?authMechanismProperties=PASSWORD:mypassword123,SERVICE_NAME:mongo"
    cleaned_auth = sanitize_mongo_uri(uri_auth)
    assert "mypassword123" not in cleaned_auth
    assert "authMechanismProperties=***" in cleaned_auth

    # 4. Authority with encoded credentials
    uri_auth_enc = "mongodb://user%40company:p%40ssw0rd!@localhost:27017/db?secret=mysecretkey"
    cleaned_enc = sanitize_mongo_uri(uri_auth_enc)
    assert "p%40ssw0rd!" not in cleaned_enc
    assert "mysecretkey" not in cleaned_enc
    assert "secret=***" in cleaned_enc
    assert "mongodb://***:***@localhost:27017" in cleaned_enc

    # 5. sanitize_error with full connection strings and inline credentials
    err_text = (
        "pymongo.errors.ServerSelectionTimeoutError: Failed to connect to "
        "mongodb://app_user:secr3t@127.0.0.1:27017/admin?password=nested_pwd&token=my_secret_token: "
        "Authentication failed for user 'app_user' with password='raw_password_in_msg'"
    )
    cleaned_err = sanitize_error(err_text)
    assert "secr3t" not in cleaned_err
    assert "app_user" not in cleaned_err
    assert "nested_pwd" not in cleaned_err
    assert "my_secret_token" not in cleaned_err
    assert "raw_password_in_msg" not in cleaned_err
    assert "password=***" in cleaned_err
    assert "token=***" in cleaned_err


# ==============================================================================
# S5-REG-10: Release Builder Atomic Replacement Failure (Sprint 5 Closure)
# ==============================================================================
def test_s5_reg_10_release_builder_atomic_replacement_failure(tmp_path, monkeypatch):
    """
    Verify that an existing valid release archive remains intact and unchanged
    if an error occurs during atomic replacement (e.g. os.replace raises OSError).
    """
    import os

    from scripts.build_release import build_release_zip

    mock_repo = tmp_path / "mock_atomic_replace_repo"
    mock_repo.mkdir()
    (mock_repo / "README.md").write_text("readme content", encoding="utf-8")
    (mock_repo / "app" / "main.py").parent.mkdir(parents=True, exist_ok=True)
    (mock_repo / "app" / "main.py").write_text("print('hello')", encoding="utf-8")

    out_zip = tmp_path / "release_submission.zip"

    # Step 1: Build initial release archive
    build_release_zip(
        output_path=str(out_zip),
        source_dir=str(mock_repo),
        required_files=["README.md", "app/main.py"],
    )
    assert out_zip.exists()
    original_size = out_zip.stat().st_size
    original_content = out_zip.read_bytes()

    # Step 2: Monkeypatch os.replace to simulate an atomic replacement I/O error
    def mock_failing_replace(src, dst):
        raise OSError("Simulated atomic replacement failure: permission or lock error")

    monkeypatch.setattr(os, "replace", mock_failing_replace)

    # Step 3: Run release builder and assert exception is raised
    with pytest.raises(OSError, match="Simulated atomic replacement failure"):
        build_release_zip(
            output_path=str(out_zip),
            source_dir=str(mock_repo),
            required_files=["README.md", "app/main.py"],
        )

    # Step 4: Verify the original release archive is completely preserved
    assert out_zip.exists()
    assert out_zip.stat().st_size == original_size
    assert out_zip.read_bytes() == original_content

    # Step 5: Verify temporary zip was cleaned up
    temp_zip = tmp_path / "release_submission.tmp.zip"
    assert not temp_zip.exists()


# ==============================================================================
# S5-REG-11: Release Builder Excludes Unrecognized Secrets and Credentials
# ==============================================================================
def test_s5_reg_11_release_builder_excludes_credentials_and_secrets(tmp_path):
    """
    Verify release builder strictly excludes secret and credential filenames:
    - credentials.json
    - secrets.json
    - secret.json
    - password.txt
    - client.pfx / cert.crt / id_ed25519
    """
    import zipfile

    from scripts.build_release import build_release_zip, is_forbidden

    # Verify is_forbidden directly
    assert is_forbidden("credentials.json")
    assert is_forbidden("secrets.json")
    assert is_forbidden("secret.json")
    assert is_forbidden("config/secrets.json")
    assert is_forbidden("password.txt")
    assert is_forbidden("client.pfx")
    assert is_forbidden("cert.crt")
    assert is_forbidden("id_ed25519")
    assert is_forbidden(".env")
    assert is_forbidden(".env.local")

    mock_repo = tmp_path / "mock_secrets_repo"
    mock_repo.mkdir()
    (mock_repo / "README.md").write_text("readme", encoding="utf-8")
    (mock_repo / "app" / "main.py").parent.mkdir(parents=True, exist_ok=True)
    (mock_repo / "app" / "main.py").write_text("code", encoding="utf-8")

    # Add secret files to repo
    (mock_repo / "credentials.json").write_text('{"api_key": "secret"}', encoding="utf-8")
    (mock_repo / "secrets.json").write_text('{"password": "secret"}', encoding="utf-8")
    (mock_repo / "password.txt").write_text("hunter2", encoding="utf-8")

    out_zip = tmp_path / "release_submission.zip"
    build_release_zip(
        output_path=str(out_zip),
        source_dir=str(mock_repo),
        required_files=["README.md", "app/main.py"],
    )

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = zf.namelist()
        assert "credentials.json" not in names
        assert "secrets.json" not in names
        assert "password.txt" not in names


