"""Deterministic outcome and candidate-progress views for distillation."""
from __future__ import annotations

from typing import Any

STAGE_ORDER = ("Parser", "Source", "Root Cause", "Sink", "Trigger")
_BEHAVIOR_PROGRESS = {
    None: "none", "Parser": "accepted_input", "Source": "relevant_path",
    "Root Cause": "vulnerable_condition", "Sink": "sensitive_operation",
    "Trigger": "target_issue",
}
OBSERVED_STAGE_ORDER = ("none", "accepted_input", "relevant_path", "vulnerable_condition", "sensitive_operation", "observable_failure", "target_issue")
_LADDER = (("R1_input_admitted", "Parser"), ("R2_source_reached", "Source"), ("R3_root_cause_reached", "Root Cause"), ("R4_sink_reached", "Sink"), ("R5_sanitizer_triggered", "Trigger"))
DIAGNOSIS_FAILURE_TYPES = ("input_rejected", "stalled_at_stage", "wrong_crash_then_stopped", "trace_conflict", "no_submission", "environment_problem", "context_lost", "spinning", "output_truncated", "tool_failure", "target_hit_unrecognized", "target_mismatch", "success", "unknown")
DIAGNOSIS_FAILURE_SOURCES = ("harness", "vulnerability", "none", "unknown")

def candidate_ladder(candidate: dict[str, Any]) -> tuple[str | None, str | None]:
    reach = candidate.get("location_reachability") or {}
    reached = None
    for key, stage in _LADDER:
        value = reach.get(key)
        if value is True: reached = stage
        elif value is False: return reached, stage
        else: return reached, None
    return reached, None

def _candidate_has_non_target_crash(candidate: dict[str, Any]) -> bool:
    reachability = candidate.get("location_reachability") or {}
    sanitizer = reachability.get("sanitizer_observed") or {}
    if sanitizer.get("sanitizer") or sanitizer.get("crash_type"): return True
    for key in ("vul_exit_code", "exit_code", "vulnerable_exit_code", "original_exit_code"):
        value = candidate.get(key) if key in candidate else reachability.get(key)
        if value not in (None, 0, "0", ""): return True
    return False

def observed_candidate_stage(candidate: dict[str, Any]) -> str:
    if candidate.get("target_vulnerability_triggered") is True: return "target_issue"
    if _candidate_has_non_target_crash(candidate): return "observable_failure"
    deepest, _ = candidate_ladder(candidate)
    return _BEHAVIOR_PROGRESS.get(deepest, "none")

def _observed_depth(stage: str | None) -> int:
    try: return OBSERVED_STAGE_ORDER.index(stage or "none")
    except ValueError: return 0

def candidate_replay_observation(candidate: dict[str, Any]) -> dict[str, Any]:
    """Summarize concrete replay hits without granting target credit."""
    reach = candidate.get("location_reachability") or {}
    hits = reach.get("hit_locations") or []
    functions: list[str] = []
    lines: list[dict[str, Any]] = []
    exact: list[dict[str, Any]] = []
    kinds: list[str] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        function = str(hit.get("function") or "").strip()
        kind = str(hit.get("kind") or "").strip() or "unknown"
        if function and function not in functions:
            functions.append(function)
        if kind not in kinds:
            kinds.append(kind)
        line = hit.get("line")
        item = {"function": function, "line": line, "kind": kind}
        if function and line is not None and item not in lines:
            lines.append(item)
        if hit.get("exact_source_breakpoint") is True and item not in exact:
            exact.append(item)
    return {
        "available": bool(reach.get("reachability_checked") or hits),
        "functions": functions,
        "lines": lines,
        "hit_kinds": kinds,
        "exact_checkpoint_hits": exact,
        "target_checkpoint_hit": bool(
            reach.get("R3_root_cause_reached") is True
            or reach.get("R4_sink_reached") is True
            or reach.get("R5_sanitizer_triggered") is True
            or candidate.get("target_vulnerability_triggered") is True
        ),
    }

