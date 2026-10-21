"""Sample-level outcome pools for the Teacher.

The Teacher compares concrete diagnoses directly. ``pools.json`` is the only
training memory: every entry comes from the diagnostician output plus the
deterministic outcome for one sample. We intentionally avoid precomputed
failure-mode keys so the Teacher can perform reason-level comparison instead of
being constrained by mechanical buckets.

Folding is idempotent by batch. Re-running a batch replaces that batch's pool
contribution instead of inflating counts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .diagnosis_view import DIAGNOSIS_SECTIONS, RUBRIC_CATEGORIES, RUBRIC_FINDINGS_KEY

EXCERPT_CHARS = 640

_FAILURE_OUTCOMES = {"failure", "partial"}
_SUCCESS_OUTCOMES = {"success"}
_INFRA_OUTCOMES = {"infrastructure"}


def empty_pools() -> dict[str, Any]:
    return {
        "protocol": "distillation-pools",
        "batches_seen": [],
        "samples_by_batch": {},
        "success_pool": [],
        "false_positive_pool": [],
        "failure_pool": [],
        "infrastructure_pool": [],
    }


def load_pools(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return empty_pools()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_pools()
    if not isinstance(payload, dict):
        return empty_pools()
    upgraded = empty_pools()
    for key in ("batches_seen", "samples_by_batch", "success_pool", "false_positive_pool", "failure_pool", "infrastructure_pool"):
        if key in payload:
            upgraded[key] = payload[key]
    _upgrade_in_place(upgraded)
    return upgraded


def save_pools(path: Path, pools: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pools, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge_pools(*pool_sets: dict[str, Any]) -> dict[str, Any]:
    """Merge persisted pool snapshots, with later snapshots owning a batch.

    A post-hoc diagnosis directory may contain only the current batch.  Its
    Correction still needs the immediately preceding TRAIN batches, while a
    rerun of a batch must replace—not double count—its older pool records.
    """
    merged = empty_pools()
    for payload in pool_sets:
        if not isinstance(payload, dict):
            continue
        # Upgrading legacy payloads must not mutate a caller-owned object.
        source = json.loads(json.dumps(payload))
        _upgrade_in_place(source)
        for raw_batch in source.get("batches_seen") or []:
            try:
                batch = int(raw_batch)
            except (TypeError, ValueError):
                continue
            _forget_batch(merged, batch)
            merged["batches_seen"] = [item for item in merged["batches_seen"] if int(item) != batch]
            merged["samples_by_batch"][str(batch)] = list(
                (source.get("samples_by_batch") or {}).get(str(batch), [])
            )
            for name in ("success_pool", "false_positive_pool", "failure_pool", "infrastructure_pool"):
                merged[name].extend(
                    sample for sample in source.get(name, [])
                    if str(sample.get("batch")) == str(batch)
                )
            merged["batches_seen"].append(batch)
    merged["batches_seen"] = sorted({int(item) for item in merged["batches_seen"]})
    _upgrade_in_place(merged)
    return merged


def update_pools(pools: dict[str, Any], batch_index: int, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold one batch of deterministic outcomes plus diagnoses into sample pools."""
    _upgrade_in_place(pools)
    _forget_batch(pools, batch_index)
    if batch_index not in pools["batches_seen"]:
        pools["batches_seen"].append(batch_index)
    pools["samples_by_batch"][str(batch_index)] = []

    for record in records:
        outcome_record = record.get("outcome_record") or {}
        outcome = str(outcome_record.get("outcome") or record.get("outcome") or "").lower()
        sample = _sample_record(record, outcome_record, batch_index)
        if outcome in _SUCCESS_OUTCOMES:
            pool_name = "success_pool"
        elif outcome in _INFRA_OUTCOMES:
            pool_name = "infrastructure_pool"
        elif _is_false_positive_sample(sample):
            pool_name = "false_positive_pool"
        elif outcome in _FAILURE_OUTCOMES:
            pool_name = "failure_pool"
        else:
            continue
        pools[pool_name].append(sample)
        pools["samples_by_batch"][str(batch_index)].append({
            "sample_id": sample.get("sample_id"),
            "pool": pool_name,
        })
    return pools



