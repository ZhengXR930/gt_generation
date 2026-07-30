#!/usr/bin/env python3
"""Build a conservative audit of new vulnerabilities with real local PoCs."""

from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTION = REPO_ROOT / "dataset" / "selected_1000.json"
REPORT = REPO_ROOT / "dataset" / "new_vulnerabilities_with_poc.json"
QUALIFIED_SELECTION = REPO_ROOT / "dataset" / "selected_new_memory_poc.json"
TESTCASE_RE = re.compile(r"testcase_id(?:=|%3[dD]|\\u003[dD])(\d+)|[?&]key=(\d+)")
ISSUE_RE = re.compile(r"(?:detail\?id=|issues/|OSS-Fuzz issue\s+)(\d+)")
CVE_RE = re.compile(r"CVE-\d{4}-\d+", re.IGNORECASE)
GHSA_RE = re.compile(r"GHSA-[0-9a-z-]+", re.IGNORECASE)
MEMORY_VULNERABILITY_CLASSES = {
    "CWE-120/121/122",  # classic, stack, and heap buffer overflow
    "CWE-125",          # out-of-bounds read
    "CWE-415",          # double free
    "CWE-416",          # use after free
    "CWE-457",          # use of uninitialized memory
    "CWE-590/763",      # invalid/mismatched deallocation
    "CWE-787",          # out-of-bounds write
}


def context(sample: dict) -> str:
    return " ".join(
        str(sample.get(key) or "")
        for key in (
            "sample_id",
            "benchmark_id",
            "public_id",
            "issue_description",
            "poc_source_url",
        )
    )


def testcase_ids(sample: dict) -> set[str]:
    return {left or right for left, right in TESTCASE_RE.findall(context(sample))}


def issue_ids(sample: dict) -> set[str]:
    return set(ISSUE_RE.findall(context(sample)))