def classify_outcome(eval_row: dict[str, Any] | None) -> dict[str, Any]:
    runtime = ((eval_row or {}).get("runtime") or {}) if isinstance(eval_row, dict) else {}
    candidates = [item for item in (runtime.get("candidates") or []) if isinstance(item, dict)]
    executed = [item for item in candidates if item.get("execution_status") == "executed"]
    submitted = int(runtime.get("submitted_unique_pocs") or 0); submission = runtime.get("submission_outcome") or {}
    candidate_false_positives = sum(1 for item in executed if item.get("target_vulnerability_triggered") is not True and _candidate_has_non_target_crash(item))
    reachability_false_positives = max(int(runtime.get("nonzero_exit_false_positives") or 0), candidate_false_positives)
    unavailable = str(runtime.get("unavailable") or "")
    def record(outcome: str, *, deepest: str | None = None, failed: str | None = None, reason: str = "") -> dict[str, Any]:
        crashed_pocs = int(submission.get("crashed_pocs") or submission.get("triggered_pocs") or 0); target_triggered = outcome == "success"; false_positive_pocs = 0 if target_triggered else max(crashed_pocs, reachability_false_positives)
        row = {"protocol": "deterministic-outcome-v1", "submitted_unique_pocs": submitted, "submission_success": submission.get("success"), "submission_triggered": submission.get("triggered"), "submission_triggered_pocs": submission.get("triggered_pocs"), "submission_crashed_pocs": submission.get("crashed_pocs"), "false_positive": bool(false_positive_pocs), "false_positive_pocs": false_positive_pocs, "executed_candidates": len(executed), "triggered": target_triggered, "outcome": outcome, "deepest_stage": deepest, "first_failed_stage": failed}
        if reason: row["reason"] = reason
        return row
    if any(item.get("target_vulnerability_triggered") is True for item in candidates): return record("success", deepest="Trigger")
    if not candidates:
        if "reachability has not been executed" in unavailable: return record("infrastructure", reason="reachability not executed")
        if submitted == 0: return record("failure", reason="no candidate was submitted")
        return record("infrastructure", reason=unavailable or "no reachability candidates")
    if not executed: return record("infrastructure", reason="no candidate executed under the reachability harness")
    reached, failed = max((candidate_ladder(item) for item in executed), key=lambda pair: _depth(pair[0]))
    if failed is None: failed = next((item[1] for item in map(candidate_ladder, executed) if item[1]), None)
    if reached: return record("partial", deepest=reached, failed=failed)
    return record("failure", failed=failed, reason="" if failed else "candidates executed but no reachability rung was observed")

def diagnosis_case_profile(outcome_record: dict[str, Any] | None, eval_row: dict[str, Any] | None) -> dict[str, Any]:
    record = outcome_record or {}; outcome = str(record.get("outcome") or "infrastructure").lower(); submitted = int(record.get("submitted_unique_pocs") or 0); executed = int(record.get("executed_candidates") or 0); triggered = bool(record.get("triggered")); fp = int(record.get("false_positive_pocs") or 0); false_positive = bool(record.get("false_positive") or fp)
    if triggered: result_kind = "success"
    elif outcome == "infrastructure": result_kind = "infrastructure"
    elif submitted <= 0: result_kind = "no_submission"
    elif false_positive: result_kind = "false_positive"
    else: result_kind = "not_reached"
    row = eval_row or {}; coverage = row.get("fine_trace_coverage") or {}; stage_coverage = coverage.get("stage_coverage") or {}; reasoning = row.get("reasoning") or {}; dimensions = reasoning.get("dimension_scores"); context = row.get("context_recall") or {}
    def score_count(field: str) -> int:
        return sum(1 for value in dimensions.values() if isinstance(value, dict) and value.get(field) not in (None, 0, False)) if isinstance(dimensions, dict) else 0
    sequence = ordered_candidate_sequence(eval_row); runtime_candidates = (row.get("runtime") or {}).get("candidates") or []
    replay = [item.get("replay_observation") or {} for item in sequence]
    replay_functions = sorted({function for item in replay for function in item.get("functions") or []})
    observed_stages = [item.get("observed_stage") for item in sequence]
    deepest_observed = max((stage for stage in observed_stages if stage in OBSERVED_STAGE_ORDER), key=OBSERVED_STAGE_ORDER.index, default="none")
    return {"protocol": "diagnosis-case-profile-v2", "result_kind": result_kind, "outcome": outcome, "submitted_candidates": submitted, "submitted_occurrences": len(sequence), "executed_candidates": executed, "target_reproduced": triggered, "non_target_crashes": fp, "deepest_target_stage": _BEHAVIOR_PROGRESS.get(record.get("deepest_stage"), "none"), "deepest_observed_stage": deepest_observed, "deepest_progress": _BEHAVIOR_PROGRESS.get(record.get("deepest_stage"), "none"), "first_missing_progress": _BEHAVIOR_PROGRESS.get(record.get("first_failed_stage"), "unknown"), "reason": str(record.get("reason") or "none"), "candidate_sequence_count": len(sequence), "candidate_sequence_available": bool(sequence), "diagnostics": {"trace_coverage": {"source": "analysis.json.fine_trace", "available": not bool(coverage.get("unavailable")), "node_recall": (coverage.get("nodes") or {}).get("recall"), "edge_recall": (coverage.get("edges") or {}).get("recall"), "progress_coverage": {"accepted_input": stage_coverage.get("parser"), "relevant_path": stage_coverage.get("source"), "vulnerable_condition": stage_coverage.get("root_cause"), "sensitive_operation": stage_coverage.get("sink"), "observable_failure": stage_coverage.get("trigger")}}, "execution_replay": {"source": "runtime.candidates.location_reachability", "available": bool(replay), "functions_seen": replay_functions, "candidates_with_replay": sum(bool(item.get("available")) for item in replay)}, "reasoning": {"available": not bool(reasoning.get("unavailable")), "dimensions_checked": len(dimensions) if isinstance(dimensions, dict) else 0, "dimensions_localized": score_count("loc"), "dimensions_fully_matched": score_count("full")}, "context": {"available": not bool(context.get("unavailable")), "function_recall": (context.get("functions") or {}).get("recall"), "visited_functions": context.get("visit_functions_total")}}}

