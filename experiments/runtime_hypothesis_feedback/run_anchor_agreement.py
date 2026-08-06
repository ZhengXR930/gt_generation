#!/usr/bin/env python3
"""E1: does an issue-derived reward guidance point at the GT causal chain?

The reward guidance is produced under the subject's information boundary -- the
public issue text and the vulnerable codebase, nothing else. The ground truth
was produced from the crash trace and the vulnerable/fixed differential, none of
which the guidance may see. That makes the GT a held-out reference: agreement
between the two is evidence that the issue text determines the causal chain,
and disagreement localises which stage it fails to determine.

This is the cheapest experiment that can refute the approach. It needs no probe
compiler and no runtime: if the guidance cannot name the function the defect is
introduced in, no amount of instrumentation engineering downstream will help.

Population and floor
--------------------
Descriptions come from dataset/issue_manifest/, one per sample, all in the same
register and all traceable to something a person wrote: CyberGym's own text, the
reporter's own words, or the maintainer's fix commit message restated. The
provenance is the stratum, because how much a description gives away depends on
who wrote it -- see the module docstring of scripts/build_issue_manifest.py.

The subject is given a natural-language vulnerability description, the way
CyberGym states one -- no Crash State, no fuzz target, no sanitizer job. Samples
whose issue_description is the OSS-Fuzz template are out of scope: stripping the
crash fields from one leaves boilerplate, not a description. The 318 samples
that already read as prose are the population, spanning all three tracks.

A prose issue still sometimes names a function outright ("An invalid memory
access occurs in the function ssh_buffer_unpack()"). The GT root cause is named
verbatim in 20% of these issues and the GT sink in 24%; in 73% neither is. The
unnamed group is the primary stratum, because agreement there cannot come from
string matching, and the named group is reported against its own floor.

Abstention is not error
-----------------------
A stage is `rewardable` only when independent drafts agreed on its anchors.
A guidance that declines to declare a stage has said "the issue does not
determine this", which is a different outcome from declaring it wrongly, and
the two are counted separately. Precision over declared stages and the rate of
declaration are both reported; neither alone describes the artifact.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for extra in (str(HERE), str(REPO_ROOT / "gt_generation")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from gt_status import classify  # noqa: E402
from reward_guidance import (  # noqa: E402
    build_guidance,
    rewardable,
    stage_points,
    stage_support,
)
MANIFEST = REPO_ROOT / "dataset" / "issue_manifest.json"
ISSUE_DIR = REPO_ROOT / "dataset" / "issue_manifest"

SELECTION = REPO_ROOT / "dataset" / "selected_1000.json"
GT_RESULTS = REPO_ROOT / "gt_results"

# Which GT anchor each guidance stage is answerable against. `propagation` has
# no single GT counterpart -- it is scored against the whole causal set below.
STAGE_TO_GT = {
    "admission": "parser_admitted",
    "root": "root_cause",
    "target": "sink",
}


def normalize_function(name: Any) -> str:
    """Bare identifier of a function, dropping C++ scope and any signature.

    GT records `QPngHandlerPrivate::readPngImage`, a crash state may print
    `readPngImage`, and a guidance may write either. Comparing the qualified
    names would score a correct answer as wrong.
    """
    text = re.split(r"[(<]", str(name or "").strip())[0]
    return text.split("::")[-1].strip()


# An issue written from an OSS-Fuzz report, rather than as a description.
OSSFUZZ_TEMPLATE = re.compile(
    r"Crash Type:|Crash State:|Job Type:|Fuzz target binary:"
)


def is_prose_issue(issue_text: str) -> bool:
    """Whether the issue reads as a description rather than a crash report."""
    return not OSSFUZZ_TEMPLATE.search(issue_text or "")


def names_function(issue_text: str, function: str) -> bool:
    """Whether the issue states this function name outright.

    This is the floor the guidance has to clear: reproducing a name the issue
    already gave is not evidence that the issue determined the causal chain.
    """
    if not function:
        return False
    return re.search(r"\b" + re.escape(function) + r"\b", issue_text or "") is not None


def gt_anchor(gt: dict[str, Any], key: str) -> dict[str, str]:
    if key == "parser_admitted":
        value = (gt.get("reachability_checkpoints") or {}).get("parser_admitted")
    else:
        value = gt.get(key)
    if not isinstance(value, dict):
        return {}
    return {
        "file": str(value.get("file") or ""),
        "function": normalize_function(value.get("function")),
    }


def gt_causal_functions(gt: dict[str, Any], result_dir: Path) -> set[str]:
    """Every function the GT places on the causal chain.

    Used for `propagation`, which names intermediate steps the GT does not
    record as a single anchor, and as a looser "somewhere on the chain" reading
    for the other stages.
    """
    found = set()
    for key in ("source", "root_cause", "sink"):
        anchor = gt_anchor(gt, key)
        if anchor.get("function"):
            found.add(anchor["function"])
    events = result_dir / "event_locations.json"
    if events.is_file():
        try:
            payload = json.loads(events.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
        entries = payload if isinstance(payload, list) else payload.get("events") or []
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("function"):
                found.add(normalize_function(entry["function"]))
    return {f for f in found if f}


def checkout(repo: str, commit: str, dest: Path, timeout: int = 1800) -> bool:
    """Fetch exactly the vulnerable commit, without the rest of the history."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    steps = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", repo],
        ["git", "fetch", "-q", "--depth", "1", "origin", commit],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    for step in steps:
        proc = subprocess.run(
            step, cwd=dest, capture_output=True, text=True,
            errors="replace", timeout=timeout,
        )
        if proc.returncode != 0:
            # Some servers refuse fetch-by-sha; fall back to a full clone.
            if step[1] == "fetch":
                shutil.rmtree(dest, ignore_errors=True)
                cloned = subprocess.run(
                    ["git", "clone", "-q", repo, str(dest)],
                    capture_output=True, text=True, errors="replace", timeout=timeout,
                )
                if cloned.returncode != 0:
                    return False
                out = subprocess.run(
                    ["git", "-C", str(dest), "checkout", "-q", commit],
                    capture_output=True, text=True, errors="replace", timeout=timeout,
                )
                return out.returncode == 0
            return False
    return True


