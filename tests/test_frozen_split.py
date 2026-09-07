"""The split is pinned once and read back; it must partition the denominator exactly."""
import json

import pytest

from reward_framework.distillation.splits import (
    DATE_FIELDS,
    build_chronological_split,
    freeze_split,
    load_frozen_split,
)


def _corpus(tmp_path, dated_count=470, total=500):
    valid = tmp_path / "valid_gt.json"
    valid.write_text(json.dumps({"samples": [f"s{i}" for i in range(total)]}), encoding="utf-8")
    dates = tmp_path / "commit_dates.json"
    dates.write_text(json.dumps({"rows": [
        # Reverse the dates so a corpus-order split and a chronological one differ.
        {"sample_id": f"s{i}", "vulnerable_commit_date": f"20{40 - i // 25:02d}-01-01T00:00:{i % 60:02d}+00:00"}
        for i in range(dated_count)
    ]}), encoding="utf-8")
    return valid, dates


def _freeze(tmp_path, **kwargs):
    valid, dates = _corpus(tmp_path, **kwargs)
    split = build_chronological_split(valid, commit_dates=dates)
    report = freeze_split(split, train_gt=tmp_path / "train_gt.json", test_gt=tmp_path / "test_gt.json")
    return split, report


def test_frozen_files_partition_the_denominator_exactly(tmp_path):
    split, _ = _freeze(tmp_path)
    valid = set(json.loads((tmp_path / "valid_gt.json").read_text())["samples"])

    loaded = load_frozen_split(train_gt=tmp_path / "train_gt.json", test_gt=tmp_path / "test_gt.json")
    assert set(loaded.train) | set(loaded.test) == valid
    assert not set(loaded.train) & set(loaded.test), "a sample cannot be in both halves"
    assert len(loaded.train) + len(loaded.test) == len(valid)


def test_reload_reproduces_the_split_it_froze(tmp_path):
    split, _ = _freeze(tmp_path)
    loaded = load_frozen_split(train_gt=tmp_path / "train_gt.json", test_gt=tmp_path / "test_gt.json")

    assert loaded.train == split.train
    assert loaded.test == split.test
    assert loaded.batches == split.batches
    assert loaded.dates == split.dates
    assert loaded.date_sources == split.date_sources
    assert loaded.undated == split.undated


def test_training_half_is_ordered_and_fully_dated(tmp_path):
    split, _ = _freeze(tmp_path)
    loaded = load_frozen_split(train_gt=tmp_path / "train_gt.json", test_gt=tmp_path / "test_gt.json")

    values = [loaded.dates[s] for s in loaded.train]
    assert values == sorted(values), "the training order is what the chronology claims"
    assert all(s in loaded.dates for s in loaded.train), "no undated sample may reach training"
    assert max(loaded.dates[s] for s in loaded.train) <= min(
        loaded.dates[s] for s in loaded.test if s in loaded.dates
    )


def test_undated_samples_are_recorded_in_the_test_half(tmp_path):
    split, report = _freeze(tmp_path)
    payload = json.loads((tmp_path / "test_gt.json").read_text())

    assert payload["undated_samples"] == split.undated
    assert "no chronological meaning" in payload["undated_placement"]
    assert report["undated_in_test"] == len(split.undated)
    assert set(payload["undated_samples"]) <= set(payload["samples"])


def test_frozen_files_carry_their_own_provenance(tmp_path):
    _freeze(tmp_path)
    for half in ("train", "test"):
        payload = json.loads((tmp_path / f"{half}_gt.json").read_text())
        assert payload["schema_version"] == f"{half}-gt-v1"
        assert payload["date_field_priority"] == list(DATE_FIELDS)
        assert payload["partitions"].endswith("valid_gt.json")
        assert payload["total"] == len(payload["samples"])
        # "why is this sample here" must be answerable from the split alone.
        for sample in payload["samples"]:
            if sample in payload["sample_dates"]:
                assert payload["sample_date_sources"][sample] in DATE_FIELDS


def test_loading_without_a_frozen_split_says_what_to_run(tmp_path):
    with pytest.raises(FileNotFoundError, match="freeze-split"):
        load_frozen_split(train_gt=tmp_path / "nope.json", test_gt=tmp_path / "nope2.json")


def test_freezing_refuses_a_corpus_order_fallback(tmp_path):
    # Without dates the ordering is corpus order. Pinning that behind a filename
    # that claims chronology is the one mistake the freeze must not make.
    valid = tmp_path / "valid_gt.json"
    valid.write_text(json.dumps({"samples": [f"s{i}" for i in range(500)]}), encoding="utf-8")
    empty = tmp_path / "no_dates.json"
    empty.write_text(json.dumps({"rows": []}), encoding="utf-8")

    split = build_chronological_split(valid, commit_dates=empty)
    assert "not chronological" in split.ordering
    with pytest.raises(ValueError, match="refusing to freeze"):
        freeze_split(split, train_gt=tmp_path / "train_gt.json", test_gt=tmp_path / "test_gt.json")
    assert not (tmp_path / "train_gt.json").exists(), "nothing may be written on refusal"