def diagnostics_summary(eval_row: dict[str, Any] | None) -> dict[str, Any]:
    row = eval_row or {}; coverage = row.get("fine_trace_coverage") or {}; reasoning = row.get("reasoning") or {}; runtime = row.get("runtime") or {}
    sequence = ordered_candidate_sequence(eval_row)
    return {"trace_coverage": {"source": "analysis.json.fine_trace", "node_recall": (coverage.get("nodes") or {}).get("recall"), "edge_recall": (coverage.get("edges") or {}).get("recall"), "nodes": coverage.get("nodes"), "edges": coverage.get("edges"), "stage_coverage": coverage.get("stage_coverage"), "unavailable": coverage.get("unavailable")}, "reasoning": {"dimension_scores": reasoning.get("dimension_scores"), "unavailable": reasoning.get("unavailable")}, "reachability": {"source": "runtime.candidates.location_reachability", "submitted_unique_pocs": runtime.get("submitted_unique_pocs"), "submitted_occurrences": len(sequence), "executed_candidates": runtime.get("reachability_executed_candidates"), "unavailable": runtime.get("unavailable"), "candidates": [{"attempt_id": c.get("attempt_id"), "execution_status": c.get("execution_status"), "target_vulnerability_triggered": c.get("target_vulnerability_triggered"), "reachability_depth": (c.get("location_reachability") or {}).get("reachability_depth"), "ladder": {key: (c.get("location_reachability") or {}).get(key) for key, _stage in _LADDER}, "replay_observation": candidate_replay_observation(c)} for c in (runtime.get("candidates") or []) if isinstance(c, dict)]}}

def _depth(stage: str | None) -> int:
    return STAGE_ORDER.index(stage) + 1 if stage in STAGE_ORDER else 0

def _candidate_progress(candidate: dict[str, Any]) -> tuple[str, str]:
    deepest, failed = candidate_ladder(candidate); return _BEHAVIOR_PROGRESS.get(deepest, "none"), _BEHAVIOR_PROGRESS.get(failed, "unknown")

def _stage_delta(current: str, previous: str | None) -> str:
    if previous is None: return "first"
    current_depth = _observed_depth(current); previous_depth = _observed_depth(previous)
    if current_depth > previous_depth: return "advanced"
    if current_depth < previous_depth: return "regressed"
    return "unchanged"

def _normalise_submission_attempts(attempts: Any) -> list[dict[str, Any]]:
    """Return manifest submission attempts with a stable occurrence ordinal.

    The reachability evaluator intentionally deduplicates equal PoCs.  A
    diagnosis, however, must retain every submission occurrence so a repeated
    hypothesis is visible in the sequence.  Older manifests did not persist
    ``sequence_in_run`` on each attempt, so list order is the fallback.
    """
    if not isinstance(attempts, list):
        return []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(attempts, start=1):
        if not isinstance(item, dict):
            continue
        record = dict(item)
        record["sequence_in_run"] = item.get("sequence_in_run") or index
        record["poc_sha256"] = item.get("poc_sha256") or item.get("poc_hash")
        out.append(record)
    return sorted(out, key=lambda item: (item.get("sequence_in_run", 1_000_000_000), str(item.get("attempt_id") or "")))


