"""The chronological train/test split, and the frozen files that pin it.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .defaults import BATCH_SIZE, REPO_ROOT, TEST_SIZE, TRAIN_SIZE, VALID_GT

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


def _counts(values: Iterable[str | None]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def _range(samples: list[str], dates: dict[str, str]) -> dict[str, str | None]:
    values = sorted(dates[s] for s in samples if s in dates)
    return {"first": values[0] if values else None, "last": values[-1] if values else None}