def teacher_view(pools: dict[str, Any], *, limit: int = 80, through_batch: int | None = None) -> dict[str, Any]:
    """Bounded sample-level view handed to the Teacher.

    When regenerating old batches after later batches have been folded into
    pools.json, keep the Teacher's view chronological: batch N may only see
    diagnoses from batches <= N. `limit` is a total input budget, not a
    separate allowance for every outcome pool. Keep all counterexamples when
    possible; they are the evidence needed to learn and gate safe lessons.
    """
    _upgrade_in_place(pools)
    visible_successes = _visible_samples(pools.get("success_pool") or [], through_batch)
    visible_false_positives = _visible_samples(pools.get("false_positive_pool") or [], through_batch)
    visible_failures = _visible_samples(pools.get("failure_pool") or [], through_batch)
    visible_infrastructure = _visible_samples(pools.get("infrastructure_pool") or [], through_batch)
    bounded = _bounded_teacher_pools(
        visible_successes,
        visible_false_positives,
        visible_failures,
        visible_infrastructure,
        limit=limit,
    )
    failure_pool = _pool_view(bounded["failure_pool"], limit=limit)
    success_pool = _pool_view(bounded["success_pool"], limit=limit)
    false_positive_pool = _pool_view(bounded["false_positive_pool"], limit=limit)
    infrastructure_pool = _pool_view(bounded["infrastructure_pool"], limit=limit)
    return {
        "protocol": "teacher-pools-view",
        "comparison_policy": {
            "mode": "success_false_positive_failure_contrast",
            "primary_objective": (
                "Improve labeled reproduction success within the finite interaction budget by treating "
                "agent-visible submission feedback as part of the search loop."
            ),
            "secondary_objective": (
                "Use labeled outcomes to assess candidate behaviors, but make future lessons conditional "
                "only on public source or issue evidence, local observations, or the actual submission response."
            ),
            "rubric_categories": list(RUBRIC_CATEGORIES),
            "note": (
                "Use labeled success, crash-producing, and failure pools only as training evidence. A bare "
                "crash/no-crash status is not future path feedback; use it to choose a source-informed next "
                "step, and reserve path comparison for locally or remotely visible crash details."
            ),
        },
        "counts": {
            "success_pool": len(success_pool),
            "false_positive_pool": len(false_positive_pool),
            "failure_pool": len(failure_pool),
            "infrastructure_pool": len(infrastructure_pool),
            "visible_before_limit": sum(len(items) for items in (
                visible_successes,
                visible_false_positives,
                visible_failures,
                visible_infrastructure,
            )),
            "returned_total": sum(len(items) for items in (
                success_pool,
                false_positive_pool,
                failure_pool,
                infrastructure_pool,
            )),
            "total_limit": max(0, int(limit)),
            "batches_seen": len([b for b in pools.get("batches_seen") or [] if through_batch is None or int(b) <= through_batch]),
        },
        "success_pool": success_pool,
        "false_positive_pool": false_positive_pool,
        "crash_pool": false_positive_pool,
        "failure_pool": failure_pool,
        "infrastructure_pool": infrastructure_pool,
    }


def correction_view(
    pools: dict[str, Any],
    *,
    batch_index: int,
    window_size: int = 3,
    limit: int = 80,
) -> dict[str, Any]:
    """Return Correction's bounded *full-history* evidence view.

    Correction still uses the rolling matched comparison to decide whether an
    already-applied update should be kept, narrowed, or rolled back.  It must
    nevertheless see the same accumulated pool as Teacher: otherwise a
    recurring operational gap can be mistaken for an isolated current-batch
    accident, and generic existing advice is too easily treated as coverage.

    ``window_size`` remains in the signature for CLI compatibility.  It only
    governs the separately supplied matched comparison, not what behavioral
    evidence Correction may inspect.
    """
    del window_size
    view = teacher_view(pools, limit=limit, through_batch=int(batch_index))
    view["protocol"] = "correction-full-history-pools-view-v2"
    view["scope"] = {
        "through_batch": int(batch_index),
        "history": "all accumulated TRAIN evidence available through this batch",
        "total_limit": max(0, int(limit)),
    }
    view["comparison_policy"] = {
        **(view.get("comparison_policy") or {}),
        "mode": "full_history_failure_coverage_plus_recent_matched_calibration",
        "note": (
            "Use the complete bounded history to identify recurring observable gaps and "
            "the separately supplied matched rolling comparison to decide rollback or "
            "same-batch calibration. This is offline TRAIN evidence and must never be "
            "copied into a future lesson."
        ),
    }
    return view



