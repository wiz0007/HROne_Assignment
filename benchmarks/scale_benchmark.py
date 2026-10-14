"""
Sprint 5 Scale & Performance Benchmark
Employee Attendance & Analytics API - Sprint 5 Engineering Hardening

Measures query plans, execution statistics, and latency for all 5 explain aliases
against ~100,000 attendance records and 1,000 employees in an isolated benchmark database.

Safety & Verification Features:
- S5-SAFE-01: Explicit opt-in (--confirm / CONFIRM_BENCHMARK=1) required before any client/DB action.
  Import-time side-effects eliminated: no MongoClient or application connection during module import.
- S5-SAFE-02: Fail-closed database guard (exact allowlist matching) and server target validation.
  Guards run before every destructive operation using explicit ValueError exceptions (-O safe).
- S5-BENCH-01: Strict explain-metric validation schema distinguishing observed zero from missing/null/malformed.
- S5-BENCH-02: Pure status aggregation function with strict precedence (FAIL > INCONCLUSIVE > PASS).
- S5-BENCH-03: Scenario-specific index policies and result correctness invariants.
- S5-SCALE-01: Live scale validation saving reproducible evidence to benchmarks/benchmark_results.json.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
import sys
import time
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Database and Target Constants
BENCHMARK_DB_NAME = "attendance_benchmark_100k"
APPROVED_BENCHMARK_DATABASES = {BENCHMARK_DB_NAME}
APPROVED_BENCHMARK_HOSTS = {"localhost", "127.0.0.1", "::1"}
APPROVED_BENCHMARK_PORTS = {27017}

AUTHORITATIVE_SCENARIOS = {
    "attendance_list (selective by emp_code + range)",
    "attendance_list (date-range across all employees, page 1)",
    "employee_monthly (single employee aggregation)",
    "department_summary (department-scoped: Engineering)",
    "department_summary (company-wide: all 10 departments)",
    "late_leaderboard (company-wide, limit=10)",
    "late_leaderboard (department-scoped: Engineering, limit=10)",
    "department_trend (30-day window: Engineering)",
}

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "benchmark_results.json")


def sanitize_mongo_uri(uri: str) -> str:
    """
    Safely parses and sanitizes a MongoDB connection URI.
    Redacts username/password credentials in authority and all sensitive
    authentication, password, token, certificate-key, and credential parameters in query strings.
    Preserves non-sensitive operational parameters (e.g., authSource, replicaSet, readPreference).
    """
    if not uri or not isinstance(uri, str):
        return ""

    # 1. Redact authority credentials (user:pass@ -> ***:***@ and user@ -> ***@)
    sanitized = re.sub(r"://([^:@\s/?#]+):([^@\s/?#]+)@", r"://***:***@", uri)
    sanitized = re.sub(r"://([^:@\s/?#]+)@", r"://***@", sanitized)

    # 2. Parse and redact sensitive query parameters
    if "?" in sanitized:
        base_part, _, query_with_frag = sanitized.partition("?")
        query_part, frag_sep, fragment = query_with_frag.partition("#")

        params = parse_qsl(query_part, keep_blank_values=True)
        if params:
            sanitized_params = []
            sensitive_keywords = (
                "pass", "pwd", "secret", "token", "cred", "private", "key"
            )
            for k, v in params:
                k_clean = k.lower().replace("-", "").replace("_", "")
                is_sensitive = (
                    any(kw in k_clean for kw in sensitive_keywords)
                    or k.lower() in ("authmechanismproperties", "tlscertificatekeyfilepassword", "proxypassword")
                )
                if is_sensitive:
                    sanitized_params.append((k, "***"))
                else:
                    sanitized_params.append((k, v))
            new_query = urlencode(sanitized_params)
            sanitized = base_part + "?" + new_query + (frag_sep + fragment if frag_sep else "")

    # 3. Fallback regex sweep for query parameters in non-standard or malformed formats
    sanitized = re.sub(
        r"([?&;](?:password|pwd|secret|token|cred|apiKey|key|tlsCertificateKeyFilePassword|authMechanismProperties|proxyPassword)[^=&\s]*)=([^&\s#]+)",
        r"\1=***",
        sanitized,
        flags=re.IGNORECASE,
    )

    return sanitized


def sanitize_error(err: Exception | str) -> str:
    """
    Sanitizes exception messages or error strings to remove credentials,
    connection URIs with auth info, and sensitive query/key parameters.
    """
    if err is None:
        return ""
    msg = str(err)

    # 1. Replace all MongoDB connection URIs
    def _replace_uri(match):
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in (":", ",", ".", ";"):
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        return sanitize_mongo_uri(raw) + trailing

    msg = re.sub(r"mongodb(?:\+srv)?://[^\s'\"\)\]\}]+", _replace_uri, msg)

    # 2. Redact username:password@ patterns that may not start with mongodb scheme
    msg = re.sub(r"://([^:@\s/?#]+):([^@\s/?#]+)@", r"://***:***@", msg)
    msg = re.sub(r"://([^:@\s/?#]+)@", r"://***@", msg)

    # 3. Redact sensitive key=value or key:value patterns in exception text
    msg = re.sub(
        r"(?i)\b(password|pwd|secret|token|cred|apiKey|key|tlsCertificateKeyFilePassword|authMechanismProperties|proxyPassword)\b\s*([:=])\s*['\"]?([^'\",\s;&\)\]\}]+)['\"]?",
        r"\1\2***",
        msg,
    )

    # 4. Redact PASSWORD:xxx or AWS_SECRET_ACCESS_KEY:xxx inside authMechanismProperties or error text
    msg = re.sub(
        r"(?i)\b(AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|PASSWORD)\s*:\s*([^'\",\s;&\)\]\}]+)",
        r"\1:***",
        msg,
    )

    # 5. Redact user/username references in error text: e.g. user 'app_user', user=app_user, username: admin
    msg = re.sub(
        r"(?i)\b(user|username)\b\s*([:=])\s*['\"]?([^'\",\s;&\)\]\}]+)['\"]?",
        r"\1\2***",
        msg,
    )
    msg = re.sub(
        r"(?i)\b(user|username)\b\s+['\"]([^'\"]+)['\"]",
        r"\1 '***'",
        msg,
    )

    return msg



# ==============================================================================
# S5-SAFE-01: Authorization & Opt-in Checks
# ==============================================================================
def check_benchmark_opt_in(confirm: bool = False) -> bool:
    """
    Verifies that benchmark execution has been explicitly authorized.
    Accepts: CLI confirm=True, or CONFIRM_BENCHMARK in ("1", "true", "yes").
    Rejects: False, empty, "0", "false", or any unrecognized string.
    """
    if confirm is True:
        return True
    raw_env = os.getenv("CONFIRM_BENCHMARK")
    if raw_env is None:
        return False
    val = raw_env.strip().lower()
    return val in ("1", "true", "yes")


# ==============================================================================
# S5-SAFE-02: Fail-Closed Database and Server Target Validation
# ==============================================================================
def validate_safe_benchmark_database(db_name: str, allowed_dbs: Optional[set[str]] = None) -> None:
    """
    Safety guard: verify we NEVER touch an unapproved or non-benchmark database.
    Uses explicit exceptions to ensure protection is NEVER bypassed in Python optimized mode (-O).
    Requires exact allowlist/equality matching: names merely containing 'benchmark' are rejected.
    """
    if not db_name or not isinstance(db_name, str):
        raise ValueError("Safety check failed: database name must be a non-empty string!")
    name = db_name.strip().lower()

    allowlist = allowed_dbs if allowed_dbs is not None else APPROVED_BENCHMARK_DATABASES
    if name not in allowlist:
        raise ValueError(
            f"Safety check failed: database '{name}' is not in approved benchmark allowlist {allowlist}!"
        )


assert_safe_benchmark_database = validate_safe_benchmark_database


def validate_safe_benchmark_target(
    uri: str,
    allowed_hosts: Optional[set[str]] = None,
    expected_target: Optional[str] = None,
) -> None:
    """
    Validates that the target MongoDB connection string is directed to an approved benchmark target.
    Prevents silent fallback to production clusters, Atlas connections, or remote environments.
    """
    if not uri or not isinstance(uri, str):
        raise ValueError("Safety check failed: MongoDB URI must be a non-empty string!")

    parsed = urlsplit(uri)
    scheme = parsed.scheme.lower()
    if scheme not in ("mongodb", "mongodb+srv"):
        raise ValueError(f"Safety check failed: unrecognized MongoDB URI scheme '{scheme}'!")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("Safety check failed: MongoDB URI does not specify a valid hostname!")

    # Reject remote production clusters or Atlas indicators
    forbidden_host_markers = [".mongodb.net", "prod", "production", "live", "cluster-prod"]
    if any(marker in hostname for marker in forbidden_host_markers):
        raise ValueError(f"Safety check failed: target host '{hostname}' indicates production cluster!")

    hosts_allowlist = allowed_hosts if allowed_hosts is not None else APPROVED_BENCHMARK_HOSTS
    if hostname not in hosts_allowlist:
        raise ValueError(
            f"Safety check failed: target host '{hostname}' is not in approved benchmark hosts {hosts_allowlist}!"
        )

    # Check port if present
    if parsed.port is not None and parsed.port not in APPROVED_BENCHMARK_PORTS:
        raise ValueError(
            f"Safety check failed: target port {parsed.port} is not in approved ports {APPROVED_BENCHMARK_PORTS}!"
        )

    # Check database path in URI if present
    path = parsed.path.strip("/")
    if path:
        path_lower = path.lower()
        if path_lower not in APPROVED_BENCHMARK_DATABASES:
            raise ValueError(
                f"Safety check failed: URI database path '{path}' does not match approved benchmark database!"
            )

    # Check expected target configuration if provided
    exp = expected_target or os.getenv("BENCHMARK_EXPECTED_TARGET")
    if exp:
        exp_clean = exp.strip().lower()
        if exp_clean != hostname and exp_clean != f"{hostname}:{parsed.port}":
            raise ValueError(
                f"Safety check failed: target '{hostname}' does not match expected target '{exp}'!"
            )


def verify_server_target(client, db_name: str) -> dict:
    """
    Verifies connection to MongoDB target and confirms the server is safe for benchmark operations.
    Fails closed if the server is unreachable, cannot list databases, or reports production characteristics.
    Never swallows verification exceptions.
    """
    if client is None:
        raise ValueError("Safety check failed: MongoClient instance is None")

    validate_safe_benchmark_database(db_name)

    try:
        client.admin.command("ping")
    except Exception as e:
        raise ValueError(f"Safety check failed: unable to ping MongoDB server target: {sanitize_error(e)}") from e

    try:
        server_info = client.server_info()
        if not server_info or not isinstance(server_info, dict):
            raise ValueError("Safety check failed: server_info returned empty or non-dict")
    except Exception as e:
        raise ValueError(f"Safety check failed: unable to verify server information: {sanitize_error(e)}") from e

    try:
        existing_dbs = client.list_database_names()
        if not isinstance(existing_dbs, list):
            raise ValueError("Safety check failed: list_database_names returned non-list")
    except Exception as e:
        raise ValueError(f"Safety check failed: unable to list database names: {sanitize_error(e)}") from e

    prod_markers = {"production", "prod_db", "company_production"}
    if any(pdb in existing_dbs for pdb in prod_markers):
        raise ValueError("Safety check failed: target MongoDB server contains production databases!")

    return server_info



# ==============================================================================
# S5-BENCH-01: Explain-Plan Metric Schema Parsing & Validation
# ==============================================================================
def validate_non_negative_int(data: dict, key: str) -> tuple[Optional[int], Optional[str]]:
    """
    Validates a non-negative integer explain metric.
    Returns (value, status) where status is None for valid values, or:
      - 'MISSING': key not present in dict
      - 'NULL': key present but explicitly None
      - 'WRONG_TYPE': key is not an int or is a bool
      - 'NEGATIVE': int < 0
    """
    if key not in data:
        return None, "MISSING"
    val = data[key]
    if val is None:
        return None, "NULL"
    if isinstance(val, bool) or not isinstance(val, int):
        return None, "WRONG_TYPE"
    if val < 0:
        return None, "NEGATIVE"
    return val, None


def validate_index_list(data: dict, key: str) -> tuple[Optional[list[str]], Optional[str]]:
    """
    Validates an index list metric.
    Returns (value, status) where status is None for valid lists, or:
      - 'MISSING': key not present in dict
      - 'NULL': key present but explicitly None
      - 'WRONG_TYPE': not a list or elements not strings
    """
    if key not in data:
        return None, "MISSING"
    val = data[key]
    if val is None:
        return None, "NULL"
    if not isinstance(val, list):
        return None, "WRONG_TYPE"
    if not all(isinstance(x, str) for x in val):
        return None, "WRONG_TYPE"
    return val, None


def extract_plan_info(explain_doc: dict) -> dict:
    """
    Traverses explain output across find queries and aggregation pipelines,
    identifying the winning plan, extracting its stage hierarchy, isolating
    rejected candidate plans, and recording executionStats, cursor-level metrics,
    and lookup stage statistics with strict schema validation.
    """
    if not isinstance(explain_doc, dict):
        return {
            "winning_plan": {},
            "rejected_plans": [],
            "plan_stages": [],
            "pipeline_stages": [],
            "indexes_used": [],
            "exec_stats": {},
            "cursor_stats": {},
            "lookup_metrics": [],
            "pipeline_returned": None,
            "is_unsupported_structure": True,
        }

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
        wp = qp.get("winningPlan", {}) if isinstance(qp, dict) else {}
        winning_plan = wp.get("queryPlan", wp) if isinstance(wp, dict) else {}
        rejected_plans = qp.get("rejectedPlans", []) if isinstance(qp, dict) else []

    # Case B: Top-level executionStats
    if "executionStats" in explain_doc and isinstance(explain_doc["executionStats"], dict):
        es = explain_doc["executionStats"]
        n_ret, n_st = validate_non_negative_int(es, "nReturned")
        k_ex, k_st = validate_non_negative_int(es, "totalKeysExamined")
        d_ex, d_st = validate_non_negative_int(es, "totalDocsExamined")
        t_ms, t_st = validate_non_negative_int(es, "executionTimeMillis")

        exec_stats = {
            "nReturned": n_ret,
            "nReturned_status": n_st,
            "totalKeysExamined": k_ex,
            "totalKeysExamined_status": k_st,
            "totalDocsExamined": d_ex,
            "totalDocsExamined_status": d_st,
            "executionTimeMillis": t_ms,
            "executionTimeMillis_status": t_st,
        }
        pipeline_returned = n_ret

    # Case C: Aggregation pipeline stages
    if "stages" in explain_doc and isinstance(explain_doc["stages"], list):
        for stg in explain_doc["stages"]:
            if not isinstance(stg, dict):
                continue
            stage_keys = [k for k in stg if k.startswith("$")]
            stage_name = stage_keys[0] if stage_keys else (next(iter(stg.keys())) if stg else "UNKNOWN")
            pipeline_stages.append(stage_name)
            if "nReturned" in stg and pipeline_returned is None:
                pipeline_returned, _ = validate_non_negative_int(stg, "nReturned")

            if "$cursor" in stg and isinstance(stg["$cursor"], dict):
                c = stg["$cursor"]
                if not winning_plan and "queryPlanner" in c and isinstance(c["queryPlanner"], dict):
                    qp = c["queryPlanner"]
                    wp = qp.get("winningPlan", {}) if isinstance(qp, dict) else {}
                    winning_plan = wp.get("queryPlan", wp) if isinstance(wp, dict) else {}
                    rejected_plans = qp.get("rejectedPlans", []) if isinstance(qp, dict) else []

                c_es = c.get("executionStats", {}) if isinstance(c.get("executionStats"), dict) else {}
                c_n_ret, c_n_st = validate_non_negative_int(c_es, "nReturned")
                if c_n_ret is None and "nReturned" in stg:
                    c_n_ret, c_n_st = validate_non_negative_int(stg, "nReturned")

                c_k_ex, c_k_st = validate_non_negative_int(c_es, "totalKeysExamined")
                c_d_ex, c_d_st = validate_non_negative_int(c_es, "totalDocsExamined")
                c_t_ms, c_t_st = validate_non_negative_int(c_es, "executionTimeMillis")
                if c_t_ms is None and "executionTimeMillisEstimate" in stg:
                    c_t_ms, c_t_st = validate_non_negative_int(stg, "executionTimeMillisEstimate")

                cursor_stats = {
                    "nReturned": c_n_ret,
                    "nReturned_status": c_n_st,
                    "totalKeysExamined": c_k_ex,
                    "totalKeysExamined_status": c_k_st,
                    "totalDocsExamined": c_d_ex,
                    "totalDocsExamined_status": c_d_st,
                    "executionTimeMillis": c_t_ms,
                    "executionTimeMillis_status": c_t_st,
                }
                if not exec_stats:
                    exec_stats = dict(cursor_stats)

            if "$lookup" in stg:
                lookup_payload = stg.get("$lookup", {})
                if not isinstance(lookup_payload, dict):
                    lookup_payload = {}

                # Check top-level or payload-level fields
                merged_lookup = dict(lookup_payload)
                for k in ("indexesUsed", "totalDocsExamined", "totalKeysExamined", "collectionScans"):
                    if k in stg:
                        merged_lookup[k] = stg[k]

                idx_val, idx_st = validate_index_list(merged_lookup, "indexesUsed")
                docs_val, docs_st = validate_non_negative_int(merged_lookup, "totalDocsExamined")
                keys_val, keys_st = validate_non_negative_int(merged_lookup, "totalKeysExamined")
                coll_val, coll_st = validate_non_negative_int(merged_lookup, "collectionScans")

                is_malformed = any(st in ("WRONG_TYPE", "NEGATIVE") for st in (coll_st, docs_st, keys_st, idx_st))
                is_missing = any(st in ("MISSING", "NULL") for st in (coll_st, docs_st, keys_st, idx_st))

                lookup_info = {
                    "indexesUsed": idx_val,
                    "indexesUsed_status": idx_st,
                    "totalDocsExamined": docs_val,
                    "totalDocsExamined_status": docs_st,
                    "totalKeysExamined": keys_val,
                    "totalKeysExamined_status": keys_st,
                    "collectionScans": coll_val,
                    "collectionScans_status": coll_st,
                    "is_malformed": is_malformed,
                    "is_missing": is_missing,
                }
                lookup_metrics.append(lookup_info)
                if idx_val:
                    lookup_indexes.extend(idx_val)

    def get_stages(node):
        stages = []
        if isinstance(node, dict):
            if node.get("stage"):
                stages.append(str(node["stage"]).upper())
            for k in ("inputStage", "outerStage", "innerStage"):
                if k in node:
                    stages.extend(get_stages(node[k]))
            if "inputStages" in node and isinstance(node["inputStages"], list):
                for s in node["inputStages"]:
                    stages.extend(get_stages(s))
        return stages

    def get_indexes(node):
        idx = []
        if isinstance(node, dict):
            if node.get("indexName"):
                idx.append(node["indexName"])
            for k in ("inputStage", "outerStage", "innerStage"):
                if k in node:
                    idx.extend(get_indexes(node[k]))
            if "inputStages" in node and isinstance(node["inputStages"], list):
                for s in node["inputStages"]:
                    idx.extend(get_indexes(s))
        return idx

    plan_stages = get_stages(winning_plan)
    indexes_used = sorted(set(get_indexes(winning_plan) + lookup_indexes))

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
        "is_unsupported_structure": len(plan_stages) == 0 and len(pipeline_stages) == 0,
    }


# ==============================================================================
# S5-BENCH-03: Scenario Policy Evaluation
# ==============================================================================
def evaluate_benchmark_scenario(
    scenario_policy: dict,
    plan_info: dict,
    real_latency_ms: float,
    actual_result_count: int = 1,
) -> tuple[str, str, dict]:
    """
    Evaluates a single benchmark scenario against its declared policy contract.
    Returns (status, status_reason, metrics_summary).

    Possible status codes:
      - PASS: all criteria satisfied with valid metrics
      - FAIL_COLLSCAN: disallowed primary or lookup collection scan observed
      - FAIL_EXPECTED_IXSCAN: expected index scan missing from primary plan
      - FAIL_UNINDEXED_LOOKUP: lookup examined documents without using indexes
      - FAIL_LATENCY: real execution latency exceeded SLA threshold
      - FAIL_RESULT_INVARIANT: document output failed correctness invariant
      - INCONCLUSIVE_MISSING_METRICS: required primary executionStats missing/null
      - INCONCLUSIVE_MALFORMED_METRICS: metrics present but malformed/negative
      - INCONCLUSIVE_MISSING_LOOKUP_METRICS: expected lookup stages missing/incomplete
      - INCONCLUSIVE_UNSUPPORTED_STRUCTURE: explain structure unrecognized
    """
    if plan_info.get("is_unsupported_structure"):
        return (
            "INCONCLUSIVE_UNSUPPORTED_STRUCTURE",
            "Explain plan structure unrecognized or contains no query stages",
            {},
        )

    plan_stages = plan_info.get("plan_stages", [])
    exec_stats = plan_info.get("exec_stats", {})
    lookup_metrics = plan_info.get("lookup_metrics", [])
    sla_threshold_ms = scenario_policy.get("sla_threshold_ms", float("inf"))
    expected_min_count = scenario_policy.get("expected_min_count", 1)

    has_primary_ixscan = any("IXSCAN" in s or "IXSEEK" in s for s in plan_stages)
    has_primary_collscan = "COLLSCAN" in plan_stages

    has_lookup_collscan = any(
        lm.get("collectionScans") is not None and lm["collectionScans"] > 0
        for lm in lookup_metrics
    )
    has_missing_lookup_metrics = any(lm.get("is_missing", False) for lm in lookup_metrics)
    has_malformed_lookup_metrics = any(lm.get("is_malformed", False) for lm in lookup_metrics)

    lookup_collscans = (
        sum(lm["collectionScans"] for lm in lookup_metrics if lm.get("collectionScans") is not None)
        if (lookup_metrics and not has_missing_lookup_metrics and not has_malformed_lookup_metrics)
        else (0 if not lookup_metrics else None)
    )
    has_unindexed_lookup = any(
        (lm.get("totalDocsExamined") or 0) > 0 and not lm.get("indexesUsed")
        for lm in lookup_metrics
    )

    metrics_summary = {
        "has_primary_ixscan": has_primary_ixscan,
        "has_primary_collscan": has_primary_collscan,
        "has_lookup_collscan": has_lookup_collscan,
        "lookup_collscans": lookup_collscans,
        "missing_lookup_metrics": has_missing_lookup_metrics,
        "malformed_lookup_metrics": has_malformed_lookup_metrics,
        "has_unindexed_lookup": has_unindexed_lookup,
        "real_latency_ms": real_latency_ms,
        "sla_threshold_ms": sla_threshold_ms,
        "actual_result_count": actual_result_count,
        "expected_min_count": expected_min_count,
    }

    # 1. Primary executionStats completeness checks
    # Must NOT be empty, missing, or unsupported structure
    if not exec_stats or not isinstance(exec_stats, dict):
        return (
            "INCONCLUSIVE_MISSING_METRICS",
            "Required primary executionStats object is missing or empty",
            metrics_summary,
        )

    required_primary_keys = ("totalDocsExamined", "totalKeysExamined", "executionTimeMillis")
    for k in required_primary_keys:
        st = exec_stats.get(f"{k}_status")
        val = exec_stats.get(k)
        if st in ("WRONG_TYPE", "NEGATIVE") or (val is not None and (not isinstance(val, int) or isinstance(val, bool) or val < 0)):
            return (
                "INCONCLUSIVE_MALFORMED_METRICS",
                f"Primary executionStats metric '{k}' is malformed ({st})",
                metrics_summary,
            )
        if k not in exec_stats or st in ("MISSING", "NULL") or val is None:
            return (
                "INCONCLUSIVE_MISSING_METRICS",
                f"Required primary executionStats metric '{k}' is missing or null",
                metrics_summary,
            )

    # 2. Primary scan and index rules
    if scenario_policy.get("prohibit_primary_collscan", True) and has_primary_collscan:
        return "FAIL_COLLSCAN", "Primary query plan executed COLLSCAN where prohibited", metrics_summary

    if scenario_policy.get("expect_primary_ixscan", False) and not has_primary_ixscan:
        return "FAIL_EXPECTED_IXSCAN", "Expected primary index scan (IXSCAN/IXSEEK) not found in plan stages", metrics_summary

    # 3. Lookup stage checks
    if scenario_policy.get("expect_lookup", False):
        if not lookup_metrics:
            return "INCONCLUSIVE_MISSING_LOOKUP_METRICS", "Scenario expected $lookup stages, but none were captured in explain plan", metrics_summary

    for idx, lm in enumerate(lookup_metrics):
        if lm.get("is_malformed"):
            return "INCONCLUSIVE_MALFORMED_METRICS", f"Lookup stage #{idx+1} contained malformed metric values", metrics_summary
        if lm.get("is_missing"):
            return "INCONCLUSIVE_MISSING_LOOKUP_METRICS", f"Required lookup metric in stage #{idx+1} is missing or null", metrics_summary
        if scenario_policy.get("prohibit_lookup_collscan", True):
            coll_scans = lm.get("collectionScans")
            if coll_scans is not None and coll_scans > 0:
                return "FAIL_COLLSCAN", f"Lookup stage executed {coll_scans} collection scan(s) where zero was required", metrics_summary
        if scenario_policy.get("expect_lookup", False) or scenario_policy.get("expect_indexed_lookup", False):
            idx_used = lm.get("indexesUsed")
            # Enforce indexed lookup independently of document counts:
            # Zero examined documents does NOT remove the requirement to establish index behavior!
            if not idx_used:
                return (
                    "FAIL_UNINDEXED_LOOKUP",
                    f"Lookup stage #{idx+1} failed indexed-lookup requirement: indexesUsed is empty or missing (zero examined documents does not remove requirement)",
                    metrics_summary,
                )

    # 4. Correctness Invariant Check
    if actual_result_count < expected_min_count:
        return (
            "FAIL_RESULT_INVARIANT",
            f"Actual result document count {actual_result_count} is below expected minimum {expected_min_count}",
            metrics_summary,
        )

    # 5. Latency SLA Check
    if real_latency_ms > sla_threshold_ms:
        return "FAIL_LATENCY", f"Real latency {real_latency_ms}ms exceeded SLA threshold {sla_threshold_ms}ms", metrics_summary

    # 6. All criteria passed
    return "PASS", scenario_policy.get("decision_reason", "Scenario criteria satisfied"), metrics_summary


# ==============================================================================
# S5-BENCH-02: Status Aggregation Function
# ==============================================================================
def aggregate_benchmark_status(
    query_results: list[dict],
    expected_scenarios: Optional[set[str] | list[str] | int] = None,
    expected_scenario_count: Optional[int] = None,
) -> tuple[str, int, list[str]]:
    """
    Pure aggregation function that converts scenario results into overall status and exit code.
    Validates exact expected scenario set: rejects missing, extra, duplicate, or malformed scenarios.
    Precedence rule: FAIL > INCONCLUSIVE > PASS.
    A definite failure is NEVER hidden by an inconclusive result.
    Returns: (overall_status, exit_code, issues_list)
      - "PASS", 0, [] when all required scenarios pass with complete valid evidence.
      - "FAIL", 1, issues when any scenario failed, count mismatch, extra/missing scenarios, or malformed.
      - "INCONCLUSIVE", 1, issues when evidence is incomplete and no definite failure.
    """
    issues = []
    if not query_results:
        return "FAIL", 1, ["No benchmark scenarios executed (empty results list)."]

    # Resolve expected set and count
    target_set = None
    if isinstance(expected_scenarios, (set, list)):
        target_set = set(expected_scenarios)
        target_count = len(target_set)
    elif isinstance(expected_scenarios, int):
        target_count = expected_scenarios
    elif isinstance(expected_scenario_count, int):
        target_count = expected_scenario_count
    else:
        target_set = AUTHORITATIVE_SCENARIOS
        target_count = len(AUTHORITATIVE_SCENARIOS)

    # 1. Exact count check
    if len(query_results) < target_count:
        issues.append(f"Expected {target_count} scenarios, but only received {len(query_results)}.")
    elif len(query_results) > target_count:
        issues.append(f"Expected {target_count} scenarios, but received {len(query_results)} (unexpected extra scenarios detected).")

    # 2. Record validation and duplicate detection
    has_duplicates = False
    has_malformed_record = False
    seen_names = set()

    for r in query_results:
        if not isinstance(r, dict):
            issues.append("Malformed result record: item is not a dictionary.")
            has_malformed_record = True
            continue
        name = r.get("name")
        if not name or not isinstance(name, str):
            issues.append("Malformed result record: missing or invalid 'name'.")
            has_malformed_record = True
            continue
        if name in seen_names:
            issues.append(f"Duplicate scenario identifier detected: '{name}'.")
            has_duplicates = True
        seen_names.add(name)

    # 3. Exact scenario set validation
    if target_set is not None:
        missing_scenarios = target_set - seen_names
        extra_scenarios = seen_names - target_set
        if missing_scenarios:
            issues.append(f"Missing required scenario(s): {sorted(missing_scenarios)}.")
        if extra_scenarios:
            issues.append(f"Unexpected extra scenario(s): {sorted(extra_scenarios)}.")

    has_failure = (
        has_duplicates
        or has_malformed_record
        or len(query_results) != target_count
        or (target_set is not None and (bool(target_set - seen_names) or bool(seen_names - target_set)))
    )
    has_inconclusive = False

    for r in query_results:
        if not isinstance(r, dict):
            continue
        st = r.get("status")
        reason = r.get("status_reason", "")
        name = r.get("name", "unknown")

        if not st or not isinstance(st, str):
            has_failure = True
            issues.append(f"Scenario '{name}' has missing or non-string status.")
        elif st.startswith("FAIL"):
            has_failure = True
            issues.append(f"Scenario '{name}' FAILED: {st} - {reason}")
        elif st.startswith("INCONCLUSIVE"):
            has_inconclusive = True
            issues.append(f"Scenario '{name}' INCONCLUSIVE: {st} - {reason}")
        elif st == "PASS":
            # Mandatory evidence check: every PASS scenario must provide valid cursor_stats evidence
            if "cursor_stats" not in r:
                has_inconclusive = True
                issues.append(f"Scenario '{name}' marked PASS without mandatory evidence ('cursor_stats' field is omitted).")
            else:
                c_stats = r["cursor_stats"]
                if not isinstance(c_stats, dict):
                    has_failure = True
                    issues.append(f"Scenario '{name}' has malformed evidence ('cursor_stats' is not a dictionary).")
                elif len(c_stats) == 0:
                    has_inconclusive = True
                    issues.append(f"Scenario '{name}' marked PASS without mandatory evidence ('cursor_stats' is empty).")
                else:
                    mandatory_metrics = ["totalDocsExamined", "totalKeysExamined", "executionTimeMillis", "nReturned"]
                    for metric in mandatory_metrics:
                        _val, err_st = validate_non_negative_int(c_stats, metric)
                        if err_st in ("WRONG_TYPE", "NEGATIVE"):
                            has_failure = True
                            issues.append(
                                f"Scenario '{name}' has invalid evidence: cursor_stats.{metric}={c_stats.get(metric)!r} ({err_st})."
                            )
                        elif err_st in ("MISSING", "NULL"):
                            has_inconclusive = True
                            issues.append(
                                f"Scenario '{name}' marked PASS with incomplete evidence: cursor_stats.{metric} is {err_st.lower()}."
                            )
        else:
            has_failure = True
            issues.append(f"Scenario '{name}' returned unrecognized status '{st}': {reason}")

    # Enforce strict precedence: FAIL > INCONCLUSIVE > PASS
    if has_failure:
        return "FAIL", 1, issues
    elif has_inconclusive:
        return "INCONCLUSIVE", 1, issues
    else:
        return "PASS", 0, []


# ==============================================================================
# Main Benchmark Execution Engine (S5-SCALE-01)
# ==============================================================================
def run_benchmark(confirm: bool = False, uri: Optional[str] = None):
    # 1. Authorization check
    if not check_benchmark_opt_in(confirm):
        print("\n[!] SAFETY ABORT: Explicit confirmation required before executing benchmark.")
        print("    The scale benchmark generates ~100,000 records and drops the benchmark database.")
        print("    To run, pass the --confirm flag or set CONFIRM_BENCHMARK=1:")
        print("        python benchmarks/scale_benchmark.py --confirm\n")
        sys.exit(1)

    target_uri = uri or MONGO_URI

    # 2. Pre-connection validation
    validate_safe_benchmark_database(BENCHMARK_DB_NAME)
    validate_safe_benchmark_target(target_uri)

    # 3. Import MongoClient and application builders behind authorization boundary
    from pymongo import MongoClient
    from app.main import (
        create_required_indexes,
        build_attendance_list_query,
        build_employee_monthly_pipeline,
        build_department_summary_pipeline,
        build_late_leaderboard_pipeline,
        build_department_trend_pipeline,
    )

    client = None
    try:
        client = MongoClient(target_uri)
        db = client[BENCHMARK_DB_NAME]

        # 4. Server identity verification
        server_info = verify_server_target(client, BENCHMARK_DB_NAME)
        mongo_version = server_info.get("version", "unknown")

        print(f"[*] Initializing benchmark database: {BENCHMARK_DB_NAME}")
        print(f"    Target MongoDB URI: {sanitize_mongo_uri(target_uri)}")
        print(f"    MongoDB Server Version: {mongo_version}")
        print(f"    Python Environment: {sys.version.split()[0]} on {sys.platform}")

        # Pre-cleanup in case of prior aborted run
        validate_safe_benchmark_database(BENCHMARK_DB_NAME)
        validate_safe_benchmark_target(target_uri)
        client.drop_database(BENCHMARK_DB_NAME)

        start_idx = time.perf_counter()
        create_required_indexes(db)
        idx_duration_ms = (time.perf_counter() - start_idx) * 1000
        print(f"[+] Indexes created idempotently in {idx_duration_ms:.1f}ms")

        # Capture index inventory from test database
        employees_indexes = {k: v.get("key") for k, v in db.employees.index_information().items()}
        attendance_indexes = {k: v.get("key") for k, v in db.attendance_logs.index_information().items()}

        # 5. Seed 1,000 employees
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

        # 6. Seed >100,000 attendance records across 140 days
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
        actual_seeded = db.attendance_logs.count_documents({})
        if actual_seeded != total_seeded:
            raise RuntimeError(
                f"Database seed verification failed: expected {total_seeded} documents, found {actual_seeded}"
            )

        # 7. Benchmark Scenarios Definition
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
                "expected_min_count": 20,
                "expect_primary_ixscan": True,
                "prohibit_primary_collscan": True,
                "expect_lookup": False,
                "prohibit_lookup_collscan": False,
                "decision_reason": "Selective employee date-range query must utilize compound index (emp_code, date) and avoid primary COLLSCAN.",
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
                "expected_min_count": 20,
                "expect_primary_ixscan": True,
                "prohibit_primary_collscan": True,
                "expect_lookup": False,
                "prohibit_lookup_collscan": False,
                "decision_reason": "Date-range query across all employees must utilize date index and avoid primary COLLSCAN.",
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
                "expected_min_count": 1,
                "expect_primary_ixscan": True,
                "prohibit_primary_collscan": True,
                "expect_lookup": False,
                "prohibit_lookup_collscan": False,
                "decision_reason": "Employee monthly aggregation begins with selective emp_code/date match and must use compound index without COLLSCAN.",
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
                "expected_min_count": 1,
                "expect_primary_ixscan": True,
                "prohibit_primary_collscan": True,
                "expect_lookup": True,
                "prohibit_lookup_collscan": True,
                "decision_reason": "Department-filtered summary must utilize department index on employees, and indexed lookup on attendance_logs with zero lookup COLLSCAN.",
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
                "sla_threshold_ms": 3000,
                "expected_min_count": 10,
                "expect_primary_ixscan": False,
                "prohibit_primary_collscan": False,
                "expect_lookup": True,
                "prohibit_lookup_collscan": True,
                "decision_reason": "Company-wide summary scans all employees (legitimate scan on employees collection), but requires strict indexed lookups on attendance_logs with zero lookup collection scans.",
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
                "expected_min_count": 1,
                "expect_primary_ixscan": True,
                "prohibit_primary_collscan": True,
                "expect_lookup": True,
                "prohibit_lookup_collscan": True,
                "decision_reason": "Company-wide late leaderboard matches late attendance records via date index and joins employee metadata via emp_code index with zero lookup COLLSCAN.",
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
                "expected_min_count": 1,
                "expect_primary_ixscan": True,
                "prohibit_primary_collscan": True,
                "expect_lookup": True,
                "prohibit_lookup_collscan": True,
                "decision_reason": "Department-scoped late leaderboard filters late attendance logs via index and performs indexed lookup into employees with zero lookup COLLSCAN.",
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
                "expected_min_count": 30,
                "expect_primary_ixscan": True,
                "prohibit_primary_collscan": True,
                "expect_lookup": True,
                "prohibit_lookup_collscan": True,
                "decision_reason": "Department trend filters department via index and joins 30-day attendance logs via compound index with zero lookup COLLSCAN.",
            },
        ]

        query_results = []
        for b in benchmarks:
            name = b["name"]
            alias = b["alias"]
            col = b["target_collection"]
            cmd = b["explain_cmd"]

            # 1. Execute explain command to extract query plan and executionStats
            raw_explain = db.command(cmd)
            info = extract_plan_info(raw_explain)

            # 2. Execute real operation to measure latency and verify output invariant
            t_exec_start = time.perf_counter()
            actual_result_count = b["real_run"]()
            real_latency_ms = round((time.perf_counter() - t_exec_start) * 1000, 2)

            stats = info["exec_stats"]
            cursor_stats = info["cursor_stats"]
            lookup_metrics = info["lookup_metrics"]
            stages = " -> ".join(info["plan_stages"][:4]) if info["plan_stages"] else "SBE/Pipeline"
            indexes = ", ".join(info["indexes_used"]) if info["indexes_used"] else "N/A"

            docs_examined = stats.get("totalDocsExamined")
            keys_examined = stats.get("totalKeysExamined")
            cursor_matched = cursor_stats.get("nReturned", stats.get("nReturned"))
            explain_time_ms = stats.get("executionTimeMillis")

            # 3. Evaluate scenario against policy contract
            status, status_reason, metrics_summary = evaluate_benchmark_scenario(
                scenario_policy=b,
                plan_info=info,
                real_latency_ms=real_latency_ms,
                actual_result_count=actual_result_count,
            )

            query_results.append({
                "alias": alias,
                "name": name,
                "target_collection": col,
                "policy": {
                    "expect_primary_ixscan": b["expect_primary_ixscan"],
                    "prohibit_primary_collscan": b["prohibit_primary_collscan"],
                    "expect_lookup": b["expect_lookup"],
                    "prohibit_lookup_collscan": b["prohibit_lookup_collscan"],
                    "sla_threshold_ms": b["sla_threshold_ms"],
                    "expected_min_count": b["expected_min_count"],
                },
                "decision_reason": b["decision_reason"],
                "status": status,
                "status_reason": status_reason,
                "stages": stages,
                "pipeline_stages": info["pipeline_stages"],
                "indexes_used": info["indexes_used"],
                "has_ixscan": metrics_summary.get("has_primary_ixscan"),
                "has_collscan": metrics_summary.get("has_primary_collscan", False) or metrics_summary.get("has_lookup_collscan", False),
                "has_primary_collscan": metrics_summary.get("has_primary_collscan"),
                "has_lookup_collscan": metrics_summary.get("has_lookup_collscan"),
                "lookup_collscans": metrics_summary.get("lookup_collscans"),
                "missing_lookup_metrics": metrics_summary.get("missing_lookup_metrics"),
                "malformed_lookup_metrics": metrics_summary.get("malformed_lookup_metrics"),
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
            })

            print(f"\nQuery: {name}")
            print(f"  Target Collection: {col}")
            print(f"  Winning Plan Stages: {stages}")
            print(f"  Indexes Used: {indexes}")
            print(f"  Cursor Matched: {cursor_matched} | Final Output Count: {actual_result_count}")
            print(f"  Examined: Docs={docs_examined}, Keys={keys_examined}")
            if lookup_metrics:
                print(f"  Lookup Metrics: {lookup_metrics}")
            scans_disp = metrics_summary.get("lookup_collscans") if metrics_summary.get("lookup_collscans") is not None else "UNKNOWN/MISSING"
            print(f"  Status: {status} ({status_reason}) [Explain={explain_time_ms}ms, Real={real_latency_ms}ms, SLA:<={b['sla_threshold_ms']}ms, Lookup Collscans:{scans_disp}]")

        # 8. Pure Status Aggregation (S5-BENCH-02)
        overall_status, exit_code, issues = aggregate_benchmark_status(query_results, AUTHORITATIVE_SCENARIOS)

        print("\n" + "=" * 80)
        print(f"BENCHMARK SUMMARY: OVERALL {overall_status} ({'ALL 8 QUERIES PASSED' if overall_status == 'PASS' else 'NON-PASS DETECTED'})")
        print("=" * 80)
        for r in query_results:
            scans_disp = r['lookup_collscans'] if r.get('lookup_collscans') is not None else "UNKNOWN/MISSING"
            print(f"  [{r['status']}] {r['name']} (Real: {r['real_latency_ms']}ms, Lookup Collscans: {scans_disp}) - Reason: {r['status_reason']}")

        # 9. Save Structured Results Artifact
        benchmark_payload = {
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "environment": {
                    "python_version": sys.version.split()[0],
                    "platform": sys.platform,
                    "mongodb_version": mongo_version,
                    "target_database": BENCHMARK_DB_NAME,
                    "target_uri": sanitize_mongo_uri(target_uri),
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
                "overall_status": overall_status,
                "exit_code": exit_code,
                "issues": issues,
            },
            "queries": query_results,
        }

        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(benchmark_payload, f, indent=2)

        print(f"\n[+] Benchmark results saved successfully to {RESULTS_FILE}")

        if exit_code != 0:
            raise RuntimeError(f"Scale benchmark non-PASS ({overall_status}): {'; '.join(issues)}")

        return benchmark_payload

    finally:
        # Safe, Guaranteed Cleanup (S5-SAFE-02)
        if client is not None:
            print(f"\n[*] Cleaning up benchmark database {BENCHMARK_DB_NAME}...")
            validate_safe_benchmark_database(BENCHMARK_DB_NAME)
            validate_safe_benchmark_target(target_uri)
            client.drop_database(BENCHMARK_DB_NAME)
            print("[+] Benchmark database dropped cleanly.")
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sprint 5 Scale & Performance Benchmark")
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Explicitly confirm execution of the benchmark on the dedicated database.",
    )
    args = parser.parse_args()

    # Pre-import authorization guard
    if not check_benchmark_opt_in(args.confirm):
        print("\n[!] SAFETY ABORT: Explicit confirmation required before executing benchmark.")
        print("    The scale benchmark generates ~100,000 records and drops the benchmark database.")
        print("    To run, pass the --confirm flag or set CONFIRM_BENCHMARK=1:")
        print("        python benchmarks/scale_benchmark.py --confirm\n")
        sys.exit(1)

    try:
        run_benchmark(confirm=True)
    except Exception as e:
        print(f"\n[!] Benchmark run failed: {sanitize_error(e)}", file=sys.stderr)
        sys.exit(1)