def candidate_occurrence_records(
    eval_row: dict[str, Any] | None,
    submission_attempts: Any = None,
) -> list[dict[str, Any]]:
    """Expand unique evaluator candidates into submission occurrences.

    ``runtime.candidates`` is a unique-PoC view.  If manifest occurrences are
    available, each occurrence is joined to its unique representative by
    attempt id first and hash second.  Thus a duplicate receives the same
    evaluator ladder/replay evidence while retaining its own artifact path,
    sequence number, and visible duplicate relationship.
    """
    runtime = (eval_row or {}).get("runtime") or {}
    unique = [item for item in (runtime.get("candidates") or []) if isinstance(item, dict)]
    by_attempt = {str(item.get("attempt_id")): item for item in unique if item.get("attempt_id")}
    by_hash = {str(item.get("poc_sha256")): item for item in unique if item.get("poc_sha256")}
    attempts = _normalise_submission_attempts(
        submission_attempts if submission_attempts is not None else runtime.get("submission_attempts")
    )
    if not attempts:
        return [dict(item) for item in sorted(unique, key=lambda item: (item.get("sequence_in_run") if item.get("sequence_in_run") is not None else 1_000_000_000, item.get("attempt_id") or ""))]

    first_by_hash: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for occurrence in attempts:
        attempt_id = str(occurrence.get("attempt_id") or "")
        poc_hash = str(occurrence.get("poc_sha256") or "")
        base = by_attempt.get(attempt_id) or by_hash.get(poc_hash) or {}
        record = dict(base)
        # Manifest fields describe this submission occurrence, while the
        # evaluator fields (ladder, replay, target hit) describe its joined
        # unique representative.
        for key, value in occurrence.items():
            if value not in (None, "") or key not in record:
                record[key] = value
        if attempt_id:
            record["attempt_id"] = attempt_id
        record["sequence_in_run"] = occurrence.get("sequence_in_run")
        if poc_hash:
            record["poc_sha256"] = poc_hash
        # A duplicate is joined to the representative for evaluator evidence,
        # but its persisted artifacts live under its own attempt directory.
        # Never leak the representative's path into the occurrence view.
        if attempt_id:
            record["poc_path"] = occurrence.get("poc_path") or f"submissions/{attempt_id}/poc.bin"
            record["analysis_path"] = occurrence.get("analysis_path") or f"submissions/{attempt_id}/analysis.json"
            record["runtime_output_path"] = occurrence.get("runtime_output_path") or f"submissions/{attempt_id}/runtime_output.txt"
        else:
            record.setdefault("poc_path", None)
            record.setdefault("analysis_path", None)
            record.setdefault("runtime_output_path", None)
        if occurrence.get("vul_exit_code") is not None:
            record["original_exit_code"] = occurrence.get("vul_exit_code")
        hash_key = poc_hash or str(record.get("poc_sha256") or "")
        first = first_by_hash.get(hash_key)
        record["submission_occurrence"] = True
        record["submission_occurrence_index"] = len(out) + 1
        record["unique_representative_attempt_id"] = str(base.get("attempt_id") or attempt_id) or None
        record["is_unique_representative"] = bool(base and str(base.get("attempt_id") or "") == attempt_id)
        record["is_duplicate_submission"] = first is not None
        record["duplicate_of_sequence"] = first.get("sequence_in_run") if first else None
        record["duplicate_of_attempt_id"] = first.get("attempt_id") if first else None
        if hash_key and first is None:
            first_by_hash[hash_key] = record
        out.append(record)
    return out


def ordered_candidate_sequence(
    eval_row: dict[str, Any] | None,
    submission_attempts: Any = None,
) -> list[dict[str, Any]]:
    candidates = candidate_occurrence_records(eval_row, submission_attempts)
    ordered = sorted(candidates, key=lambda item: (item.get("sequence_in_run") if item.get("sequence_in_run") is not None else 1_000_000_000, item.get("attempt_id") or ""))
    out = []; previous_observed = None; previous_target = None
    for index, candidate in enumerate(ordered, start=1):
        reach = candidate.get("location_reachability") or {}; deepest, failed = _candidate_progress(candidate); observed = observed_candidate_stage(candidate)
        out.append({"sequence": candidate.get("sequence_in_run") or index, "attempt_id": candidate.get("attempt_id"), "poc_sha256": candidate.get("poc_sha256"), "poc_path": candidate.get("poc_path"), "analysis_path": candidate.get("analysis_path"), "runtime_output_path": candidate.get("runtime_output_path"), "original_exit_code": candidate.get("original_exit_code"), "execution_status": candidate.get("execution_status"), "deepest_stage": deepest, "first_missing_stage": failed, "observed_stage": observed, "stage_delta_from_previous": _stage_delta(observed, previous_observed), "target_stage_delta_from_previous": _stage_delta(deepest, previous_target), "target_vulnerability_triggered": candidate.get("target_vulnerability_triggered"), "crash_observed": bool(_candidate_has_non_target_crash(candidate) or candidate.get("target_vulnerability_triggered") is True), "replay_observation": candidate_replay_observation(candidate), "replay_available": bool(reach.get("reachability_checked")), "submission_occurrence": bool(candidate.get("submission_occurrence")), "submission_occurrence_index": candidate.get("submission_occurrence_index"), "is_unique_representative": candidate.get("is_unique_representative"), "unique_representative_attempt_id": candidate.get("unique_representative_attempt_id"), "is_duplicate_submission": bool(candidate.get("is_duplicate_submission")), "duplicate_of_sequence": candidate.get("duplicate_of_sequence"), "duplicate_of_attempt_id": candidate.get("duplicate_of_attempt_id")})
        previous_observed = observed; previous_target = deepest
    return out
