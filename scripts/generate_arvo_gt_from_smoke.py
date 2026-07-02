#!/usr/bin/env python3
"""Generate a first-pass ARVO GT from reproduced artifacts.

The output is intentionally conservative. It uses only:
- sanitizer_grounding_smoke.json
- patch.diff
- source_sample.json / trigger.json
- exported source files, when available, to quote code lines

It does not assign per-step grounding labels; runtime/repository evidence belongs
in validation artifacts rather than agent-generated GT.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTION = ROOT / "selected_samples_json" / "cybergym_overlap_50.json"


def norm_file(path: str | None) -> str:
    if not path:
        return ""
    p = path.replace("\\", "/")
    if p.startswith("/src/"):
        p = p[5:]
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p.lstrip("/")


def same_file(a: str, b: str) -> bool:
    a = norm_file(a)
    b = norm_file(b)
    return bool(a and b and (a == b or a.endswith("/" + b) or b.endswith("/" + a)))


def read_code(src_root: Path, file: str, line: int | None) -> str:
    if not file or not isinstance(line, int) or line <= 0:
        return ""
    candidates = [src_root / file]
    parts = Path(file).parts
    for i in range(1, len(parts)):
        candidates.append(src_root / Path(*parts[i:]))
    for path in candidates:
        if path.exists() and path.is_file():
            try:
                lines = path.read_text(errors="ignore").splitlines()
                if 1 <= line <= len(lines):
                    return lines[line - 1].strip()
            except Exception:
                return ""
    return ""


def patch_lines(patch_path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    old_file = ""
    new_file = ""
    old_line: int | None = None
    new_line: int | None = None
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for raw in patch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("--- "):
            old_file = norm_file(raw[4:].split("\t", 1)[0].strip())
            continue
        if raw.startswith("+++ "):
            new_file = norm_file(raw[4:].split("\t", 1)[0].strip())
            continue
        match = hunk_re.match(raw)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(2))
            continue
        if old_line is None or new_line is None or not raw:
            continue
        marker = raw[0]
        text = raw[1:].strip()
        if marker == " ":
            old_line += 1
            new_line += 1
        elif marker == "-":
            if old_file and old_file != "/dev/null":
                out.append({"file": old_file, "line": old_line, "kind": "removed", "text": text})
            old_line += 1
        elif marker == "+":
            if new_file and new_file != "/dev/null":
                out.append({"file": new_file, "line": new_line, "kind": "added", "text": text})
            new_line += 1
    return out


def has_loc(loc: dict[str, Any] | None) -> bool:
    return isinstance(loc, dict) and bool(loc.get("file")) and isinstance(loc.get("line"), int)


def choose_root(san: dict[str, Any], patches: list[dict[str, Any]], src_root: Path) -> dict[str, Any]:
    free = san.get("free_context") or {}
    crash = san.get("crash_location") or {}
    # Prefer a removed patch line close to the sanitizer free context for UAF.
    if has_loc(free):
        same = [p for p in patches if p["kind"] == "removed" and same_file(p["file"], free.get("file", ""))]
        if same:
            same.sort(key=lambda p: abs(int(p["line"]) - int(free.get("line", p["line"]))))
            best = same[0]
            if abs(int(best["line"]) - int(free.get("line", best["line"]))) <= 8:
                return {
                    "file": best["file"],
                    "function": free.get("function", ""),
                    "line": best["line"],
                    "description": "Patch-adjacent lifetime/state transition associated with the sanitizer free context.",
                }
    # Otherwise prefer free context for UAF-like crashes.
    if has_loc(free):
        return {
            "file": free.get("file", ""),
            "function": free.get("function", ""),
            "line": free.get("line"),
            "description": "Sanitizer free context; this is the most concrete runtime root-cause anchor available in the first-pass GT.",
        }
    # Fallback to nearest removed patch line in same file as crash.
    same = [p for p in patches if p["kind"] == "removed" and same_file(p["file"], crash.get("file", ""))]
    if same:
        same.sort(key=lambda p: abs(int(p["line"]) - int(crash.get("line", p["line"]))))
        best = same[0]
        return {
            "file": best["file"],
            "function": crash.get("function", ""),
            "line": best["line"],
            "description": "Patch-modified statement selected as first-pass root-cause anchor.",
        }
    return {
        "file": crash.get("file", ""),
        "function": crash.get("function", ""),
        "line": crash.get("line"),
        "description": "Crash location used as fallback root-cause anchor; requires review.",
    }


def find_entry_frame(san: dict[str, Any]) -> dict[str, Any]:
    for frame in san.get("crash_stack", []):
        file = frame.get("file", "")
        fn = frame.get("function", "")
        if "LLVMFuzzerTestOneInput" in fn or "fuzz" in file.lower():
            return frame
    stack = san.get("crash_stack", [])
    return stack[-1] if stack else {}


def loc_obj(loc: dict[str, Any], desc: str) -> dict[str, Any]:
    return {
        "file": norm_file(loc.get("file", "")),
        "function": loc.get("function", ""),
        "line": loc.get("line"),
        "description": desc,
    }


def frame_step(step_no: int, frame: dict[str, Any], role: str, src_root: Path) -> dict[str, Any]:
    file = norm_file(frame.get("file", ""))
    line = frame.get("line")
    fn = frame.get("function", "")
    return {
        "step": step_no,
        "file": file,
        "function": fn,
        "line": line,
        "role": role,
        "var": "runtime_state",
        "code": read_code(src_root, file, line),
        "note": f"Runtime sanitizer frame used as first-pass {role} evidence.",
    }


def normalize_sanitizer(san: dict[str, Any]) -> dict[str, Any]:
    out = dict(san)
    for key in ["crash_location", "allocation_context", "free_context"]:
        if isinstance(out.get(key), dict):
            out[key]["file"] = norm_file(out[key].get("file", ""))
    for key in ["crash_stack", "allocation_stack", "free_stack"]:
        frames = []
        for frame in out.get(key, []) or []:
            f = dict(frame)
            f["file"] = norm_file(f.get("file", ""))
            if f.get("line") is None:
                continue
            frames.append({k: f[k] for k in ["frame", "function", "file", "line"] if k in f})
        out[key] = frames
    out.setdefault("detector", "asan")
    out.setdefault("trace_format", "asan")
    out.setdefault("sanitizer", "AddressSanitizer")
    out.setdefault("cross_tool_confirmed", False)
    out.setdefault("reproduction_rate", 1.0)
    out.setdefault("flaky", False)
    out["cross_validation"] = {
        "sink_matches_crash": "not_validated_by_generator",
        "trace_consistent_with_stack": "not_validated_by_generator",
        "tainted_value_reaches_sink": "not_validated_by_generator",
        "patch_resolves": bool(out.get("patch_resolves")),
    }
    return out


def load_source_metadata(result: Path, sample_id: str) -> dict[str, Any]:
    source_path = result / "source_sample.json"
    if source_path.exists():
        return json.loads(source_path.read_text())
    if DEFAULT_SELECTION.exists():
        for record in json.loads(DEFAULT_SELECTION.read_text()):
            if record.get("local_sample_id") == sample_id:
                source_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
                return record
    return {"local_sample_id": sample_id}


def load_trigger_metadata(result: Path) -> dict[str, Any]:
    trigger_path = result / "trigger.json"
    if trigger_path.exists():
        return json.loads(trigger_path.read_text())
    return {}


def infer_poc_format(source: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
    target_binary = str(trigger.get("target_binary") or "")
    target_command = str(trigger.get("target_command") or trigger.get("command") or "")
    container_poc_path = str(trigger.get("container_poc_path") or "/tmp/poc")
    text = " ".join(
        str(value)
        for value in [
            source.get("normalized_bug_description"),
            source.get("original_bug_description"),
            target_binary,
            target_command,
        ]
        if value
    ).lower()
    format_hints = [
        ("pkcs12", "PKCS#12 DER"),
        ("pdf", "PDF"),
        ("tga", "TGA image"),
        ("png", "PNG image"),
        ("jpeg", "JPEG image"),
        ("jpg", "JPEG image"),
        ("bmp", "BMP image"),
        ("zip", "ZIP archive"),
        ("xml", "XML document"),
        ("json", "JSON document"),
        ("html", "HTML document"),
        ("javascript", "JavaScript source"),
        ("js", "JavaScript source"),
        ("mp4", "MP4 media container"),
        ("pcap", "pcap capture/filter input"),
    ]
    format_name = "project-specific fuzzer input"
    for needle, label in format_hints:
        if needle in text:
            format_name = label
            break
    component = Path(target_binary).name or "target parser input"
    return {
        "name": format_name,
        "contract": (
            f"Single PoC file at {container_poc_path} is consumed by {component}. "
            "A candidate should be accepted far enough by the target harness to "
            "reach the annotated source/root-cause/sink chain."
        ),
    }


def bug_description(source: dict[str, Any], sample_id: str) -> dict[str, str]:
    original = str(
        source.get("original_bug_description")
        or source.get("bug_description")
        or source.get("description")
        or f"CyberGym/ARVO sample {sample_id}"
    )
    normalized = str(
        source.get("normalized_bug_description")
        or source.get("normalized_description")
        or original
    )
    return {
        "original": original,
        "original_source": "ARVO-Meta / OSS-Fuzz issue metadata",
        "normalized": normalized,
    }


def reachability_checkpoints(entry: dict[str, Any], src_root: Path) -> dict[str, Any]:
    admitted = loc_obj(
        entry,
        "The PoC is admitted far enough to enter the target parser or harness path toward the annotated source/root-cause/sink chain.",
    )
    admitted["code"] = read_code(src_root, admitted.get("file", ""), admitted.get("line"))
    return {"parser_admitted": admitted}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample_id")
    ap.add_argument("--results", type=Path, default=ROOT / "gt_results")
    ap.add_argument("--work", type=Path, default=ROOT / "work" / "cybergym_arvo50")
    args = ap.parse_args()

    sid = args.sample_id
    result = args.results / sid
    src_root = args.work / sid / "vul" / "src"
    san = json.loads((result / "sanitizer_grounding_smoke.json").read_text())
    source = load_source_metadata(result, sid)
    trigger = load_trigger_metadata(result)
    patches = patch_lines(result / "patch.diff")
    san_norm = normalize_sanitizer(san)
    crash = san_norm.get("crash_location", {})
    root = choose_root(san_norm, patches, src_root)
    entry = find_entry_frame(san_norm)
    alloc = san_norm.get("allocation_context") or {}
    free = san_norm.get("free_context") or {}

    gt: dict[str, Any] = {
        "sample_id": sid,
        "vuln_id": f"OSS-Fuzz issue {source.get('benchmark_id')} / CyberGym arvo:{source.get('benchmark_id')}",
        "project": {
            "id": source.get("project") or trigger.get("project") or "",
            "repo": source.get("repo_url") or source.get("repo") or "",
            "vulnerable_commit": source.get("vulnerable_commit") or "",
            "fixed_commit": source.get("fix_commit") or "",
        },
        "classification": {
            "class": san_norm.get("crash_type") or source.get("primary_category") or "",
            "cwe": source.get("primary_category") or source.get("category") or "",
        },
        "bug_description": bug_description(source, sid),
        "source": {
            **loc_obj(entry, "Fuzzer or harness entry frame for attacker-controlled PoC input."),
            "value_from": "Attacker-controlled PoC bytes supplied to the fuzz target.",
        },
        "sink": loc_obj(crash, "Authoritative sanitizer crash location."),
        "root_cause": root,
        "reachability_checkpoints": reachability_checkpoints(entry, src_root),
        "tainted_value_origin": {
            "file": root["file"],
            "function": root["function"],
            "line": root["line"],
            "var": "vulnerability_state",
            "code": read_code(src_root, root["file"], root["line"]),
            "description": "First-pass vulnerability-relevant state anchor selected from patch/runtime evidence.",
        },
        "coarse_trace": [],
        "fine_trace": [],
        "sanitizer_ground_truth": san_norm,
        "poc": {
            "path": trigger.get("local_poc_path") or "poc",
            "trigger": trigger.get("target_command") or trigger.get("command") or "",
            "format": infer_poc_format(source, trigger),
        },
    }

    # Coarse trace from project frames in crash stack, preserving order from entry to crash.
    project_frames = [
        f for f in san_norm.get("crash_stack", [])
        if "libfuzzer/" not in f.get("file", "") and "compiler-rt/" not in f.get("file", "")
    ]
    coarse_frames = list(reversed(project_frames[:8]))
    seen = set()
    coarse = []
    for frame in coarse_frames:
        key = (frame.get("file"), frame.get("function"))
        if key in seen:
            continue
        seen.add(key)
        coarse.append({
            "step": len(coarse) + 1,
            "file": frame.get("file", ""),
            "function": frame.get("function", ""),
            "role": "entry" if not coarse else ("sink" if frame == project_frames[0] else "propagation"),
            "summary": "Project frame observed in the sanitizer crash stack.",
        })
    if not coarse:
        coarse = [{"step": 1, "file": crash.get("file", ""), "function": crash.get("function", ""), "role": "sink", "summary": "Sanitizer crash location."}]
    gt["coarse_trace"] = coarse

    fine = []
    fine.append(frame_step(1, entry, "source", src_root))
    if has_loc(alloc):
        fine.append(frame_step(len(fine) + 1, alloc, "allocation", src_root))
    if has_loc(free):
        fine.append(frame_step(len(fine) + 1, free, "free", src_root))
    fine.append({
        "step": len(fine) + 1,
        "file": root["file"],
        "function": root["function"],
        "line": root["line"],
        "role": "root_cause",
        "var": "vulnerability_state",
        "code": read_code(src_root, root["file"], root["line"]),
        "note": root["description"],
    })
    if project_frames:
        caller = project_frames[1] if len(project_frames) > 1 else project_frames[0]
        if not (same_file(caller.get("file", ""), root["file"]) and caller.get("line") == root["line"]):
            fine.append(frame_step(len(fine) + 1, caller, "unsafe_use", src_root))
    fine.append({
        "step": len(fine) + 1,
        "file": crash.get("file", ""),
        "function": crash.get("function", ""),
        "line": crash.get("line"),
        "role": "sink",
        "var": "crashing_access",
        "code": read_code(src_root, crash.get("file", ""), crash.get("line")),
        "note": "Authoritative sanitizer crash location.",
    })
    # Re-number after possible duplicate root/sink overlaps.
    dedup = []
    seen_locs = set()
    for step in fine:
        key = (step["role"], step["file"], step["line"])
        if key in seen_locs:
            continue
        seen_locs.add(key)
        step["step"] = len(dedup) + 1
        dedup.append(step)
    gt["fine_trace"] = dedup

    (result / "ground_truth.json").write_text(json.dumps(gt, indent=2, ensure_ascii=False) + "\n")
    (result / "watchpoint.json").write_text(json.dumps({"schema": "gdb_watch.v1", "hits": []}, indent=2) + "\n")


if __name__ == "__main__":
    main()
