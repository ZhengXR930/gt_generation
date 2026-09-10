"""Sample-level success/failure pools for the Teacher.

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

BEHAVIOR_SECTIONS = (
    "search_behavior",
    "candidate_behavior",
    "feedback_behavior",
    "outcome_diagnosis",
)

EXCERPT_CHARS = 640

_FAILURE_OUTCOMES = {"failure", "partial"}
_SUCCESS_OUTCOMES = {"success"}
_INFRA_OUTCOMES = {"infrastructure"}


def empty_pools() -> dict[str, Any]:
    return {
        "protocol": "distillation-pools-v5",
        "batches_seen": [],
        "samples_by_batch": {},
        "failure_pool": [],
        "success_pool": [],
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
    for key in ("batches_seen", "samples_by_batch", "failure_pool", "success_pool", "infrastructure_pool"):
        if key in payload:
            upgraded[key] = payload[key]
    return upgraded


def save_pools(path: Path, pools: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pools, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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
        if outcome in _SUCCESS_OUTCOMES:
            pool_name = "success_pool"
        elif outcome in _FAILURE_OUTCOMES:
            pool_name = "failure_pool"
        elif outcome in _INFRA_OUTCOMES:
            pool_name = "infrastructure_pool"
        else:
            continue
        sample = _sample_record(record, outcome_record, batch_index)
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
    diagnoses from batches <= N.
    """
    _upgrade_in_place(pools)
    visible_failures = _visible_samples(pools.get("failure_pool") or [], through_batch)
    visible_successes = _visible_samples(pools.get("success_pool") or [], through_batch)
    failure_pool = _pool_view(visible_failures, limit=limit)
    success_pool = _pool_view(visible_successes, limit=limit)
    crash_pool = _pool_view(_false_positive_samples(visible_failures), limit=limit)
    infrastructure_pool = _pool_view(_visible_samples(pools.get("infrastructure_pool") or [], through_batch), limit=limit)
    return {
        "protocol": "teacher-pools-view-v5",
        "comparison_policy": {
            "mode": "success_failure_crash_contrast",
            "note": (
                "Infer reusable differences between successful and failed runs from the diagnosis fields. "
                "The crash_pool contains runs that produced a crash or submission-level success but did not "
                "trigger the GT vulnerability. Treat it as crash-producing behavior evidence, not as a veto: "
                "compare it with true successes to learn when crash pursuit helps and when it drifts. "
                "Do not turn any single diagnosis into a lesson without success/failure contrast."
            ),
        },
        "counts": {
            "failure_pool": len(failure_pool),
            "success_pool": len(success_pool),
            "crash_pool": len(crash_pool),
            "infrastructure_pool": len(infrastructure_pool),
            "batches_seen": len([b for b in pools.get("batches_seen") or [] if through_batch is None or int(b) <= through_batch]),
        },
        "failure_pool": failure_pool,
        "success_pool": success_pool,
        "crash_pool": crash_pool,
        "infrastructure_pool": infrastructure_pool,
    }



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
    visible_failures = _visible_samples(pools.get("failure_pool") or [], through_batch)
    crash_samples = _false_positive_samples(visible_failures)
    crash_ids = {sample.get("sample_id") for sample in crash_samples}
    plain_failures = [sample for sample in visible_failures if sample.get("sample_id") not in crash_ids]

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
        add("failure_recovery", visible_failures, max(0, failure_count))

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
    pools["protocol"] = "distillation-pools-v5"


def _forget_batch(pools: dict[str, Any], batch_index: int) -> None:
    previous = pools.setdefault("samples_by_batch", {}).pop(str(batch_index), [])
    previous_ids = {sample.get("sample_id") for sample in previous if sample.get("sample_id")}
    if previous_ids:
        for name in ("failure_pool", "success_pool", "infrastructure_pool"):
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
        **{section: _short_value(record.get(section)) for section in BEHAVIOR_SECTIONS},
        "retry_recommendation": _short_value(record.get("retry_recommendation")),
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
        if bool(sample.get("false_positive")) or int(sample.get("false_positive_pocs") or 0) > 0
    ]


def _pool_view(samples: list[dict[str, Any]], *, limit: int = 80) -> list[dict[str, Any]]:
    return [_teacher_sample(sample) for sample in samples[-limit:]]


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
            *BEHAVIOR_SECTIONS,
            "retry_recommendation",
        )
        if sample.get(key) not in (None, "")
    }
    if sample.get("false_positive"):
        out["false_positive"] = True
    if int(sample.get("false_positive_pocs") or 0) > 0:
        out["false_positive_pocs"] = int(sample.get("false_positive_pocs") or 0)
    return out