def build_validation_panel(
    pools: dict[str, Any],
    *,
    through_batch: int | None = None,
    success_count: int = 2,
    crash_count: int = 2,
    failure_count: int = 2,
    panel_index: int = 0,
) -> dict[str, Any]:
    """Build a deterministic proposal-validation panel from sample pools.

    The panel is intentionally separate from Teacher/Curator prompting. It gives
    every candidate packet the same small regression check: preserve successes,
    preserve successes, compare crash-producing runs, and improve ordinary failures.
    """
    _upgrade_in_place(pools)
    visible_successes = _visible_samples(pools.get("success_pool") or [], through_batch)
    crash_samples = _visible_samples(pools.get("false_positive_pool") or [], through_batch)
    plain_failures = _visible_samples(pools.get("failure_pool") or [], through_batch)

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(role: str, samples: list[dict[str, Any]], count: int) -> None:
        if count <= 0 or not samples:
            return
        ordered = list(reversed(samples))
        offset = (max(0, panel_index) * count) % len(ordered)
        rotated = ordered[offset:] + ordered[:offset]
        for sample in rotated:
            sample_id = str(sample.get("sample_id") or "")
            if not sample_id or sample_id in seen:
                continue
            selected.append({"role": role, **_teacher_sample(sample)})
            seen.add(sample_id)
            if sum(1 for item in selected if item.get("role") == role) >= count:
                break

    add("success_preservation", visible_successes, max(0, success_count))
    add("crash_reference", crash_samples, max(0, crash_count))
    add("failure_recovery", plain_failures, max(0, failure_count))

    # If most failures are risk-shaped, still keep the requested failure section
    # populated without duplicating samples.
    if sum(1 for item in selected if item.get("role") == "failure_recovery") < max(0, failure_count):
        add("failure_recovery", plain_failures + crash_samples, max(0, failure_count))

    counts: dict[str, int] = {}
    for item in selected:
        role = str(item.get("role") or "")
        counts[role] = counts.get(role, 0) + 1
    return {
        "protocol": "skill-update-validation-panel-v1",
        "through_batch": through_batch,
        "panel_index": panel_index,
        "selection_policy": {
            "success_preservation": "recent successful samples",
            "crash_reference": "recent failed/partial samples with explicit crash/submission evidence but no GT trigger",
            "failure_recovery": "recent failed/partial samples not already selected as false positives when available",
        },
        "requested_counts": {
            "success_preservation": success_count,
            "crash_reference": crash_count,
            "failure_recovery": failure_count,
        },
        "counts": counts,
        "samples": selected,
    }


# --------------------------------------------------------------------------- internals


def _upgrade_in_place(pools: dict[str, Any]) -> None:
    defaults = empty_pools()
    for key, value in defaults.items():
        pools.setdefault(key, value)
    false_positives = list(pools.get("false_positive_pool") or [])
    failures = []
    seen_fp = {str(sample.get("sample_id")) for sample in false_positives if sample.get("sample_id")}
    for sample in pools.get("failure_pool") or []:
        if _is_false_positive_sample(sample):
            sample_id = str(sample.get("sample_id") or "")
            if not sample_id or sample_id not in seen_fp:
                false_positives.append(sample)
                if sample_id:
                    seen_fp.add(sample_id)
        else:
            failures.append(sample)
    pools["false_positive_pool"] = false_positives
    pools["failure_pool"] = failures
    pools["protocol"] = "distillation-pools"


def _forget_batch(pools: dict[str, Any], batch_index: int) -> None:
    previous = pools.setdefault("samples_by_batch", {}).pop(str(batch_index), [])
    previous_ids = {sample.get("sample_id") for sample in previous if sample.get("sample_id")}
    if previous_ids:
        for name in ("success_pool", "false_positive_pool", "failure_pool", "infrastructure_pool"):
            pools[name] = [
                sample for sample in pools.get(name, [])
                if sample.get("sample_id") not in previous_ids
            ]


def _sample_record(
    record: dict[str, Any],
    outcome_record: dict[str, Any],
    batch_index: int,
) -> dict[str, Any]:
    outcome = outcome_record.get("outcome") or record.get("outcome")
    crashed_pocs = int(outcome_record.get("submission_crashed_pocs") or outcome_record.get("submission_triggered_pocs") or 0)
    false_positive_pocs = int(record.get("false_positive_pocs") or outcome_record.get("false_positive_pocs") or (crashed_pocs if crashed_pocs and outcome != "success" else 0))
    return {
        "batch": batch_index,
        "sample_id": record.get("sample_id"),
        "project": record.get("project"),
        "outcome": outcome,
        "issue_description": _short(record.get("issue_description")),
        "vulnerability_type": record.get("vulnerability_type"),
        **{section: _short_value(record.get(section)) for section in DIAGNOSIS_SECTIONS},
        RUBRIC_FINDINGS_KEY: _rubric_findings(record.get(RUBRIC_FINDINGS_KEY)),
        "false_positive": bool(record.get("false_positive") or outcome_record.get("false_positive") or false_positive_pocs),
        "false_positive_pocs": false_positive_pocs,
    }


