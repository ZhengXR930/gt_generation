#!/usr/bin/env python3
"""Conservatively correct GT line numbers against vulnerable git source.

The script materializes one sparse vulnerable checkout at a time, searches for
each GT code snippet in the claimed file, and updates line/code only when the
snippet has a unique match. It does not change trace semantics.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], *, timeout: int | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout


def checked(cmd: list[str], *, timeout: int | None = None) -> str:
    code, out = run(cmd, timeout=timeout)
    if code != 0:
        raise RuntimeError(f"command failed ({code}): {' '.join(cmd)}\n{out[-4000:]}")
    return out


def norm_path(path: str | None) -> str:
    if not path:
        return ""
    p = path.replace("\\", "/")
    for marker in ("/src/", "/build_sanitizer/", "/build_debug/", "/build_valgrind/"):
        if marker in p:
            p = p.split(marker, 1)[1]
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p.strip("/")


def compact_code(text: str) -> str:
    text = re.sub(r"//.*", "", text or "")
    return re.sub(r"\s+", " ", text).strip().rstrip(";")


def normalize_repo_url(url: str) -> str:
    if url.startswith("git://git.gnupg.org/gnupg.git"):
        return "https://github.com/gpg/gnupg.git"
    if url.startswith("git://"):
        return "https://" + url[len("git://") :]
    if url.startswith("http://anongit.freedesktop.org/"):
        return "https://gitlab.freedesktop.org/poppler/poppler.git"
    if url.startswith("https://anongit.freedesktop.org/git/poppler/poppler.git"):
        return "https://gitlab.freedesktop.org/poppler/poppler.git"
    return url


def location_path_variants(gt: dict[str, Any]) -> list[str]:
    variants: set[str] = set()
    for loc in collect_locations(gt):
        p = norm_path(str(loc.get("file")))
        if not p:
            continue
        parts = p.split("/")
        for i in range(0, min(4, len(parts))):
            variants.add("/".join(parts[i:]))
    return sorted(v for v in variants if v)


def materialize_src(record: dict[str, Any], gt: dict[str, Any], dst: Path) -> None:
    repo = normalize_repo_url(str(record.get("repo") or record.get("repo_url") or ""))
    commit = str(record.get("vulnerable_commit") or "")
    if not repo or not commit:
        raise RuntimeError("record lacks repo or vulnerable_commit")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    checked(["git", "init", "-q", str(dst)], timeout=120)
    checked(["git", "-C", str(dst), "remote", "add", "origin", repo], timeout=120)
    checked(["git", "-C", str(dst), "config", "advice.detachedHead", "false"], timeout=120)
    checked(["git", "-C", str(dst), "sparse-checkout", "init", "--no-cone"], timeout=120)
    variants = location_path_variants(gt)
    if variants:
        checked(["git", "-C", str(dst), "sparse-checkout", "set", *variants], timeout=120)
    code, out = run(["git", "-C", str(dst), "fetch", "--depth=1", "--filter=blob:none", "origin", commit], timeout=900)
    if code != 0:
        code, out = run(["git", "-C", str(dst), "fetch", "--depth=1", "origin", commit], timeout=900)
    if code != 0:
        raise RuntimeError(f"git fetch failed for {repo} {commit}\n{out[-4000:]}")
    checked(["git", "-C", str(dst), "checkout", "-q", "FETCH_HEAD"], timeout=300)


def find_source_file(src_root: Path, claimed: str) -> Path | None:
    claimed = norm_path(claimed)
    direct = src_root / claimed
    if direct.exists():
        return direct
    parts = claimed.split("/")
    candidates = []
    for i in range(1, len(parts)):
        direct = src_root / Path(*parts[i:])
        if direct.exists():
            candidates.append(direct)
    if candidates:
        return sorted(candidates, key=lambda p: len(str(p)))[0]
    name = parts[-1] if parts else claimed
    matches = [p for p in src_root.rglob(name) if p.is_file()]
    suffix_matches = [p for p in matches if str(p).replace("\\", "/").endswith(claimed)]
    if suffix_matches:
        return sorted(suffix_matches, key=lambda p: len(str(p)))[0]
    if len(matches) == 1:
        return matches[0]
    return None


def collect_locations(gt: dict[str, Any]) -> list[dict[str, Any]]:
    locs: list[dict[str, Any]] = []
    for name in ("source", "sink", "root_cause", "taint_source", "tainted_value_origin"):
        obj = gt.get(name)
        if isinstance(obj, dict) and obj.get("file") and obj.get("line"):
            locs.append({"kind": name, "obj": obj, "file": obj.get("file"), "line": obj.get("line"), "function": obj.get("function"), "code": obj.get("code")})
    for step in gt.get("fine_trace", []):
        if isinstance(step, dict) and step.get("file") and step.get("line"):
            locs.append({"kind": f"fine_trace[{step.get('step')}]", "obj": step, "file": step.get("file"), "line": step.get("line"), "function": step.get("function"), "code": step.get("code")})
    return locs


def unique_code_match(path: Path, code: str, old_line: int | None = None) -> tuple[int, str] | None:
    needle = compact_code(code)
    if len(needle) < 8:
        return None
    # Match compiler/sanitizer/editor line numbering: only newline bytes count
    # as line breaks.  Some C/C++ sources contain form-feed page separators;
    # splitlines() would count them and shift every following source location.
    lines = path.read_text(errors="replace").split("\n")
    matches: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines, start=1):
        hay = compact_code(line)
        if not hay:
            continue
        if needle in hay or (len(hay) >= 12 and hay in needle):
            matches.append((i, line.strip(), "single"))
    if not matches:
        for i in range(len(lines)):
            window = " ".join(line.strip() for line in lines[i : i + 8])
            hay = compact_code(window)
            if needle in hay:
                matches.append((i + 1, lines[i].strip(), "window"))
    if len(matches) == 1:
        line, actual, _ = matches[0]
        return line, actual
    if old_line is not None and matches:
        # If the same code appears more than once, prefer the occurrence nearest
        # to the existing GT line. This is still deterministic and source-based;
        # it does not infer vulnerability semantics.
        ranked = sorted(matches, key=lambda item: (abs(item[0] - old_line), 0 if item[2] == "single" else 1))
        if len(ranked) == 1 or abs(ranked[0][0] - old_line) < abs(ranked[1][0] - old_line):
            return ranked[0][0], ranked[0][1]
    return None


def process_sample(record: dict[str, Any], results: Path, work: Path) -> dict[str, Any]:
    sid = record["local_sample_id"]
    gt_path = results / sid / "ground_truth.json"
    if not gt_path.exists():
        return {"sample_id": sid, "status": "missing_gt"}
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    src_root = work / sid / "src"
    corrected = []
    unresolved = []
    try:
        materialize_src(record, gt, src_root)
        for loc in collect_locations(gt):
            code = str(loc.get("code") or "")
            if not code:
                continue
            source_file = find_source_file(src_root, str(loc.get("file")))
            if not source_file:
                unresolved.append({"kind": loc["kind"], "reason": "file_missing", "file": loc.get("file")})
                continue
            try:
                old_line = int(loc.get("line"))
            except Exception:
                old_line = None
            match = unique_code_match(source_file, code, old_line)
            if not match:
                unresolved.append({"kind": loc["kind"], "reason": "not_unique_or_missing", "file": loc.get("file"), "code": code[:120]})
                continue
            line, actual = match
            obj = loc["obj"]
            if obj.get("line") != line or compact_code(str(obj.get("code") or "")) != compact_code(actual):
                corrected.append({"kind": loc["kind"], "file": loc.get("file"), "old_line": obj.get("line"), "new_line": line})
                obj["line"] = line
                obj["code"] = actual
        if corrected:
            gt_path.write_text(json.dumps(gt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"sample_id": sid, "status": "ok", "corrected": corrected, "unresolved": unresolved}
    except Exception as exc:
        return {"sample_id": sid, "status": "failed", "error": str(exc)}
    finally:
        shutil.rmtree(work / sid, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, default=ROOT / "selected_samples_json/cybergym_overlap_50_final_gt_passed.json")
    parser.add_argument("--results", type=Path, default=ROOT / "gt_results")
    parser.add_argument("--work", type=Path, default=ROOT / "work/line_correct_git")
    parser.add_argument("--output", type=Path, default=ROOT / "gt_results/cybergym_arvo50_line_correction_report.json")
    parser.add_argument("--sample-id", action="append", default=[])
    args = parser.parse_args()
    records = json.loads(args.selection.read_text(encoding="utf-8"))
    if args.sample_id:
        wanted = set(args.sample_id)
        records = [record for record in records if record["local_sample_id"] in wanted]
    args.work.mkdir(parents=True, exist_ok=True)
    items = []
    for index, record in enumerate(records, start=1):
        print(f"[{index}/{len(records)}] correcting {record['local_sample_id']}", flush=True)
        items.append(process_sample(record, args.results, args.work))
        args.output.write_text(json.dumps({"count": len(items), "items": items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"count": len(items), "corrected_samples": sum(1 for item in items if item.get("corrected"))}, indent=2))


if __name__ == "__main__":
    main()