def score(guidance: dict[str, Any], gt: dict[str, Any], result_dir: Path,
          issue_text: str) -> dict[str, Any]:
    """Per-stage agreement between the guidance anchors and the GT."""
    causal = gt_causal_functions(gt, result_dir)
    stages: dict[str, Any] = {}
    for stage in ("admission", "root", "propagation", "target"):
        points = stage_points(guidance, stage)
        functions = {normalize_function(p.get("function")) for p in points}
        functions.discard("")
        files = {Path(str(p.get("file") or "")).name for p in points}
        record: dict[str, Any] = {
            "support": stage_support(guidance, stage),
            "rewardable": rewardable(guidance, stage),
            "declared": bool(points),
            "anchor_functions": sorted(functions),
            "on_causal_chain": bool(functions & causal),
        }
        gt_key = STAGE_TO_GT.get(stage)
        if gt_key:
            anchor = gt_anchor(gt, gt_key)
            record["gt_function"] = anchor.get("function", "")
            record["gt_file"] = anchor.get("file", "")
            record["function_hit"] = bool(
                anchor.get("function") and anchor["function"] in functions
            )
            record["file_hit"] = bool(
                anchor.get("file") and Path(anchor["file"]).name in files
            )
            # The bar this stage has to clear by inference rather than copying.
            record["gt_named_in_issue"] = names_function(
                issue_text, anchor.get("function", "")
            )
        stages[stage] = record
    return stages


