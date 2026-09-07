#!/usr/bin/env python3
"""Persist reward-framework submission history.

This helper records mechanical submission state only. It does not judge whether
a candidate has semantic evidence gain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path | None) -> Any:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def history_path(value: Path | None) -> Path:
    return value or _default_history()


def cmd_record(args: argparse.Namespace) -> int:
    candidate = Path(args.candidate)
    analysis = Path(args.analysis) if args.analysis else None
    result_json = Path(args.result_json) if args.result_json else None
    record = {
        "protocol": "reward-submit-history-v1",
        "attempt_id": args.attempt_id or uuid.uuid4().hex,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "candidate_path": str(candidate),
        "candidate_sha256": sha256_file(candidate),
        "candidate_size": candidate.stat().st_size if candidate.is_file() else None,
        "analysis_path": str(analysis) if analysis else None,
        "analysis_sha256": sha256_file(analysis),
        "result_path": str(result_json) if result_json else None,
        "result": load_json(result_json),
        "status": args.status,
        "note": args.note,
    }
    path = history_path(args.history_jsonl)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False))
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    path = history_path(args.history_jsonl)
    rows: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    summary = {
        "protocol": "reward-submit-history-summary-v1",
        "history_path": str(path),
        "attempts": len(rows),
        "candidate_hashes": [row.get("candidate_sha256") for row in rows],
        "statuses": [row.get("status") for row in rows],
        "latest": rows[-1] if rows else None,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0



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
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--candidate", required=True)
    record.add_argument("--analysis")
    record.add_argument("--result-json")
    record.add_argument("--history-jsonl", type=Path)
    record.add_argument("--attempt-id")
    record.add_argument("--status", default="unknown")
    record.add_argument("--note", default="")
    record.set_defaults(func=cmd_record)

    summarize = sub.add_parser("summarize")
    summarize.add_argument("--history-jsonl", type=Path)
    summarize.add_argument("--out", type=Path)
    summarize.set_defaults(func=cmd_summarize)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
