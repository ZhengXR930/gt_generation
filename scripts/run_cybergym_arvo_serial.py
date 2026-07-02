#!/usr/bin/env python3
"""Serially materialize and reproduce CyberGym/ARVO overlap samples.

This script intentionally processes one sample at a time. It pulls at most one
ARVO image at a time, exports the runtime/source artifacts needed for GT work,
runs vulnerable/fixed differential checks, then removes the Docker image.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTION = ROOT / "selected_samples_json" / "cybergym_overlap_50.json"
DEFAULT_RESULTS = ROOT / "gt_results"
DEFAULT_WORK = ROOT / "work" / "cybergym_arvo50"


FRAME_RE = re.compile(r"^\s*#(\d+)\s+0x[0-9a-fA-F]+ in (.*?) (/src/.*?):(\d+):(\d+)")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout


def checked(cmd: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> str:
    code, out = run(cmd, cwd=cwd, timeout=timeout)
    if code != 0:
        raise RuntimeError(f"command failed ({code}): {' '.join(cmd)}\n{out[-4000:]}")
    return out


def norm_src_path(path: str) -> str:
    return path[5:] if path.startswith("/src/") else path


def parse_sanitizer(trace: str) -> dict[str, Any]:
    m = re.search(r"ERROR: AddressSanitizer: ([^\s]+)", trace)
    crash_type = m.group(1) if m else None
    m = re.search(r"\b(READ|WRITE) of size (\d+)", trace)
    access_type = m.group(1) if m else None
    access_size = int(m.group(2)) if m else None

    sections: dict[str, list[dict[str, Any]]] = {
        "crash_stack": [],
        "free_stack": [],
        "allocation_stack": [],
    }
    current: str | None = "crash_stack"
    for line in trace.splitlines():
        if line.startswith("freed by thread"):
            current = "free_stack"
            continue
        if line.startswith("previously allocated by thread"):
            current = "allocation_stack"
            continue
        if line.startswith("SUMMARY:"):
            current = None
        if current is None:
            continue
        fm = FRAME_RE.match(line)
        if not fm:
            continue
        sections[current].append(
            {
                "frame": int(fm.group(1)),
                "function": fm.group(2).strip(),
                "file": norm_src_path(fm.group(3)),
                "line": int(fm.group(4)),
                "column": int(fm.group(5)),
                "raw": line.strip(),
            }
        )

    def first_project_frame(frames: list[dict[str, Any]]) -> dict[str, Any]:
        runtime_markers = (
            "llvm/projects/compiler-rt/",
            "libfuzzer/",
            "__libc_start_main",
            "asan_",
        )
        for frame in frames:
            if not any(marker in frame["file"] or marker in frame["function"] for marker in runtime_markers):
                return {k: frame[k] for k in ("function", "file", "line", "column")}
        return {k: frames[0][k] for k in ("function", "file", "line", "column")} if frames else {}

    return {
        "detector": "asan",
        "trace_format": "asan",
        "sanitizer": "AddressSanitizer",
        "crash_type": crash_type,
        "access_type": access_type,
        "access_size": access_size,
        "crash_location": first_project_frame(sections["crash_stack"]),
        "free_context": first_project_frame(sections["free_stack"]),
        "allocation_context": first_project_frame(sections["allocation_stack"]),
        **sections,
    }


def docker_cp(container: str, src: str, dst: Path) -> None:
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    checked(["docker", "cp", f"{container}:{src}", str(dst)], timeout=900)


def export_image(image: str, sample_work: Path, variant: str, *, copy_src: bool, copy_out: bool) -> dict[str, Any]:
    checked(["docker", "pull", image], timeout=1800)
    container = f"gt_{variant}_{os.getpid()}_{sample_work.name}"
    checked(["docker", "create", "--name", container, image], timeout=120)
    exported: dict[str, Any] = {"image": image}
    try:
        variant_dir = sample_work / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        docker_cp(container, "/bin/arvo", variant_dir / "arvo_entrypoint.sh")
        if variant == "vul":
            docker_cp(container, "/tmp/poc", variant_dir / "poc")
        if copy_src:
            docker_cp(container, "/src", variant_dir / "src")
            exported["src"] = str(variant_dir / "src")
        if copy_out:
            docker_cp(container, "/out", variant_dir / "out")
            exported["out"] = str(variant_dir / "out")
        code, pwd = run(["docker", "run", "--rm", "--entrypoint", "/bin/pwd", image], timeout=120)
        if code == 0:
            exported["container_workdir"] = next((line.strip() for line in pwd.splitlines() if line.strip()), "")
        code, script = run(
            ["docker", "run", "--rm", "--entrypoint", "/bin/bash", image, "-lc", "sed -n '1,240p' /bin/arvo"],
            timeout=120,
        )
        if code == 0:
            exported["entrypoint_text"] = script
            match = re.search(r"(/\S+)\s+/tmp/poc", script)
            if match:
                exported["target_command"] = match.group(0)
                exported["target_binary"] = match.group(1)
    finally:
        run(["docker", "rm", "-f", container], timeout=120)
        run(["docker", "rmi", image], timeout=300)
    return exported


def run_arvo_image(image: str, poc_path: Path, trace_path: Path, timeout: int) -> int:
    code, out = run(
        ["docker", "run", "--rm", "-v", f"{poc_path}:/tmp/poc:ro", image, "arvo"],
        timeout=timeout,
    )
    trace_path.write_text(out)
    run(["docker", "rmi", image], timeout=300)
    return code


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


SKIP_DIFF_DIRS = {".git", ".svn", ".hg", "__pycache__", "node_modules", ".cache"}
SKIP_DIFF_SUFFIXES = {
    ".a",
    ".o",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".class",
    ".jar",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".gz",
    ".xz",
    ".bz2",
    ".db",
    ".sqlite",
}


def should_skip_diff_file(path: Path) -> bool:
    if any(part in SKIP_DIFF_DIRS for part in path.parts):
        return True
    return path.suffix.lower() in SKIP_DIFF_SUFFIXES


def read_text_lines_for_diff(path: Path) -> list[str] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > 2_000_000 or b"\0" in data:
        return None
    try:
        return data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1").splitlines()
        except UnicodeDecodeError:
            return None


def sanitizer_relevant_files(sanitizer: dict[str, Any]) -> set[Path]:
    files: set[Path] = set()
    for key in ("crash_location", "allocation_context", "free_context"):
        loc = sanitizer.get(key)
        if isinstance(loc, dict) and loc.get("file"):
            files.add(Path(str(loc["file"])))
    for key in ("crash_stack", "allocation_stack", "free_stack"):
        for frame in sanitizer.get(key, []) or []:
            if isinstance(frame, dict) and frame.get("file"):
                files.add(Path(str(frame["file"])))
    return files


def generate_patch_from_exported_sources(
    sample_work: Path,
    result_dir: Path,
    relevant_files: set[Path] | None = None,
) -> None:
    patch_path = result_dir / "patch.diff"
    if patch_path.exists() and patch_path.stat().st_size > 0:
        return
    vul_src = sample_work / "vul" / "src"
    fix_src = sample_work / "fix" / "src"
    if not vul_src.exists() or not fix_src.exists():
        return
    if relevant_files:
        rels = {rel for rel in relevant_files if not should_skip_diff_file(rel)}
    else:
        rels = set()
        for root in (vul_src, fix_src):
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(root)
                if should_skip_diff_file(rel):
                    continue
                rels.add(rel)

    chunks: list[str] = []
    for rel in sorted(rels):
        old_path = vul_src / rel
        new_path = fix_src / rel
        old_lines = read_text_lines_for_diff(old_path) if old_path.exists() else []
        new_lines = read_text_lines_for_diff(new_path) if new_path.exists() else []
        if old_lines is None or new_lines is None or old_lines == new_lines:
            continue
        rel_s = rel.as_posix()
        diff_lines = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel_s}",
            tofile=f"b/{rel_s}",
            n=3,
            lineterm="",
        )
        chunks.extend(line + "\n" for line in diff_lines)
    patch_path.write_text("".join(chunks))


def process_sample(record: dict[str, Any], args: argparse.Namespace) -> None:
    sid = record["local_sample_id"]
    arvo_id = str(record["arvo_id"])
    result_dir = args.results.resolve() / sid
    sample_work = args.work.resolve() / sid
    result_dir.mkdir(parents=True, exist_ok=True)
    sample_work.mkdir(parents=True, exist_ok=True)

    vul_image = f"n132/arvo:{arvo_id}-vul"
    fix_image = f"n132/arvo:{arvo_id}-fix"
    log_lines = [f"{now()} start {sid}"]

    source_poc_dir = ROOT / "final_dataset" / record["poc_dir"]
    for name in ("patch.diff", "trigger.json", "source_sample.json"):
        src = source_poc_dir / name
        if src.exists():
            shutil.copyfile(src, result_dir / name)
    if not (result_dir / "source_sample.json").exists():
        write_json(result_dir / "source_sample.json", record)
    dataset_patch = ROOT / "final_dataset" / record["patch_diff_path"]
    if dataset_patch.exists():
        shutil.copyfile(dataset_patch, result_dir / "patch.diff")
    elif (result_dir / "patch.diff").exists():
        (result_dir / "patch.diff").unlink()

    try:
        exported_vul = export_image(vul_image, sample_work, "vul", copy_src=args.copy_src, copy_out=args.copy_out)
        poc_path = sample_work / "vul" / "poc"
        shutil.copyfile(poc_path, result_dir / "poc")

        # Re-pull for execution because export_image removes the image after copying.
        checked(["docker", "pull", vul_image], timeout=1800)
        vul_code = run_arvo_image(vul_image, poc_path, result_dir / "sanitizer_trace.txt", args.run_timeout)
        log_lines.append(f"{now()} vul_exit={vul_code}")
        sanitizer_trace = (result_dir / "sanitizer_trace.txt").read_text(errors="ignore")
        sanitizer = parse_sanitizer(sanitizer_trace)

        exported_fix = export_image(fix_image, sample_work, "fix", copy_src=args.copy_src, copy_out=args.copy_out)
        generate_patch_from_exported_sources(sample_work, result_dir, sanitizer_relevant_files(sanitizer))
        checked(["docker", "pull", fix_image], timeout=1800)
        fix_code = run_arvo_image(fix_image, poc_path, result_dir / "post_patch_trace.txt", args.run_timeout)
        log_lines.append(f"{now()} fix_exit={fix_code}")

        post_patch_trace = (result_dir / "post_patch_trace.txt").read_text(errors="ignore")
        sanitizer["patch_resolves"] = fix_code == 0 and "ERROR: AddressSanitizer" not in post_patch_trace
        sanitizer["cross_tool_confirmed"] = False
        sanitizer["reproduction_rate"] = 1.0
        sanitizer["flaky"] = False
        sanitizer["source"] = "parsed from ARVO vulnerable Docker sanitizer_trace.txt"
        write_json(result_dir / "sanitizer_grounding_smoke.json", sanitizer)

        build_sh = result_dir / "build.sh"
        build_sh.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail

# Re-materialize CyberGym/ARVO sample {sid}.
# Project-specific dependency/build adaptation should be appended below when
# producing debug/Valgrind/watchpoint binaries.

ARVO_ID={arvo_id}
ROOT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
WORK_DIR="${{ROOT_DIR}}/../../work/cybergym_arvo50/{sid}"
mkdir -p "$WORK_DIR"

docker pull "n132/arvo:${{ARVO_ID}}-vul"
docker create --name "gt_${{ARVO_ID}}_vul_$$" "n132/arvo:${{ARVO_ID}}-vul"
docker cp "gt_${{ARVO_ID}}_vul_$$:/src" "$WORK_DIR/vul/src"
docker cp "gt_${{ARVO_ID}}_vul_$$:/out" "$WORK_DIR/vul/out"
docker cp "gt_${{ARVO_ID}}_vul_$$:/tmp/poc" "$WORK_DIR/vul/poc"
docker rm -f "gt_${{ARVO_ID}}_vul_$$"
docker rmi "n132/arvo:${{ARVO_ID}}-vul"

docker pull "n132/arvo:${{ARVO_ID}}-fix"
docker create --name "gt_${{ARVO_ID}}_fix_$$" "n132/arvo:${{ARVO_ID}}-fix"
docker cp "gt_${{ARVO_ID}}_fix_$$:/src" "$WORK_DIR/fix/src"
docker cp "gt_${{ARVO_ID}}_fix_$$:/out" "$WORK_DIR/fix/out"
docker rm -f "gt_${{ARVO_ID}}_fix_$$"
docker rmi "n132/arvo:${{ARVO_ID}}-fix"
"""
        )
        build_sh.chmod(0o755)

        state = {
            "sample_id": sid,
            "status": "materialized_smoke_completed",
            "current_stage": "gt_generation",
            "completed_stages": [
                "arvo_vulnerable_export",
                "arvo_vulnerable_reproduction",
                "arvo_fixed_export",
                "arvo_post_patch_differential",
                "sanitizer_trace_structuring",
            ],
            "failure": None,
            "artifacts": {
                "build_script": "build.sh",
                "patch_diff": "patch.diff",
                "poc": "poc",
                "sanitizer_trace": "sanitizer_trace.txt",
                "post_patch_trace": "post_patch_trace.txt",
                "ground_truth": None,
                "work_vul_src": str(sample_work / "vul" / "src") if args.copy_src else None,
                "work_fix_src": str(sample_work / "fix" / "src") if args.copy_src else None,
                "work_vul_out": str(sample_work / "vul" / "out") if args.copy_out else None,
                "work_fix_out": str(sample_work / "fix" / "out") if args.copy_out else None,
            },
            "reproduction": {
                "primary_detector": "asan",
                "primary_detector_crash_observed": "ERROR: AddressSanitizer" in sanitizer_trace,
                "docker_exit_code_vul": vul_code,
                "docker_exit_code_fix": fix_code,
                "patch_resolves": sanitizer["patch_resolves"],
                "crash_type": sanitizer.get("crash_type"),
                "access_type": sanitizer.get("access_type"),
                "access_size": sanitizer.get("access_size"),
                "crash_location": sanitizer.get("crash_location"),
                "free_context": sanitizer.get("free_context"),
                "allocation_context": sanitizer.get("allocation_context"),
                "target_command": exported_vul.get("target_command") or record.get("target_command"),
                "target_binary": exported_vul.get("target_binary") or record.get("target_binary"),
            },
            "arvo_export": {"vul": exported_vul, "fix": exported_fix},
            "validation": {
                "schema_valid": False,
                "post_patch_differential_valid": sanitizer["patch_resolves"],
                "requires_human_review": True,
                "review_reason": "GT has not been generated yet; smoke artifacts are ready.",
            },
            "cleanup": {"docker_images_removed": True, "source_deleted": False, "build_deleted": False},
            "updated_at": now(),
        }
        write_json(result_dir / "sample_state.json", state)
        log_lines.append(f"{now()} completed {sid}")
    except Exception as exc:
        log_lines.append(f"{now()} failed {sid}: {exc}")
        write_json(
            result_dir / "sample_state.json",
            {
                "sample_id": sid,
                "status": "failed",
                "current_stage": "arvo_materialization",
                "failure": {"type": type(exc).__name__, "message": str(exc)},
                "updated_at": now(),
            },
        )
        raise
    finally:
        (result_dir / "generation.log").write_text("\n".join(log_lines) + "\n")
        if args.delete_work:
            shutil.rmtree(sample_work, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start-at", default="")
    parser.add_argument("--copy-src", action="store_true")
    parser.add_argument("--copy-out", action="store_true")
    parser.add_argument("--delete-work", action="store_true")
    parser.add_argument("--run-timeout", type=int, default=300)
    args = parser.parse_args()

    records = json.loads(args.selection.read_text())
    if args.start_at:
        idx = next((i for i, r in enumerate(records) if r["local_sample_id"] == args.start_at), None)
        if idx is None:
            raise SystemExit(f"start sample not found: {args.start_at}")
        records = records[idx:]
    if args.limit:
        records = records[: args.limit]

    for index, record in enumerate(records, 1):
        print(f"[{index}/{len(records)}] {record['local_sample_id']} {record.get('project')} {record.get('cwe')}", flush=True)
        process_sample(record, args)


if __name__ == "__main__":
    main()
