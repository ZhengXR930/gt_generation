"""Utilities for legacy chronological splits and frozen split files.

The split is ordered by the committer date of each sample's ``vulnerable_commit``
- the state of the repository the agent is actually handed. Samples whose date
could not be resolved are appended after every dated one, which places them in
the held-out tail; their position there carries no chronological meaning.

The ordering is computed **once** and frozen into ``gt_results/train_gt.json``
and ``gt_results/test_gt.json``, next to the ``valid_gt.json`` denominator they
partition. Everything downstream reads those files. That matters because a split
recomputed per run would silently change the moment another date is resolved,
and results either side of that change would no longer be comparable.

The frozen files carry each sample's date and which field it came from, so the
question "why is this sample in train" is answerable from the split alone.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .defaults import BATCH_SIZE, REPO_ROOT, TEST_SIZE, TRAIN_SIZE, VALID_GT
from poc_generation.gt_trigger_oracle import (
    gt_trigger_state,
    gt_triggered,
    reachability_summary_candidate,
    complete_manifest_reachability_summary,
    complete_reachability_summary,
    enrich_submission_oracle,
)

GT_RESULTS = REPO_ROOT / "gt_results"
TRAIN_GT = GT_RESULTS / "train_gt.json"
TEST_GT = GT_RESULTS / "test_gt.json"
DEFAULT_COMMIT_DATES = REPO_ROOT / "dataset" / "commit_dates.json"

# Priority order. The vulnerable commit is the state the agent is handed, so it
# is the axis of record; the fix commit is days away for OSS-Fuzz samples and
# stands in when the vulnerable one could not be resolved. The local ARVO patch
# carries the same fix-commit date and covers repositories unreachable from here.
DATE_FIELDS = ("vulnerable_commit_date", "fix_commit_date", "patch_commit_date")

ORDERING = "vulnerable_commit committer date, ascending; undated samples appended in corpus order"
UNDATED_PLACEMENT = (
    "appended after every dated sample, which places them in the held-out test tail; "
    "their position within the test set carries no chronological meaning"
)


@dataclass(frozen=True)
class ChronologicalSplit:
    train: list[str]
    test: list[str]
    batches: list[list[str]]
    ordering: str = ORDERING
    dates: dict[str, str] = field(default_factory=dict)
    date_sources: dict[str, str] = field(default_factory=dict)
    undated: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "protocol": "reward-skill-distillation-split-v3",
            "ordering": self.ordering,
            "date_field_priority": list(DATE_FIELDS),
            "train_size": len(self.train),
            "test_size": len(self.test),
            "batch_size": BATCH_SIZE,
            "undated_samples": self.undated,
            "undated_placement": UNDATED_PLACEMENT,
            "batches": [
                {"batch_index": index, "samples": batch}
                for index, batch in enumerate(self.batches)
            ],
            "train": self.train,
            "test": self.test,
            "sample_dates": self.dates,
            "sample_date_sources": self.date_sources,
            "date_source_counts": _counts(self.date_sources.values()),
            "train_date_range": _range(self.train, self.dates),
            "test_date_range": _range(self.test, self.dates),
        }


# --------------------------------------------------------------------------- frozen split


def load_frozen_split(*, train_gt: Path = TRAIN_GT, test_gt: Path = TEST_GT,
                      batch_size: int = BATCH_SIZE) -> ChronologicalSplit:
    """Read the pinned split. Batches follow from the frozen training order."""
    for path in (train_gt, test_gt):
        if not path.is_file():
            raise FileNotFoundError(
                f"missing frozen split file {path}; run "
                "`python -m reward_framework.distillation.cli freeze-split` once"
            )
    train_payload = json.loads(train_gt.read_text(encoding="utf-8"))
    test_payload = json.loads(test_gt.read_text(encoding="utf-8"))
    train = [str(item) for item in train_payload.get("samples") or []]
    test = [str(item) for item in test_payload.get("samples") or []]

    dates, sources = {}, {}
    for payload in (train_payload, test_payload):
        dates.update(payload.get("sample_dates") or {})
        sources.update(payload.get("sample_date_sources") or {})
    return ChronologicalSplit(
        train=train,
        test=test,
        batches=[train[i:i + batch_size] for i in range(0, len(train), batch_size)],
        ordering=str(train_payload.get("ordering") or ORDERING),
        dates=dates,
        date_sources=sources,
        undated=[str(item) for item in test_payload.get("undated_samples") or []],
    )


def freeze_split(split: ChronologicalSplit, *, train_gt: Path = TRAIN_GT, test_gt: Path = TEST_GT) -> dict[str, Any]:
    """Pin the split next to the denominator it partitions.

    Refuses to pin a corpus-order fallback. Without dates the ordering is not
    chronological, and freezing it would bake that in behind a filename that
    claims otherwise.
    """
    if not split.dates:
        raise ValueError(
            "refusing to freeze a split with no commit dates: the ordering would be "
            "corpus order, not chronological. Restore dataset/commit_dates.json first."
        )
    stamp = datetime.now(timezone.utc).isoformat()
    for path, half, samples, extra in (
        (train_gt, "train", split.train, {}),
        (test_gt, "test", split.test, {"undated_samples": split.undated, "undated_placement": UNDATED_PLACEMENT}),
    ):
        payload = {
            "schema_version": f"{half}-gt-v1",
            "generated_at": stamp,
            "description": f"{half.capitalize()} half of the frozen chronological split of valid_gt.json.",
            "ordering": split.ordering,
            "date_field_priority": list(DATE_FIELDS),
            "partitions": str(VALID_GT.relative_to(REPO_ROOT)),
            "total": len(samples),
            **extra,
            "date_range": _range(samples, split.dates),
            "date_source_counts": _counts(split.date_sources.get(s) for s in samples if s in split.date_sources),
            "samples": samples,
            "sample_dates": {s: split.dates[s] for s in samples if s in split.dates},
            "sample_date_sources": {s: split.date_sources[s] for s in samples if s in split.date_sources},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "train_gt": str(train_gt),
        "test_gt": str(test_gt),
        "train": len(split.train),
        "test": len(split.test),
        "undated_in_test": len(split.undated),
        "date_source_counts": _counts(split.date_sources.values()),
        "train_date_range": _range(split.train, split.dates),
        "test_date_range": _range(split.test, split.dates),
    }


# --------------------------------------------------------------------------- one-off ordering


def load_commit_dates(path: Path | None = None) -> dict[str, tuple[str, str]]:
    """sample_id -> (ISO date, which field it came from).

    The field is carried alongside the date because the three sources are not the
    same axis: the vulnerable commit is where the agent's workspace is staged,
    while the fix commit and the local patch sit a few days later.
    """
    source = path or DEFAULT_COMMIT_DATES
    if not source.is_file():
        return {}
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    dates: dict[str, tuple[str, str]] = {}
    for row in payload.get("rows") or []:
        if not isinstance(row, dict) or not row.get("sample_id"):
            continue
        chosen = next((name for name in DATE_FIELDS if row.get(name)), None)
        if chosen:
            dates[str(row["sample_id"])] = (str(row[chosen]), chosen)
    return dates


def build_chronological_split(
    valid_gt: Path = VALID_GT,
    *,
    train_size: int = TRAIN_SIZE,
    test_size: int = TEST_SIZE,
    batch_size: int = BATCH_SIZE,
    commit_dates: Path | None = None,
    require_dates: bool = False,
) -> ChronologicalSplit:
    """Order the corpus by commit date. Used once, by ``freeze-split``."""
    payload = json.loads(valid_gt.read_text(encoding="utf-8"))
    samples = [str(item).strip() for item in payload.get("samples", []) if str(item).strip()]
    required = train_size + test_size
    if len(samples) < required:
        raise ValueError(f"valid_gt has {len(samples)} samples, need at least {required}")

    dates = load_commit_dates(commit_dates)
    ordered, ordering = _chronological_order(samples, dates)
    used = ordered[:required]
    train = used[:train_size]
    undated = [sample for sample in used if sample not in dates]
    # Undated samples sit at the tail, which puts them in the held-out set. That
    # is a deliberate placement, so it is not an error. A *training* sample with
    # no date would be, because the batch order is what the chronology claims.
    undated_train = [sample for sample in train if sample not in dates]
    if require_dates and undated_train:
        raise ValueError(
            f"{len(undated_train)} training samples have no commit date in "
            "dataset/commit_dates.json, so the batch order is not chronological"
        )
    return ChronologicalSplit(
        train=train,
        test=used[train_size:],
        batches=[train[i:i + batch_size] for i in range(0, len(train), batch_size)],
        ordering=ordering,
        dates={sample: dates[sample][0] for sample in used if sample in dates},
        date_sources={sample: dates[sample][1] for sample in used if sample in dates},
        undated=undated,
    )


def _chronological_order(samples: list[str], dates: dict[str, tuple[str, str]]) -> tuple[list[str], str]:
    """Dated samples oldest first, then whatever has no date, in corpus order."""
    if not dates:
        return samples, "NO COMMIT DATES AVAILABLE: falling back to corpus order, which is not chronological"
    position = {sample: index for index, sample in enumerate(samples)}
    dated = sorted((s for s in samples if s in dates), key=lambda s: (dates[s][0], position[s]))
    return dated + [s for s in samples if s not in dates], ORDERING


def write_split_manifest(out: Path, split: ChronologicalSplit) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(split.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- outcome-balanced training batches


_NON_EVALUATED_STATUSES = frozenset(
    {"infrastructure_failed", "runtime_spec_unavailable", "not_executed"}
)


def _candidate_is_evaluated(candidate: dict[str, Any]) -> bool:
    return (
        not candidate.get("error")
        and candidate.get("execution_status") not in _NON_EVALUATED_STATUSES
    )


def read_baseline_outcomes(
    results_dir: Path,
    sample_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Read sample-level GT outcome labels from saved reachability artifacts.

    A sample is a baseline ``success`` when at least one evaluable candidate
    has an explicit GT sanitizer trigger.  Everything else is ``failure``;
    infrastructure/error rows are not allowed to create a false success or FP
    label.  This is deliberately independent of the R1--R4 checkpoint ladder,
    so samples without checkpoints can still participate when their saved GT
    result is present.
    """
    rows: dict[str, dict[str, Any]] = {}
    for raw_sample_id in sample_ids:
        sample_id = str(raw_sample_id)
        path = results_dir / sample_id / "reachability_eval.json"
        if not path.is_file():
            raise FileNotFoundError(f"missing reachability evaluation for {sample_id}: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid reachability evaluation for {sample_id}: {path}") from exc
        manifest_path = results_dir / sample_id / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = None
        payload = enrich_submission_oracle(payload, manifest) or payload
        candidates = payload.get("candidates") or []
        evaluated = [item for item in candidates if isinstance(item, dict) and _candidate_is_evaluated(item)]
        # PoC bytes may be cleaned after evaluation.  In that case a later
        # reachability_eval.json contains only missing-file errors, while the
        # manifest retains the complete evaluator summary.  Reuse that saved
        # summary instead of turning a known run into a false failure.
        if not evaluated:
            saved_summary = complete_reachability_summary(payload)
            if saved_summary is None:
                saved_summary = complete_manifest_reachability_summary(manifest or {})
            if isinstance(saved_summary, dict):
                evaluated = [reachability_summary_candidate(saved_summary)]
        gt_triggered_count = sum(gt_triggered(item) for item in evaluated)
        fp_candidates = sum(
            gt_trigger_state(item) is False
            and item.get("original_exit_code") not in (None, 0, 300)
            for item in evaluated
        )
        rows[sample_id] = {
            "outcome": "success" if gt_triggered_count else "failure",
            "gt_triggered_pocs": gt_triggered_count,
            "fp_triggered_pocs": fp_candidates,
            "evaluated_unique_pocs": len(evaluated),
            "source": str(path),
            "reachability_source": "manifest.reachability.summary" if len(evaluated) == 1 and evaluated[0].get("execution_status") == "stored_evaluator_summary" else str(path),
        }
    return rows