def _short(value: Any, limit: int = EXCERPT_CHARS) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _short_value(value: Any, limit: int = EXCERPT_CHARS) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _short_value(item, limit=limit)
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [_short_value(item, limit=limit) for item in value if item not in (None, "", [], {})]
    return _short(value, limit=limit)


def _visible_samples(samples: list[dict[str, Any]], through_batch: int | None) -> list[dict[str, Any]]:
    visible = []
    for sample in samples:
        try:
            batch = int(sample.get("batch"))
        except (TypeError, ValueError):
            continue
        if through_batch is None or batch <= through_batch:
            visible.append(sample)
    return sorted(
        visible,
        key=lambda sample: (
            int(sample.get("batch")),
            str(sample.get("sample_id") or ""),
        ),
    )


def _false_positive_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failure-side counterexamples where crashing did not equal GT success."""
    return [
        sample for sample in samples
        if _is_false_positive_sample(sample)
    ]


def _is_false_positive_sample(sample: dict[str, Any]) -> bool:
    return bool(sample.get("false_positive")) or int(sample.get("false_positive_pocs") or 0) > 0


def _rubric_findings(value: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return findings
    allowed = set(RUBRIC_CATEGORIES)
    for item in value:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "")
        if category not in allowed:
            continue
        findings.append({
            "category": category,
            "present": bool(item.get("present")),
            "evidence": _short(item.get("evidence")),
        })
    return findings


def _pool_view(samples: list[dict[str, Any]], *, limit: int = 80) -> list[dict[str, Any]]:
    return [_teacher_sample(sample) for sample in samples[-limit:]]


def _bounded_teacher_pools(
    successes: list[dict[str, Any]],
    false_positives: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    infrastructure: list[dict[str, Any]],
    *,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    """Cap a Teacher view globally while retaining outcome counterexamples.

    Successes are useful contrast, but ordinary failures and false-positive
    crashes expose the behavior a new lesson must correct.  Retain every
    non-success record whenever it fits; then fill the remaining budget with
    the most recent successes.  The exceptional all-non-success case is
    balanced across the three non-success pools rather than silently dropping
    an outcome type.
    """
    budget = max(0, int(limit))
    groups = {
        "success_pool": successes,
        "false_positive_pool": false_positives,
        "failure_pool": failures,
        "infrastructure_pool": infrastructure,
    }
    total = sum(len(items) for items in groups.values())
    if total <= budget:
        return groups

    non_success_names = ("false_positive_pool", "failure_pool", "infrastructure_pool")
    non_success_total = sum(len(groups[name]) for name in non_success_names)
    if non_success_total <= budget:
        kept = {name: list(groups[name]) for name in non_success_names}
        kept["success_pool"] = list(successes[-(budget - non_success_total):])
        return kept

    # Rare with a 100-sample TRAIN split, but make the overflow behavior
    # explicit and fair: take recent records round-robin across failure modes.
    selected = {name: [] for name in groups}
    remaining = {name: list(reversed(groups[name])) for name in non_success_names}
    while budget > 0 and any(remaining.values()):
        for name in non_success_names:
            if budget <= 0:
                break
            if remaining[name]:
                selected[name].append(remaining[name].pop(0))
                budget -= 1
    for name in non_success_names:
        selected[name].reverse()
    return selected


def _teacher_sample(sample: dict[str, Any]) -> dict[str, Any]:
    """Concrete evidence for Teacher/Curator artifacts; never written to skills."""
    out = {
        key: sample.get(key)
        for key in (
            "sample_id",
            "project",
            "outcome",
            "issue_description",
            "vulnerability_type",
            *DIAGNOSIS_SECTIONS,
            RUBRIC_FINDINGS_KEY,
        )
        if sample.get(key) not in (None, "", [], {})
    }
    if sample.get("false_positive"):
        out["false_positive"] = True
    if int(sample.get("false_positive_pocs") or 0) > 0:
        out["false_positive_pocs"] = int(sample.get("false_positive_pocs") or 0)
    return out
