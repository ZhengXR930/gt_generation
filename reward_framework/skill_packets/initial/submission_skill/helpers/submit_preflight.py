#!/usr/bin/env python3
"""Mechanical candidate preflight for reward-framework submissions.

This helper intentionally does not decide whether a candidate has semantic
evidence gain. It only reports file presence, candidate hashes, exact duplicate
status, near-duplicate byte similarity, and a small structural summary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def similarity_prefix(a: bytes, b: bytes) -> float:
    if not a and not b:
        return 1.0
    limit = min(len(a), len(b), 4096)
    if limit == 0:
        return 0.0
    same = sum(1 for i in range(limit) if a[i] == b[i])
    length_penalty = min(len(a), len(b)) / max(len(a), len(b)) if max(len(a), len(b)) else 1.0
    return (same / limit) * length_penalty


def summarize_bytes(data: bytes) -> dict[str, Any]:
    printable = sum(32 <= b <= 126 or b in (9, 10, 13) for b in data)
    return {
        "size": len(data),
        "sha256": sha256_bytes(data),
        "first_32_hex": data[:32].hex(),
        "last_32_hex": data[-32:].hex() if data else "",
        "nul_bytes": data.count(0),
        "newline_bytes": data.count(10),
        "printable_ratio": printable / len(data) if data else None,
    }



def _default_history() -> Path:
    """State lives beside the agent's working directory, not at an absolute path.

    The four harnesses stage the workspace in different places, so the location
    comes from the environment the adapter set, falling back to a path relative
    to the agent's own working directory.
    """
    import os

    state = os.environ.get("REWARD_FRAMEWORK_STATE_DIR", ".poc_skill_state")
    return Path(state) / "submit_history.jsonl"

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--history-jsonl", type=Path, default=_default_history())
    parser.add_argument("--analysis", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    report: dict[str, Any] = {
        "protocol": "reward-submit-preflight-v1",
        "candidate": str(args.candidate),
        "candidate_exists": args.candidate.is_file(),
        "analysis": str(args.analysis) if args.analysis else None,
        "analysis_exists": args.analysis.is_file() if args.analysis else None,
        "blocking_errors": [],
        "warnings": [],
    }
    if not args.candidate.is_file():
        report["blocking_errors"].append("candidate_missing")
    else:
        data = args.candidate.read_bytes()
        report["summary"] = summarize_bytes(data)
        history = load_history(args.history_jsonl)
        current_hash = report["summary"]["sha256"]
        exact = [row for row in history if row.get("candidate_sha256") == current_hash]
        if exact:
            report["warnings"].append("exact_duplicate_candidate")
            report["exact_duplicate_attempts"] = [row.get("attempt_id") for row in exact]
        near = []
        for row in history[-20:]:
            prior_path = Path(str(row.get("candidate_path") or ""))
            if prior_path.is_file():
                score = similarity_prefix(data, prior_path.read_bytes())
                if score >= 0.95 and row.get("candidate_sha256") != current_hash:
                    near.append({"attempt_id": row.get("attempt_id"), "prefix_similarity": round(score, 4)})
        if near:
            report["warnings"].append("near_duplicate_candidate")
            report["near_duplicates"] = near

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 2 if report["blocking_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
