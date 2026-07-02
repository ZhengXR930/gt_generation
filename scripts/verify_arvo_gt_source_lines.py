#!/usr/bin/env python3
"""Verify ARVO GT source locations against vulnerable source checkouts.

The verifier is intentionally serial and storage-conscious:

1. Materialize one vulnerable source tree. By default this uses
   repo+vulnerable_commit with sparse checkout of only GT-mentioned files.
   A Docker /src export mode is available for ARVO image checks.
3. Check every source/sink/root/fine_trace file:line and code snippet.
4. Delete the copied source and Docker image before moving on.

It does not judge vulnerability semantics. It only answers whether the GT's
claimed locations can be audited against the vulnerable source tree.
"""

from __future__ import annotations

import argparse
import json
import os
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


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


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
    text = re.sub(r"//.*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text)
    return re.sub(r"\s+", " ", text).strip().rstrip(";")


def find_source_file(src_root: Path, claimed: str) -> Path | None:
    claimed = norm_path(claimed)
    direct = src_root / claimed
    if direct.exists():
        return direct
    parts = claimed.split("/")
    candidates: list[Path] = []
    for i in range(1, len(parts)):
        suffix = Path(*parts[i:])
        direct = src_root / suffix
        if direct.exists():
            candidates.append(direct)
    if candidates:
        return sorted(candidates, key=lambda p: len(str(p)))[0]
    name = parts[-1] if parts else claimed
    if not name:
        return None
    matches = [p for p in src_root.rglob(name) if p.is_file()]
    suffix_matches = [p for p in matches if str(p).replace("\\", "/").endswith(claimed)]
    if suffix_matches:
        return sorted(suffix_matches, key=lambda p: len(str(p)))[0]
    if len(matches) == 1:
        return matches[0]
    return None


def source_line(path: Path, line: int, line_end: int | None = None) -> str | None:
    try:
        # Source locations from compilers, sanitizers, diffs, and editors count
        # physical newline bytes.  Python's splitlines() also splits on form
        # feeds, which appear in some C/C++ sources as page separators and
        # would shift every following line number.
        lines = path.read_text(errors="replace").split("\n")
    except OSError:
        return None
    if line < 1 or line > len(lines):
        return None
    end = line_end if line_end and line_end >= line else line
    end = min(end, len(lines))
    return "\n".join(lines[line - 1 : end])


def code_matches(claimed_code: str | None, actual: str | None) -> bool | None:
    if not claimed_code:
        return None
    if actual is None:
        return False
    c = compact_code(claimed_code)
    a = compact_code(actual)
    if not c:
        return None
    return c in a or a in c


def collect_locations(gt: dict[str, Any]) -> list[dict[str, Any]]:
    locs: list[dict[str, Any]] = []
    for name in ("source", "sink", "root_cause", "taint_source", "tainted_value_origin"):
        obj = gt.get(name)
        if isinstance(obj, dict) and obj.get("file") and obj.get("line"):
            locs.append(
                {
                    "kind": name,
                    "file": obj.get("file"),
                    "line": obj.get("line"),
                    "line_end": obj.get("line_end"),
                    "code": obj.get("code"),
                    "role": obj.get("role", name),
                }
            )
    for step in gt.get("fine_trace", []):
        if isinstance(step, dict) and step.get("file") and step.get("line"):
            locs.append(
                {
                    "kind": f"fine_trace[{step.get('step')}]",
                    "file": step.get("file"),
                    "line": step.get("line"),
                    "line_end": step.get("line_end"),
                    "code": step.get("code"),
                    "role": step.get("role"),
                    "var": step.get("var"),
                }
            )
    return locs


def verify_locations(src_root: Path, gt: dict[str, Any]) -> dict[str, Any]:
    checked_items: list[dict[str, Any]] = []
    counts = {
        "total": 0,
        "file_found": 0,
        "line_found": 0,
        "code_match": 0,
        "code_mismatch": 0,
        "code_absent": 0,
    }
    for loc in collect_locations(gt):
        counts["total"] += 1
        rel = norm_path(str(loc.get("file")))
        found = find_source_file(src_root, rel)
        item = dict(loc)
        item["normalized_file"] = rel
        item["resolved_file"] = str(found.relative_to(src_root)) if found else None
        if not found:
            item["status"] = "file_missing"
            checked_items.append(item)
            continue
        counts["file_found"] += 1
        actual = source_line(found, int(loc["line"]), loc.get("line_end"))
        item["source_line"] = actual
        if actual is None:
            item["status"] = "line_missing"
            checked_items.append(item)
            continue
        counts["line_found"] += 1
        match = code_matches(loc.get("code"), actual)
        if match is True:
            counts["code_match"] += 1
            item["status"] = "code_match"
        elif match is False:
            counts["code_mismatch"] += 1
            item["status"] = "code_mismatch"
        else:
            counts["code_absent"] += 1
            item["status"] = "line_only"
        checked_items.append(item)
    status = "pass"
    if any(item["status"] in {"file_missing", "line_missing", "code_mismatch"} for item in checked_items):
        status = "warn"
    return {"status": status, "counts": counts, "locations": checked_items}


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


def materialize_src_from_git(record: dict[str, Any], gt: dict[str, Any], dst: Path) -> None:
    repo = normalize_repo_url(str(record.get("repo") or ""))
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
    code, out = run(
        ["git", "-C", str(dst), "fetch", "--depth=1", "--filter=blob:none", "origin", commit],
        timeout=900,
    )
    if code != 0:
        code, out = run(["git", "-C", str(dst), "fetch", "--depth=1", "origin", commit], timeout=900)
    if code != 0:
        raise RuntimeError(f"git fetch failed for {repo} {commit}\n{out[-4000:]}")
    checked(["git", "-C", str(dst), "checkout", "-q", "FETCH_HEAD"], timeout=300)


def copy_src_from_image(arvo_id: int, dst: Path) -> None:
    image = f"n132/arvo:{arvo_id}-vul"
    checked(["docker", "pull", image], timeout=1800)
    container = f"gt_src_verify_{os.getpid()}_{arvo_id}"
    try:
        checked(["docker", "create", "--name", container, image], timeout=120)
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        checked(["docker", "cp", f"{container}:/src", str(dst)], timeout=900)
    finally:
        run(["docker", "rm", "-f", container], timeout=120)
        run(["docker", "rmi", image], timeout=300)


def verify_sample(record: dict[str, Any], results: Path, work: Path, source_provider: str) -> dict[str, Any]:
    sid = record["local_sample_id"]
    arvo_id = int(record["arvo_id"])
    gt_path = results / sid / "ground_truth.json"
    if not gt_path.exists():
        return {"sample_id": sid, "arvo_id": arvo_id, "status": "fail", "error": "missing ground_truth.json"}
    sample_work = work / sid
    src_root = sample_work / "src"
    try:
        gt = read_json(gt_path)
        if source_provider == "docker":
            copy_src_from_image(arvo_id, src_root)
        else:
            materialize_src_from_git(record, gt, src_root)
        out = verify_locations(src_root, gt)
        out.update({"sample_id": sid, "arvo_id": arvo_id, "project": record.get("project")})
        return out
    except Exception as exc:
        return {"sample_id": sid, "arvo_id": arvo_id, "project": record.get("project"), "status": "fail", "error": str(exc)}
    finally:
        if sample_work.exists():
            shutil.rmtree(sample_work)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, default=ROOT / "selected_samples_json" / "cybergym_overlap_50_effective.json")
    parser.add_argument("--results", type=Path, default=ROOT / "gt_results")
    parser.add_argument("--work", type=Path, default=ROOT / "work" / "source_line_verify")
    parser.add_argument("--output", type=Path, default=ROOT / "gt_results" / "cybergym_arvo50_source_line_audit.json")
    parser.add_argument("--source-provider", choices=["git", "docker"], default="git")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sample-id", action="append", default=[])
    args = parser.parse_args()

    records = read_json(args.selection)
    if args.sample_id:
        wanted = set(args.sample_id)
        records = [record for record in records if record["local_sample_id"] in wanted]
    if args.limit:
        records = records[: args.limit]

    args.work.mkdir(parents=True, exist_ok=True)
    samples = []
    for index, record in enumerate(records, start=1):
        sid = record["local_sample_id"]
        print(f"[{index}/{len(records)}] verifying {sid}", flush=True)
        samples.append(verify_sample(record, args.results, args.work, args.source_provider))
        partial = {
            "total": len(samples),
            "counts": {},
            "samples": samples,
        }
        for item in samples:
            partial["counts"][item["status"]] = partial["counts"].get(item["status"], 0) + 1
        write_json(args.output, partial)

    report = {"total": len(samples), "counts": {}, "samples": samples}
    for item in samples:
        report["counts"][item["status"]] = report["counts"].get(item["status"], 0) + 1
    write_json(args.output, report)
    print(json.dumps({"total": report["total"], "counts": report["counts"]}, indent=2))


if __name__ == "__main__":
    main()