def select(limit: int, tracks: tuple[str, ...],
           origins: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Stratify by who wrote the description, which is what sets its floor."""
    records = {r["sample_id"]: r for r in json.loads(SELECTION.read_text())}
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.is_file() else {}
    chosen: list[dict[str, Any]] = []
    for sid in sorted(records):
        if tracks and not sid.startswith(tracks):
            continue
        record = records[sid]
        if classify(sid)[0] != "complete":
            continue
        if not (record.get("repo") and record.get("vulnerable_commit")):
            continue
        gt_path = GT_RESULTS / sid / "ground_truth.json"
        if not gt_path.is_file():
            continue
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        root = gt_anchor(gt, "root_cause").get("function")
        sink = gt_anchor(gt, "sink").get("function")
        if not root or not sink:
            continue
        entry = manifest.get(sid) or {}
        issue_text = str(entry.get("text") or "")
        if not issue_text.strip():
            continue
        chosen.append({
            "sample_id": sid,
            "stratum": str(entry.get("origin") or "unknown"),
            "names_gt_root": names_function(issue_text, root),
            "names_gt_sink": names_function(issue_text, sink),
            "root_equals_sink": root == sink,
            "repo": record["repo"], "commit": record["vulnerable_commit"],
        })

    # The two large provenances carry the result; the smaller ones are shown to
    # keep their own floors visible rather than folded into an average.
    order = origins or ("cybergym", "commit_derived", "reporter",
                        "crash_block_removed")
    if origins:
        # Validating one provenance at a time: give it the whole budget.
        buckets = {name: [c for c in chosen if c["stratum"] == name]
                   for name in order}
        out: list[dict[str, Any]] = []
        per = max(1, limit // len(order))
        for name in order:
            bucket = buckets[name]
            if len(bucket) > per:
                step = len(bucket) / per
                bucket = [bucket[int(i * step)] for i in range(per)]
            out.extend(bucket)
        return out[:limit]
    buckets = {name: [c for c in chosen if c["stratum"] == name] for name in order}
    quota = {"cybergym": limit * 2 // 5, "commit_derived": limit * 2 // 5,
             "reporter": limit // 10}
    quota["crash_block_removed"] = max(0, limit - sum(quota.values()))
    out: list[dict[str, Any]] = []
    for name in order:
        bucket = buckets[name]
        want = quota[name]
        if len(bucket) > want > 0:
            # Even stride, so ARVO's dense low ids do not crowd out SEC and OSV.
            step = len(bucket) / want
            bucket = [bucket[int(i * step)] for i in range(want)]
        out.extend(bucket[:want])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--tracks", default="arvo,secbench,osv")
    parser.add_argument("--out-dir", type=Path, default=HERE / "runs" / "e1_anchor_agreement")
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/e1_codebases"))
    parser.add_argument("--api-url", required=False)
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--model", default="gpt-5.4-2026-03-05")
    parser.add_argument("--samples", type=int, default=3,
                        help="independent draft+audit passes per subject")
    parser.add_argument("--origins", default="",
                        help="restrict to these manifest provenances")
    parser.add_argument("--plan-only", action="store_true",
                        help="print the stratified selection and exit")
    args = parser.parse_args(argv)

    tracks = tuple(t.strip() for t in args.tracks.split(",") if t.strip())
    origins = tuple(o.strip() for o in (args.origins or "").split(",") if o.strip())
    subjects = select(args.limit, tracks, origins)
    counts = Counter(s["stratum"] for s in subjects)
    print(f"selected {len(subjects)} subjects: {dict(counts)}")
    for subject in subjects:
        print(f"  {subject['stratum']:20s} {subject['sample_id']}")
    if args.plan_only:
        return 0

    import os
    api_key = os.environ.get(args.api_key_env or "", "")
    if not api_key or not args.api_url:
        print("\nmodel access is required to generate guidance: pass --api-url and "
              f"export {args.api_key_env}", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.workdir.mkdir(parents=True, exist_ok=True)
    results = []
    for index, subject in enumerate(subjects, 1):
        sid = subject["sample_id"]
        print(f"[{index}/{len(subjects)}] {sid} ({subject['stratum']})", flush=True)
        codebase = args.workdir / sid
        if not checkout(subject["repo"], subject["commit"], codebase):
            results.append({**subject, "error": "checkout failed"})
            print("    checkout failed", flush=True)
            continue

        issue_path = ISSUE_DIR / f"{sid}.txt"

        try:
            guidance = build_guidance(
                sample_id=sid, issue_path=issue_path, codebase=codebase,
                output_path=args.out_dir / "guidance" / f"{sid}.json",
                api_key=api_key, model=args.model, api_url=args.api_url,
                samples=args.samples,
            )
        except (ValueError, RuntimeError) as exc:
            results.append({**subject, "error": str(exc)[:300]})
            print(f"    guidance failed: {str(exc)[:160]}", flush=True)
            continue
        finally:
            # The checkout is large and the guidance records what it read.
            shutil.rmtree(codebase, ignore_errors=True)

        gt = json.loads((GT_RESULTS / sid / "ground_truth.json").read_text())
        scored = score(guidance, gt, GT_RESULTS / sid,
                       issue_path.read_text(encoding="utf-8"))
        results.append({**subject, "stages": scored})
        root = scored["root"]
        print(f"    root: declared={root['declared']} rewardable={root['rewardable']} "
              f"hit={root.get('function_hit')} (gt={root.get('gt_function')})", flush=True)

    report = args.out_dir / "results.json"
    report.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    summarize(results)
    print(f"\nwritten: {report}")
    return 0


def summarize(results: list[dict[str, Any]]) -> None:
    print("\n=== E1 anchor agreement ===")
    usable = [r for r in results if "stages" in r]
    errors = len(results) - len(usable)
    print(f"scored {len(usable)}, failed {errors}")
    for stratum in ("cybergym", "commit_derived", "reporter", "crash_block_removed"):
        rows = [r for r in usable if r["stratum"] == stratum]
        if not rows:
            continue
        print(f"\n{stratum}  n={len(rows)}")
        for stage in ("admission", "root", "propagation", "target"):
            declared = [r for r in rows if r["stages"][stage]["declared"]]
            reward = [r for r in rows if r["stages"][stage]["rewardable"]]
            hits = [r for r in reward if r["stages"][stage].get("function_hit")]
            chain = [r for r in reward if r["stages"][stage]["on_causal_chain"]]
            floor = [r for r in rows if r["stages"][stage].get("gt_named_in_issue")]
            line = (f"  {stage:12s} declared={len(declared):3d} rewardable={len(reward):3d}")
            if stage in STAGE_TO_GT:
                precision = f"{len(hits)/len(reward):.0%}" if reward else "n/a"
                line += (f" hit={len(hits):3d} ({precision} of rewardable)"
                         f" named-in-issue={len(floor)}")
            line += f" on-chain={len(chain)}"
            print(line)


if __name__ == "__main__":
    raise SystemExit(main())