def resolve_poc(sample: dict) -> Path | None:
    raw = sample.get("poc_path")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    candidates = [path] if path.is_absolute() else [
        REPO_ROOT / path,
        REPO_ROOT / "dataset" / path,
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def valid_poc(sample: dict) -> tuple[Path, bytes] | None:
    path = resolve_poc(sample)
    if path is None:
        return None
    raw = path.read_bytes()
    prefix = raw[:2048].lstrip().lower()
    if (
        not raw
        or prefix.startswith(b"<!doctype html")
        or prefix.startswith(b"<html")
        or b"<title>sign in" in prefix
    ):
        return None
    return path, raw


def crash_signature(sample: dict) -> str:
    title = str(sample.get("issue_description") or "").split(" OSS-Fuzz report:")[0]
    normalized = " ".join(title.lower().split())
    return f"{str(sample.get('project') or '').lower()}::{normalized}"


def public_vulnerability_key(sample: dict) -> str:
    text = context(sample)
    cves = CVE_RE.findall(text)
    if cves:
        return cves[0].upper()
    ghsas = GHSA_RE.findall(text)
    if ghsas:
        return ghsas[0].upper()
    return str(sample.get("public_id") or sample["sample_id"]).upper()


def artifact_record(sample: dict, path: Path, raw: bytes, **extra: object) -> dict:
    return {
        "sample_id": sample["sample_id"],
        "public_id": sample.get("public_id"),
        "project": sample.get("project"),
        "source_dataset": sample.get("source_dataset"),
        "poc_path": str(path.relative_to(REPO_ROOT)),
        "poc_size": len(raw),
        "poc_sha256": hashlib.sha256(raw).hexdigest(),
        **extra,
    }


def main() -> int:
    samples = json.loads(SELECTION.read_text(encoding="utf-8"))
    base = [sample for sample in samples if sample.get("selection_group") != "new_diverse"]
    new = [sample for sample in samples if sample.get("selection_group") == "new_diverse"]

    base_testcases: dict[str, list[str]] = collections.defaultdict(list)
    base_issues: dict[str, list[str]] = collections.defaultdict(list)
    base_commits: dict[str, list[str]] = collections.defaultdict(list)
    base_public_keys: dict[str, list[str]] = collections.defaultdict(list)
    for sample in base:
        for value in testcase_ids(sample):
            base_testcases[value].append(sample["sample_id"])
        for value in issue_ids(sample):
            base_issues[value].append(sample["sample_id"])
        for field in ("vulnerable_commit", "fix_commit"):
            if sample.get(field):
                base_commits[str(sample[field])].append(sample["sample_id"])
        base_public_keys[public_vulnerability_key(sample)].append(sample["sample_id"])

    oss_candidates: list[tuple[dict, Path, bytes]] = []
    rejected: list[dict] = []
    for sample in new:
        if sample.get("source_dataset") != "OSV.dev:OSS-Fuzz":
            continue
        if sample.get("vulnerability_class") not in MEMORY_VULNERABILITY_CLASSES:
            rejected.append(
                {
                    "sample_id": sample["sample_id"],
                    "reason": "memory_vulnerability_type_unconfirmed",
                    "vulnerability_class": sample.get("vulnerability_class"),
                }
            )
            continue
        artifact = valid_poc(sample)
        if artifact is None:
            rejected.append({"sample_id": sample["sample_id"], "reason": "no_real_local_poc"})
            continue
        overlaps: set[str] = set()
        for value in testcase_ids(sample):
            overlaps.update(base_testcases[value])
        for value in issue_ids(sample):
            overlaps.update(base_issues[value])
        for field in ("vulnerable_commit", "fix_commit"):
            value = str(sample.get(field) or "")
            overlaps.update(base_commits[value])
        if overlaps:
            rejected.append(
                {
                    "sample_id": sample["sample_id"],
                    "reason": "existing_benchmark_overlap",
                    "overlaps": sorted(overlaps),
                }
            )
            continue
        oss_candidates.append((sample, artifact[0], artifact[1]))

    # Same project and same sanitizer crash title is conservatively treated as
    # one vulnerability even when OSS-Fuzz assigned multiple report IDs.
    by_signature: dict[str, list[tuple[dict, Path, bytes]]] = collections.defaultdict(list)
    for candidate in oss_candidates:
        by_signature[crash_signature(candidate[0])].append(candidate)
    qualified_oss: list[dict] = []
    for signature, group in sorted(by_signature.items()):
        group.sort(key=lambda item: item[0]["sample_id"])
        chosen = group[0]
        qualified_oss.append(
            artifact_record(
                chosen[0],
                chosen[1],
                chosen[2],
                uniqueness_key=signature,
                collapsed_report_ids=[item[0]["sample_id"] for item in group[1:]],
            )
        )

    local_candidates: list[tuple[dict, Path, bytes]] = []
    for sample in new:
        if sample.get("source_dataset") == "OSV.dev:OSS-Fuzz":
            continue
        if sample.get("vulnerability_class") not in MEMORY_VULNERABILITY_CLASSES:
            rejected.append(
                {
                    "sample_id": sample["sample_id"],
                    "reason": "memory_vulnerability_type_unconfirmed",
                    "vulnerability_class": sample.get("vulnerability_class"),
                }
            )
            continue
        artifact = valid_poc(sample)
        if artifact is None:
            continue
        key = public_vulnerability_key(sample)
        overlaps = set(base_public_keys[key])
        for field in ("vulnerable_commit", "fix_commit"):
            overlaps.update(base_commits[str(sample.get(field) or "")])
        if overlaps:
            rejected.append(
                {
                    "sample_id": sample["sample_id"],
                    "reason": "existing_benchmark_overlap",
                    "overlaps": sorted(overlaps),
                }
            )
            continue
        local_candidates.append((sample, artifact[0], artifact[1]))

    by_public_key: dict[str, list[tuple[dict, Path, bytes]]] = collections.defaultdict(list)
    for candidate in local_candidates:
        by_public_key[public_vulnerability_key(candidate[0])].append(candidate)
    qualified_local: list[dict] = []
    for key, group in sorted(by_public_key.items()):
        group.sort(key=lambda item: item[0]["sample_id"])
        chosen = group[0]
        qualified_local.append(
            artifact_record(
                chosen[0],
                chosen[1],
                chosen[2],
                uniqueness_key=key,
                collapsed_report_ids=[item[0]["sample_id"] for item in group[1:]],
            )
        )

    qualified = qualified_oss + qualified_local
    qualified_ids = {record["sample_id"] for record in qualified}
    qualified_samples = [
        sample for sample in samples if sample.get("sample_id") in qualified_ids
    ]
    QUALIFIED_SELECTION.write_text(
        json.dumps(qualified_samples, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    report = {
        "policy": {
            "poc_required": "nonempty local artifact; HTML/report snapshots rejected",
            "memory_vulnerability_required": sorted(MEMORY_VULNERABILITY_CLASSES),
            "benchmark_overlap": "testcase ID, OSS-Fuzz issue ID, commit, or public vulnerability ID",
            "oss_fuzz_dedup": "same project plus same crash title collapsed",
            "non_oss_dedup": "same CVE/GHSA/public ID collapsed",
            "reproduction_note": "local PoC presence is not equivalent to successful vulnerable-build reproduction",
        },
        "counts": {
            "qualified_unique_new_vulnerabilities_with_local_poc": len(qualified),
            "qualified_oss_fuzz": len(qualified_oss),
            "qualified_other_public_sources": len(qualified_local),
            "rejected": len(rejected),
        },
        "qualified_selection": str(QUALIFIED_SELECTION.relative_to(REPO_ROOT)),
        "qualified": qualified,
        "rejected": sorted(rejected, key=lambda item: item["sample_id"]),
    }
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
