#!/usr/bin/env python3
"""Collect the upstream fix commit message for every sample lacking a description.

A description has to come from somewhere real. CyberGym supplies one for the 195
samples it contains; for the rest the maintainer already wrote an account of the
defect in the commit that repaired it -- "Push an error on sigalg mismatch in
X509_verify. It was failing but not pushing an error." That is evidence, not
invention, and it is available offline from the repository we already record.

Fetching is one shallow fetch of one commit per sample, run sequentially. The
messages are cached so the later rewrite into the shared register never has to
touch the network again, and so what each description was derived from stays
inspectable.

Some messages will not be usable -- merge commits, version bumps, "fix fuzzer
issue" -- and those samples are listed so they can be handled from the upstream
issue instead.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "gt_generation"))
from gt_status import classify  # noqa: E402

SELECTION = REPO_ROOT / "dataset" / "selected_1000.json"
GT_RESULTS = REPO_ROOT / "gt_results"
CYBERGYM_TASKS = REPO_ROOT / "external" / "cybergym_metadata" / "tasks.json"
CACHE = REPO_ROOT / "dataset" / "fix_commit_messages.json"

# A message that says nothing about a defect.
UNINFORMATIVE = re.compile(
    r"^\s*(merge\s|revert\s|bump\s|update\s+(deps|submodule|version)|"
    r"release\s+\d|version\s+\d|\[?ci\]?\s|roll\s)",
    re.I,
)


def cybergym_ids() -> set[str]:
    if not CYBERGYM_TASKS.is_file():
        return set()
    payload = json.loads(CYBERGYM_TASKS.read_text(encoding="utf-8"))
    tasks = list(payload.values()) if isinstance(payload, dict) else payload
    return {
        "arvo_" + str(t["task_id"]).split(":", 1)[1]
        for t in tasks
        if str(t.get("task_id", "")).startswith("arvo:")
        and str(t.get("vulnerability_description") or "").strip()
    }


def fetch_message(repo: str, commit: str, timeout: int = 60) -> str:
    """The maintainer's message for one commit, or empty when it cannot be had.

    A slow or unreachable remote must cost one sample, not the whole run: an
    unhandled fetch timeout ended the first pass at 150 of 373.
    """
    try:
        return _fetch_message(repo, commit, timeout)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return ""


def _fetch_message(repo: str, commit: str, timeout: int) -> str:
    with tempfile.TemporaryDirectory() as work:
        steps = [
            ["git", "init", "-q"],
            ["git", "remote", "add", "origin", repo],
            ["git", "fetch", "-q", "--depth", "1", "origin", commit],
        ]
        for step in steps:
            done = subprocess.run(step, cwd=work, capture_output=True,
                                  text=True, errors="replace", timeout=timeout)
            if done.returncode != 0:
                return ""
        shown = subprocess.run(
            ["git", "log", "-1", "--format=%B", "FETCH_HEAD"],
            cwd=work, capture_output=True, text=True, errors="replace", timeout=60,
        )
        return shown.stdout.strip() if shown.returncode == 0 else ""


def main() -> int:
    records = {r["sample_id"]: r for r in json.loads(SELECTION.read_text())}
    have = cybergym_ids()
    todo = [
        sid for sid in sorted(records)
        if classify(sid)[0] == "complete"
        and sid not in have
        and (GT_RESULTS / sid / "ground_truth.json").is_file()
        and records[sid].get("repo") and records[sid].get("fix_commit")
    ]
    cache = json.loads(CACHE.read_text()) if CACHE.is_file() else {}
    todo = [s for s in todo if s not in cache]
    print(f"{len(todo)} fix commit messages to fetch", flush=True)

    def one(sid: str) -> tuple[str, dict[str, object]]:
        record = records[sid]
        message = fetch_message(str(record["repo"]), str(record["fix_commit"]))
        return sid, {
            "repo": record["repo"],
            "fix_commit": record["fix_commit"],
            "message": message,
            "usable": bool(message) and not UNINFORMATIVE.match(message)
            and len(message.strip()) >= 30,
        }

    # Three at a time: these are network waits, and the box has other work.
    with ThreadPoolExecutor(max_workers=3) as pool:
        for index, (sid, entry) in enumerate(pool.map(one, todo), 1):
            cache[sid] = entry
            if index % 10 == 0 or index == len(todo):
                CACHE.write_text(
                    json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
                usable = sum(1 for v in cache.values() if v["usable"])
                print(f"  [{index}/{len(todo)}] cached {len(cache)}, usable {usable}",
                      flush=True)

    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    usable = [s for s, v in cache.items() if v["usable"]]
    empty = [s for s, v in cache.items() if not v["message"]]
    thin = [s for s, v in cache.items() if v["message"] and not v["usable"]]
    print(f"\ncached {len(cache)}: {len(usable)} usable, {len(thin)} uninformative, "
          f"{len(empty)} unfetchable")
    if thin[:5]:
        print("  uninformative examples:")
        for sid in thin[:5]:
            print(f"    {sid}: {cache[sid]['message'][:90]!r}")
    print(f"written: {CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
