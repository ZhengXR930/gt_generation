"""Generate runtime context ground truth for valid GT samples.

The collector runs the frozen PoC once under GDB, using GT anchors as
breakpoints, and records the visited project functions plus bounded stacks.
It is resumable: by default existing ``context_gt.json`` files are left intact.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluator.reachability.arvo_gdb import prepare_arvo_target, target_arguments
from evaluator.reachability.engine import extract_reachability_checkpoints, write_breakpoint_spec
from evaluator.reachability.local_gdb import _ensure_runtime_prepared, _gdb_invocation_for_runtime
from evaluator.reachability.runtime_spec import compile_runtime_spec, container_path_on_host


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _valid_sample_ids(valid_gt: Path) -> list[str]:
    data = _load_json(valid_gt)
    raw = data.get("samples", data if isinstance(data, list) else [])
    result: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            sample_id = (
                item.get("sample_id")
                or item.get("id")
                or item.get("task_id")
                or item.get("name")
            )
        else:
            sample_id = str(item)
        if sample_id:
            result.append(str(sample_id))
    return result


def _sample_project(gt_dir: Path) -> str:
    info = gt_dir / "sample_info.json"
    if not info.is_file():
        return ""
    try:
        return str(_load_json(info).get("project") or "").strip()
    except Exception:
        return ""


def _load_or_build_anchors(gt_dir: Path) -> list[dict[str, Any]]:
    frozen = gt_dir / "context" / "context_breakpoints.json"
    if frozen.is_file():
        data = json.loads(frozen.read_text(encoding="utf-8", errors="replace"))
        items = data.get("breakpoints") if isinstance(data, dict) else data
        return [item for item in items if isinstance(item, dict)]

    gt = _load_json(gt_dir / "ground_truth.json")
    anchors = extract_reachability_checkpoints(gt)
    anchors.extend(_fine_trace_anchors(gt))
    anchors.extend(_verified_invariant_anchors(gt_dir))
    return _dedupe_anchors(anchors)


def _fine_trace_anchors(gt: dict[str, Any]) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    for index, step in enumerate(gt.get("fine_trace") or [], 1):
        if not isinstance(step, dict):
            continue
        loc = _location(step)
        if not loc:
            continue
        loc["kind"] = "fine_trace"
        value = step.get("step")
        loc["fine_trace_step"] = value if isinstance(value, int) else index
        loc["role"] = step.get("role") or ""
        loc["note"] = step.get("note") or step.get("description") or ""
        anchors.append(loc)
    return anchors


def _verified_invariant_anchors(gt_dir: Path) -> list[dict[str, Any]]:
    path = gt_dir / "verified_invariants.json"
    if not path.is_file():
        return []
    try:
        data = _load_json(path)
    except Exception:
        return []
    anchors: list[dict[str, Any]] = []
    for node in data.get("nodes") or []:
        if not isinstance(node, dict) or node.get("verified") is False:
            continue
        loc = _location(node)
        if not loc:
            continue
        role = str(node.get("role") or "node")
        loc.update(
            {
                "kind": f"invariant_node:{role}",
                "invariant_id": node.get("invariant_id") or "",
                "role": role,
                "operands": node.get("operands") or [],
                "relation": node.get("relation") or {},
                "note": node.get("description") or "",
            }
        )
        anchors.append(loc)
    for edge in data.get("edges") or []:
        if not isinstance(edge, dict) or edge.get("verified") is False:
            continue
        for prefix in ("from", "to"):
            loc = _location(
                {
                    "file": edge.get(f"{prefix}_file"),
                    "function": edge.get(f"{prefix}_function"),
                    "line": edge.get(f"{prefix}_line"),
                }
            )
            if not loc:
                continue
            loc.update(
                {
                    "kind": f"invariant_edge:{prefix}",
                    "invariant_id": edge.get("invariant_id") or "",
                    "role": edge.get("type") or "edge",
                    "operands": edge.get("operands") or [],
                    "relation": edge.get("relation") or {},
                }
            )
            anchors.append(loc)
    return anchors


def _location(raw: dict[str, Any]) -> dict[str, Any]:
    file = str(raw.get("file") or "").strip()
    function = str(raw.get("function") or "").strip()
    line = raw.get("line")
    try:
        line = int(line) if line not in {None, ""} else None
    except (TypeError, ValueError):
        line = None
    if not file and not function:
        return {}
    return {
        "file": file,
        "function": function,
        "line": line,
        "code": str(raw.get("code") or raw.get("statement") or "").strip(),
    }


def _dedupe_anchors(anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for item in anchors:
        key = (item.get("kind"), item.get("file"), item.get("function"), item.get("line"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _normalize_file(path: str, source_root: str = "") -> str:
    value = str(path or "").replace("\\", "/")
    for marker in (
        "/gt/_work/src/",
        "/gt/_work/",
        "/workspace/repo-vul/src-vul/",
        "repo-vul/src-vul/",
    ):
        if marker in value:
            return value.split(marker, 1)[1].lstrip("/")
    if source_root and value.startswith(source_root.rstrip("/") + "/"):
        return value[len(source_root.rstrip("/")) + 1 :].lstrip("/")
    if value.startswith("/src/"):
        parts = value.strip("/").split("/")
        if len(parts) > 2:
            return "/".join(parts[2:])
    return value.lstrip("/")


def _context_from_events(events: list[dict[str, Any]], source_root: str) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, Any]] = set()
    context: list[dict[str, Any]] = []
    for event in events:
        frames = []
        hit = event.get("hit")
        if isinstance(hit, dict):
            frames.append(hit)
        stack = event.get("stack")
        if isinstance(stack, list):
            frames.extend(frame for frame in stack if isinstance(frame, dict))
        for frame in frames:
            function = str(frame.get("function") or "")
            file = _normalize_file(str(frame.get("file") or ""), source_root)
            if not function or not file:
                continue
            line = frame.get("line")
            key = (file, function, line)
            if key in seen:
                continue
            seen.add(key)
            item: dict[str, Any] = {"file": file, "function": function, "line": line}
            code = str(frame.get("code") or "").strip()
            if code:
                item["code"] = code
            context.append(item)
    return context



_SANITIZER_FRAME_RE = re.compile(
    r"^\s*#\d+\s+0x[0-9a-fA-F]+\s+in\s+(?P<function>.*?)\s+(?P<file>/(?:gt/_work|src|work)[^:\s]+):(?P<line>\d+)"
)



def _sanitize_sanitizer_function(value: str) -> str:
    text = str(value or "").strip()
    if "(" in text:
        text = text.split("(", 1)[0].rstrip()
    if "::" in text:
        tokens = text.split()
        for index, token in enumerate(tokens):
            if "::" in token:
                return " ".join(tokens[index:])
    return text


def _is_runtime_context_file(file: str) -> bool:
    normalized = file.replace("\\", "/")
    runtime_markers = (
        "llvm-project/",
        "compiler-rt/",
        "sanitizer_common/",
        "aflplusplus/",
        "aflpp_driver.c",
        "asan_",
        "/usr/include/",
    )
    return any(marker in normalized for marker in runtime_markers)


def _context_from_sanitizer_trace(gt_dir: Path, source_root: str) -> list[dict[str, Any]]:
    """Recover dynamic context from an existing real PoC sanitizer stack.

    This is a fallback for samples where GDB anchor breakpoints cannot collect
    events because the harness wrapper or fuzzer runtime stops before anchors are
    observed. It only consumes crash stacks emitted by running the frozen PoC.
    """
    candidates = [
        gt_dir / "sanitizer_trace.txt",
        gt_dir / "default_crash_trace.txt",
    ]
    for trace_path in candidates:
        if not trace_path.is_file():
            continue
        context: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int]] = set()
        for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _SANITIZER_FRAME_RE.search(line)
            if not match:
                continue
            raw_function = _sanitize_sanitizer_function(match.group("function"))
            file = _normalize_file(match.group("file"), source_root)
            try:
                lineno = int(match.group("line"))
            except ValueError:
                continue
            if not raw_function or not file or _is_runtime_context_file(file):
                continue
            # Sanitizer sometimes repeats inline frames with the same source
            # location. Keep the function spelling but avoid duplicate points.
            key = (file, raw_function, lineno)
            if key in seen:
                continue
            seen.add(key)
            context.append(
                {
                    "file": file,
                    "function": raw_function,
                    "line": lineno,
                    "kind": "sanitizer_backtrace",
                }
            )
        if context:
            return context
    return []


def _files_from_context(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for item in context:
        file = str(item.get("file") or "")
        function = str(item.get("function") or "")
        if not file or not function:
            continue
        grouped.setdefault(file, [])
        if function not in grouped[file]:
            grouped[file].append(function)
    return [{"file": file, "functions": funcs} for file, funcs in sorted(grouped.items())]


def _run_arvo_context(
    *, gt_dir: Path, repo_root: Path, anchors: list[dict[str, Any]], timeout: int, max_events: int, backtrace_limit: int
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    spec = compile_runtime_spec(gt_dir, require_artifacts=False)
    context_dir = gt_dir / "context"
    breakpoints_path = context_dir / "context_breakpoints.json"
    hits_path = context_dir / "context_hits.json"
    write_breakpoint_spec(anchors, breakpoints_path)
    _unlink_if_exists(hits_path)
    project = _sample_project(gt_dir)
    source_root = f"/src/{project}" if project else ""
    with prepare_arvo_target(spec.image, repo_root=repo_root) as prepared:
        gdb_script = repo_root / "evaluator" / "reachability" / "gdb_context_trace.py"
        bundled_gdb = [
            "/opt/reachability-gdb/lib64/ld-linux-x86-64.so.2",
            "--library-path",
            "/opt/reachability-gdb/lib/x86_64-linux-gnu:/opt/reachability-gdb/usr/lib/x86_64-linux-gnu",
            "/opt/reachability-gdb/usr/bin/gdb",
        ]
        env = [
            "HOME=/tmp",
            "PYTHONHOME=/opt/reachability-gdb/usr",
            "ASAN_OPTIONS=detect_leaks=0",
            f"CONTEXT_BREAKPOINTS={breakpoints_path}",
            f"CONTEXT_OUTPUT={hits_path}",
            f"CONTEXT_MAX_EVENTS={max_events}",
            f"CONTEXT_BACKTRACE_LIMIT={backtrace_limit}",
            f"CONTEXT_SOURCE_ROOT={source_root}",
        ]
        command = ["docker", "exec"]
        for item in env:
            command.extend(["-e", item])
        command.extend(
            [
                "-w",
                str(repo_root),
                prepared.container_id,
                *bundled_gdb,
                "--data-directory=/opt/reachability-gdb/usr/share/gdb",
                "--batch",
                "-q",
                "-x",
                str(gdb_script),
                "--args",
                *target_arguments(prepared, gt_dir / "poc"),
            ]
        )
        result = _run(command, timeout)
    return result, _load_context_hits(hits_path), {"source_root": source_root, "argv": command_after_args(result["command"])}


def _run_local_context(
    *, gt_dir: Path, repo_root: Path, anchors: list[dict[str, Any]], timeout: int, max_events: int, backtrace_limit: int
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    spec = compile_runtime_spec(gt_dir)
    context_dir = gt_dir / "context"
    breakpoints_path = context_dir / "context_breakpoints.json"
    hits_path = context_dir / "context_hits.json"
    write_breakpoint_spec(anchors, breakpoints_path)
    _unlink_if_exists(hits_path)
    _ensure_runtime_prepared(
        spec=spec,
        gt_dir=gt_dir,
        repo_root=repo_root,
        output_dir=context_dir,
        timeout=max(timeout, 1800),
    )
    candidate = str((gt_dir / "poc").resolve())
    arguments = [item.replace(spec.input_placeholder, candidate) for item in spec.arguments]
    executable_host = container_path_on_host(gt_dir, spec.executable, spec.workdir)
    gdb_executable, gdb_arguments = _gdb_invocation_for_runtime(
        executable=spec.executable,
        executable_host=executable_host,
        arguments=arguments,
    )
    gdb_script = repo_root / "evaluator" / "reachability" / "gdb_context_trace.py"
    source_root = "/gt/_work/src"
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--cap-add",
        "SYS_PTRACE",
        "--security-opt",
        "seccomp=unconfined",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/tmp",
        "-e",
        f"CONTEXT_BREAKPOINTS={breakpoints_path}",
        "-e",
        f"CONTEXT_OUTPUT={hits_path}",
        "-e",
        f"CONTEXT_MAX_EVENTS={max_events}",
        "-e",
        f"CONTEXT_BACKTRACE_LIMIT={backtrace_limit}",
        "-e",
        f"CONTEXT_SOURCE_ROOT={source_root}",
    ]
    for key, value in sorted(spec.environment.items()):
        command.extend(["-e", f"{key}={value}"])
    command.extend(
        [
            "-v",
            f"{repo_root}:{repo_root}",
            "-v",
            f"{gt_dir.resolve()}:/gt",
            "-w",
            spec.workdir,
            spec.image,
            "gdb",
            "--batch",
            "-q",
            "-x",
            str(gdb_script),
            "--args",
            gdb_executable,
            *gdb_arguments,
        ]
    )
    result = _run(command, timeout)
    return result, _load_context_hits(hits_path), {"source_root": source_root, "argv": command_after_args(result["command"])}


def command_after_args(command: list[str]) -> list[str]:
    try:
        index = command.index("--args")
        return command[index + 1 :]
    except ValueError:
        return []


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _run(command: list[str], timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "command": command,
            "returncode": 124,
            "stdout": stdout,
            "stderr": stderr + "\ncontext collection timed out\n",
        }


def _load_context_hits(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    events = data.get("events") if isinstance(data, dict) else data
    return [event for event in events or [] if isinstance(event, dict)]


def generate_one(
    sample_id: str,
    *,
    repo_root: Path,
    timeout: int,
    max_events: int,
    backtrace_limit: int,
    force: bool,
) -> dict[str, Any]:
    gt_dir = repo_root / "gt_results" / sample_id
    output = gt_dir / "context_gt.json"
    nested_output = gt_dir / "context" / "context_gt.json"
    if output.is_file() and not force:
        return {"sample": sample_id, "status": "skipped_existing"}
    if not (gt_dir / "poc").is_file():
        return {"sample": sample_id, "status": "missing_poc"}
    context_dir = gt_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    anchors = _load_or_build_anchors(gt_dir)
    if not anchors:
        return {"sample": sample_id, "status": "missing_anchors"}
    runner = _run_arvo_context if sample_id.startswith("arvo_") else _run_local_context
    try:
        result, events, meta = runner(
            gt_dir=gt_dir,
            repo_root=repo_root,
            anchors=anchors,
            timeout=timeout,
            max_events=max_events,
            backtrace_limit=backtrace_limit,
        )
    except Exception as exc:
        (context_dir / "context_error.txt").write_text(str(exc) + "\n", encoding="utf-8")
        return {"sample": sample_id, "status": "error", "error": str(exc)}
    (context_dir / "gdb_stdout.txt").write_text(result["stdout"], encoding="utf-8")
    (context_dir / "gdb_stderr.txt").write_text(result["stderr"], encoding="utf-8")
    source_root = str(meta.get("source_root") or "")
    context = _context_from_events(events, source_root)
    collection_mode = "anchor_backtrace"
    if not context:
        fallback_context = _context_from_sanitizer_trace(gt_dir, source_root)
        if fallback_context:
            context = fallback_context
            collection_mode = "sanitizer_backtrace_fallback"
    payload = {
        "schema_version": "gt-context-v1",
        "sample_id": sample_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collection": {
            "mode": collection_mode,
            "timeout_seconds": timeout,
            "max_events": max_events,
            "backtrace_limit": backtrace_limit,
            "anchor_count": len(anchors),
            "event_count": len(events),
            "truncated": len(events) >= max_events,
        },
        "debug_command": {
            "command": result["command"],
            "argv": meta.get("argv") or [],
            "environment": {
                "ASAN_OPTIONS": "detect_leaks=0",
                "CONTEXT_MAX_EVENTS": str(max_events),
                "CONTEXT_BACKTRACE_LIMIT": str(backtrace_limit),
                "CONTEXT_SOURCE_ROOT": str(meta.get("source_root") or ""),
            },
            "returncode": result["returncode"],
        },
        "anchors": anchors,
        "events": events,
        "context": context,
        "files": _files_from_context(context),
    }
    hits_path = context_dir / "context_hits.json"
    if hits_path.is_file():
        try:
            hits = json.loads(hits_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(hits, dict) and "final_stop" in hits:
                payload["final_stop"] = hits["final_stop"]
        except Exception:
            pass
    nested_output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    shutil.copy2(nested_output, output)
    if events:
        status = "ok"
    elif context:
        status = "ok_fallback"
    else:
        status = "no_events"
    return {
        "sample": sample_id,
        "status": status,
        "returncode": result["returncode"],
        "events": len(events),
        "context": len(context),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate context_gt.json for GT samples.")
    parser.add_argument("--gt-root", type=Path, default=Path("gt_results"))
    parser.add_argument("--valid-gt", type=Path, default=Path("gt_results/valid_gt.json"))
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--samples-file", type=Path)
    parser.add_argument("--missing-only", action="store_true", default=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-events", type=int, default=120)
    parser.add_argument("--backtrace-limit", type=int, default=24)
    args = parser.parse_args()

    repo_root = Path.cwd().resolve()
    if args.sample:
        sample_ids = args.sample
    elif args.samples_file:
        sample_ids = [
            line.strip()
            for line in args.samples_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        sample_ids = _valid_sample_ids(args.valid_gt)
    if args.missing_only and not args.force:
        sample_ids = [
            sample_id
            for sample_id in sample_ids
            if not (repo_root / "gt_results" / sample_id / "context_gt.json").is_file()
        ]
    if args.limit:
        sample_ids = sample_ids[: args.limit]
    print(json.dumps({"selected": len(sample_ids), "parallel": args.parallel}, ensure_ascii=False), flush=True)
    if args.parallel <= 1:
        for sample_id in sample_ids:
            print(json.dumps(generate_one(
                sample_id,
                repo_root=repo_root,
                timeout=args.timeout,
                max_events=args.max_events,
                backtrace_limit=args.backtrace_limit,
                force=args.force,
            ), ensure_ascii=False), flush=True)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {
            executor.submit(
                generate_one,
                sample_id,
                repo_root=repo_root,
                timeout=args.timeout,
                max_events=args.max_events,
                backtrace_limit=args.backtrace_limit,
                force=args.force,
            ): sample_id
            for sample_id in sample_ids
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {"sample": futures[future], "status": "error", "error": str(exc)}
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