def rebalance_training_batches(
    train_samples: Iterable[str],
    outcomes: dict[str, dict[str, Any]],
    *,
    batch_size: int = BATCH_SIZE,
    target_success_per_batch: int = 6,
    seed: str = "reward-skill-distillation-rebalanced-20261022",
) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """Partition a fixed train roster into deterministic outcome-balanced batches.

    The sample roster is not changed.  The requested 6-success/4-failure
    target is applied as evenly as possible; if live labels drift (for example
    61 successes and 39 failures), the unavoidable remainder is recorded as a
    single 7/3 batch rather than silently dropping or replacing a sample.
    """
    samples = [str(item) for item in train_samples]
    if not samples or len(samples) % batch_size:
        raise ValueError("train roster must be non-empty and divisible by batch_size")
    missing = [sample for sample in samples if sample not in outcomes]
    if missing:
        raise ValueError(f"missing outcome labels for: {', '.join(missing)}")
    if len(set(samples)) != len(samples):
        raise ValueError("train roster contains duplicate sample IDs")

    batch_count = len(samples) // batch_size
    success = [sample for sample in samples if outcomes[sample].get("outcome") == "success"]
    failure = [sample for sample in samples if outcomes[sample].get("outcome") == "failure"]
    unknown = [sample for sample in samples if outcomes[sample].get("outcome") not in {"success", "failure"}]
    if unknown:
        raise ValueError(f"unknown outcome labels for: {', '.join(unknown)}")

    def seeded_order(values: list[str], label: str) -> list[str]:
        return sorted(
            values,
            key=lambda sample: hashlib.sha256(f"{seed}|{label}|{sample}".encode("utf-8")).hexdigest(),
        )

    success = seeded_order(success, "success")
    failure = seeded_order(failure, "failure")
    target = max(0, min(batch_size, int(target_success_per_batch)))
    success_quotas = [target] * batch_count
    delta = len(success) - sum(success_quotas)
    if delta > 0:
        for index in range(delta):
            success_quotas[index % batch_count] += 1
    elif delta < 0:
        for index in range(-delta):
            slot = index % batch_count
            if success_quotas[slot] <= 0:
                raise ValueError("cannot distribute the live success count across batches")
            success_quotas[slot] -= 1

    batches: list[list[str]] = []
    counts: list[dict[str, Any]] = []
    success_offset = failure_offset = 0
    for batch_index, success_quota in enumerate(success_quotas):
        failure_quota = batch_size - success_quota
        selected_success = success[success_offset:success_offset + success_quota]
        selected_failure = failure[failure_offset:failure_offset + failure_quota]
        success_offset += success_quota
        failure_offset += failure_quota
        if len(selected_success) != success_quota or len(selected_failure) != failure_quota:
            raise ValueError("outcome quotas exceed the available labeled samples")
        batch: list[str] = []
        for index in range(max(success_quota, failure_quota)):
            if index < success_quota:
                batch.append(selected_success[index])
            if index < failure_quota:
                batch.append(selected_failure[index])
        batches.append(batch)
        counts.append({
            "batch_index": batch_index,
            "size": len(batch),
            "success": success_quota,
            "failure": failure_quota,
            "samples": batch,
        })
    if success_offset != len(success) or failure_offset != len(failure):
        raise ValueError("not all labeled samples were assigned to a batch")
    return batches, counts


def validate_outcome_balanced_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the recorded baseline labels against every training batch.

    Legacy manifests without ``baseline_outcomes`` remain readable for old
    runs. New manifests fail fast if a batch roster and its recorded outcome
    counts disagree.
    """
    outcomes = payload.get("baseline_outcomes")
    batches = payload.get("batches")
    if not isinstance(outcomes, dict) or not isinstance(batches, list):
        return {"validated": False, "reason": "legacy_manifest_without_outcome_labels"}
    seen: list[str] = []
    counts: list[dict[str, int]] = []
    recorded_counts = payload.get("train_batch_outcome_counts") or []
    for index, item in enumerate(batches):
        samples = [str(sample) for sample in (item.get("samples") or [])]
        success = sum(outcomes.get(sample, {}).get("outcome") == "success" for sample in samples)
        failure = sum(outcomes.get(sample, {}).get("outcome") == "failure" for sample in samples)
        if success + failure != len(samples):
            raise ValueError(f"batch {index} contains an unlabeled sample")
        if index >= len(recorded_counts):
            raise ValueError("train_batch_outcome_counts is incomplete")
        recorded = recorded_counts[index]
        if int(recorded.get("success", -1)) != success or int(recorded.get("failure", -1)) != failure:
            raise ValueError(f"batch {index} outcome counts do not match its roster")
        seen.extend(samples)
        counts.append({"batch_index": index, "size": len(samples), "success": success, "failure": failure})
    if len(seen) != len(set(seen)):
        raise ValueError("training batches contain duplicate sample IDs")
    if set(seen) != set(str(item) for item in payload.get("train") or []):
        raise ValueError("training batches do not cover the recorded train roster")
    return {"validated": True, "batches": counts}


def _counts(values: Iterable[str | None]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def _range(samples: list[str], dates: dict[str, str]) -> dict[str, str | None]:
    values = sorted(dates[s] for s in samples if s in dates)
    return {"first": values[0] if values else None, "last": values[-1] if values else None}
