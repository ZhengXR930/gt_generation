"""Collect PoC execution context for a completed GT package.

`context_gt.json` is a runtime witness for the source files and functions a
PoC traverses on the way to the vulnerability.  It is deliberately cheaper than
full instruction tracing: the collector breaks at GT semantic anchors and stores
the bounded GDB backtrace at each anchor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .reachability import (
    _arvo_context,
    _proxy_environment,
    _repo_root,
    _restore_vulnerable_source,
    derive_debug_command,
)


SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".rs", ".go")
RUNTIME_MARKERS = (
    "/usr/",
    "/lib/",
    "llvm/projects/compiler-rt/",
    "compiler-rt/",
    "libfuzzer/",
    "__libc_start_main",
    "__sanitizer",
    "sanitizer_",
    "asan_",
    "msan_",
    "ubsan_",
)
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _engine_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    evaluator = root / "evaluator"
    extra = str(evaluator)
    env["PYTHONPATH"] = extra + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return env


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(str(item)) for item in argv)


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _location_key(item: dict[str, Any]) -> tuple[str, str, int | None]:
    return (
        _normalize_file(str(item.get("file") or "")),
        str(item.get("function") or ""),
        _to_int(item.get("line")),
    )


def _normalize_file(path: str) -> str:
    path = path.replace("\\", "/").strip()
    if "@" in path:
        path = path.split("@", 1)[0]
    for marker in ("/_work/src/", "/src/", "/source/", "/work/"):
        if marker in path:
            path = path.split(marker, 1)[1]
            break
    return path.strip("/")


def _normalize_location(raw: dict[str, Any], *, kind: str) -> dict[str, Any]:
    file = _normalize_file(str(raw.get("file") or ""))
    function = str(raw.get("function") or "").strip()
    line = _to_int(raw.get("line"))
    if not file and not function:
        return {}
    return {
        "kind": kind,
        "file": file,
        "function": function,
        "line": line,
        "code": str(raw.get("code") or raw.get("statement") or "").strip(),
        "note": str(raw.get("note") or raw.get("description") or "").strip(),
    }


def build_context_checkpoints(
    gt: dict[str, Any],
    verified_invariants: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic context breakpoints from GT semantic artifacts."""
    root = _repo_root()
    sys.path.insert(0, str(root / "evaluator"))
    from reachability.engine import extract_reachability_checkpoints

    checkpoints: list[dict[str, Any]] = []
    checkpoints.extend(extract_reachability_checkpoints(gt))

    for item in gt.get("fine_trace") or []:
        if isinstance(item, dict):
            location = _normalize_location(item, kind="fine_trace")
            if location:
                location["fine_trace_step"] = item.get("step")
                checkpoints.append(location)

    inv = verified_invariants or {}
    for node in inv.get("nodes") or []:
        if isinstance(node, dict):
            location = _normalize_location(
                node, kind=f"invariant_node:{node.get('role') or 'unknown'}"
            )
            if location:
                location["invariant_id"] = node.get("invariant_id")
                location["role"] = node.get("role")
                checkpoints.append(location)
    for edge in inv.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        for endpoint in ("from", "to"):
            location = _normalize_location(
                {
                    "file": edge.get(f"{endpoint}_file"),
                    "function": edge.get(f"{endpoint}_function"),
                    "line": edge.get(f"{endpoint}_line"),
                    "description": edge.get("description"),
                },
                kind=f"invariant_edge_{endpoint}",
            )
            if location:
                location["invariant_id"] = edge.get("invariant_id")
                location["edge_type"] = edge.get("type")
                checkpoints.append(location)

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None, str]] = set()
    for checkpoint in checkpoints:
        normalized = _normalize_location(
            checkpoint, kind=str(checkpoint.get("kind") or "context")
        )
        if not normalized:
            continue
        for key in ("event_point", "assertion_role", "fine_trace_step", "invariant_id", "role", "edge_type"):
            if key in checkpoint:
                normalized[key] = checkpoint.get(key)
        key = (*_location_key(normalized), str(normalized.get("kind") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _read_code_line(codebase: Path | None, file: str, line: int | None) -> str:
    if codebase is None or line is None or line < 1:
        return ""
    normalized = _normalize_file(file)
    suffix_candidates = []
    parts = normalized.split("/") if normalized else []
    for index in range(len(parts)):
        suffix = "/".join(parts[index:])
        if suffix:
            suffix_candidates.append(codebase / suffix)
    candidates = [
        codebase / file,
        codebase / normalized,
        codebase / Path(normalized).name,
        *suffix_candidates,
    ]
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if 0 <= line - 1 < len(lines):
            return lines[line - 1].strip()
    return ""


def _is_project_frame(frame: dict[str, Any]) -> bool:
    file = _normalize_file(str(frame.get("file") or ""))
    function = str(frame.get("function") or "")
    if not file or not function:
        return False
    lowered = (file + " " + function).lower()
    if any(marker in lowered for marker in RUNTIME_MARKERS):
        return False
    return file.endswith(SOURCE_SUFFIXES)


def _context_from_events(
    events: list[dict[str, Any]],
    *,
    codebase: Path | None,
) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None]] = set()
    for event in events:
        stack = event.get("stack") if isinstance(event.get("stack"), list) else []
        # GDB reports newest frame first. Reverse it so the resulting path reads
        # from entry/admission toward the currently observed vulnerability point.
        for frame in reversed([item for item in stack if isinstance(item, dict)]):
            if not _is_project_frame(frame):
                continue
            item = {
                "file": _normalize_file(str(frame.get("file") or "")),
                "function": str(frame.get("function") or ""),
                "line": _to_int(frame.get("line")),
            }
            key = _location_key(item)
            if key in seen:
                continue
            seen.add(key)
            code = _read_code_line(codebase, item["file"], item["line"])
            if not code:
                code = str(frame.get("code") or "").strip()
            if code:
                item["code"] = code
            context.append(item)
    return context


def _files_summary(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_file: dict[str, list[str]] = {}
    for item in context:
        file = str(item.get("file") or "")
        function = str(item.get("function") or "")
        if not file:
            continue
        functions = by_file.setdefault(file, [])
        if function and function not in functions:
            functions.append(function)
    return [
        {"file": file, "functions": functions}
        for file, functions in sorted(by_file.items())
    ]


def _load_context_hits(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"events": [], "final_stop": {}, "truncated": False}
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        return {"events": [], "final_stop": {}, "truncated": False}
    return data


def collect_context_gt(
    *,
    gt_path: Path,
    debug_command: str,
    poc: Path | None,
    out_dir: Path,
    codebase: Path | None = None,
    verified_invariants_path: Path | None = None,
    timeout: int = 120,
    max_events: int = 200,
    backtrace_limit: int = 32,
) -> dict[str, Any]:
    root = _repo_root()
    sys.path.insert(0, str(root / "evaluator"))
    from reachability.engine import write_breakpoint_spec

    gt = _load_json(gt_path)
    verified = (
        _load_json(verified_invariants_path)
        if verified_invariants_path and verified_invariants_path.is_file()
        else {}
    )
    checkpoints = build_context_checkpoints(gt, verified)
    out_dir.mkdir(parents=True, exist_ok=True)
    breakpoints_path = out_dir / "context_breakpoints.json"
    hits_path = out_dir / "context_hits.json"
    report_path = out_dir / "context_gt.json"
    write_breakpoint_spec(checkpoints, breakpoints_path)

    command = _format_command(debug_command, poc)
    command_env, argv = _split_command_env(command)
    if not argv:
        raise ValueError("empty debug command")
    env = _engine_env(root)
    env.update(command_env)
    env["CONTEXT_BREAKPOINTS"] = str(breakpoints_path)
    env["CONTEXT_OUTPUT"] = str(hits_path)
    env["CONTEXT_MAX_EVENTS"] = str(max_events)
    env["CONTEXT_BACKTRACE_LIMIT"] = str(backtrace_limit)
    if codebase is not None:
        env["CONTEXT_SOURCE_ROOT"] = str(codebase)
    full_command = [
        "gdb",
        "--batch",
        "-q",
        "-x",
        str(root / "evaluator" / "reachability" / "gdb_context_trace.py"),
        "--args",
        *argv,
    ]
    proc = subprocess.run(
        full_command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    (out_dir / "gdb_stdout.txt").write_text(proc.stdout, encoding="utf-8", errors="replace")
    (out_dir / "gdb_stderr.txt").write_text(proc.stderr, encoding="utf-8", errors="replace")
    hits = _load_context_hits(hits_path)
    events = [item for item in hits.get("events", []) if isinstance(item, dict)]
    context = _context_from_events(events, codebase=codebase)
    report = {
        "schema_version": "gt-context-v1",
        "sample_id": str(gt.get("sample_id") or ""),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collection": {
            "mode": "anchor_backtrace",
            "timeout_seconds": timeout,
            "max_events": max_events,
            "backtrace_limit": backtrace_limit,
            "anchor_count": len(checkpoints),
            "event_count": len(events),
            "truncated": bool(hits.get("truncated")),
        },
        "debug_command": {
            "command": full_command,
            "argv": argv,
            "environment": command_env,
            "returncode": proc.returncode,
        },
        "anchors": checkpoints,
        "events": events,
        "context": context,
        "files": _files_summary(context),
        "final_stop": hits.get("final_stop") if isinstance(hits.get("final_stop"), dict) else {},
    }
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def _format_command(template: str, poc: Path | None) -> str:
    if poc is None:
        return template
    return template.replace("{poc}", str(poc))


def _split_command_env(command: str) -> tuple[dict[str, str], list[str]]:
    parts = shlex.split(command)
    env: dict[str, str] = {}
    if parts and parts[0] == "env":
        parts = parts[1:]
    index = 0
    while index < len(parts) and _ENV_ASSIGNMENT_RE.match(parts[index]):
        key, value = parts[index].split("=", 1)
        env[key] = value
        index += 1
    return env, parts[index:]


def _debug_command_from_runtime_spec(result_dir: Path) -> str:
    path = result_dir / "runtime_spec.json"
    if not path.is_file():
        return ""
    try:
        spec = _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    env = spec.get("environment") if isinstance(spec.get("environment"), dict) else {}
    argv: list[str] = []
    for key, value in sorted(env.items()):
        argv.append(f"{key}={value}")
    executable = str(spec.get("executable") or "")
    if not executable:
        return ""
    argv.append(executable)
    for arg in spec.get("arguments") or []:
        argv.append(str(arg).replace("{poc}", "/gt/poc"))
    return _shell_join(argv)


def _debug_command_from_reachability_report(result_dir: Path) -> str:
    path = result_dir / "reachability_report.json"
    if not path.is_file():
        return ""
    try:
        report = _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    command = ((report.get("debug_command") or {}).get("command") or [])
    if not isinstance(command, list):
        return ""
    try:
        index = command.index("--args")
    except ValueError:
        return ""
    argv = [str(item) for item in command[index + 1:]]
    if not argv:
        return ""
    host_prefixes = [
        str(result_dir.resolve()),
        f"/mnt/datastore/gt_generation/gt_results/{result_dir.name}",
        f"/data00/home/zhengxinran/Documents/trae_projects/test/gt_generation/gt_results/{result_dir.name}",
    ]
    rewritten: list[str] = []
    for item in argv:
        value = item
        for prefix in host_prefixes:
            if value == prefix:
                value = "/gt"
                break
            if value.startswith(prefix + "/"):
                value = "/gt/" + value[len(prefix) + 1:]
                break
        if value.endswith("/poc"):
            value = "/gt/poc"
        rewritten.append(value)
    return _shell_join(rewritten)


def _debug_command_for_result_dir(result_dir: Path) -> str:
    for supplier in (
        _debug_command_from_runtime_spec,
        _debug_command_from_reachability_report,
        derive_debug_command,
    ):
        command = supplier(result_dir)
        if command:
            return command
    return ""


def _copy_context_output(result_dir: Path) -> bool:
    produced = result_dir / "context" / "context_gt.json"
    if not produced.is_file():
        return False
    (result_dir / "context_gt.json").write_text(
        produced.read_text(encoding="utf-8", errors="replace"),
        encoding="utf-8",
    )
    return True


def _result_context_paths(result_dir: Path) -> dict[str, Path]:
    out_dir = result_dir / "context"
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "out_dir": out_dir,
        "breakpoints": out_dir / "context_breakpoints.json",
        "hits": out_dir / "context_hits.json",
        "report": out_dir / "context_gt.json",
        "stdout": out_dir / "gdb_stdout.txt",
        "stderr": out_dir / "gdb_stderr.txt",
    }


def _prepare_result_context(result_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Path]]:
    root = _repo_root()
    sys.path.insert(0, str(root / "evaluator"))
    from reachability.engine import write_breakpoint_spec

    gt = _load_json(result_dir / "ground_truth.json")
    verified_path = result_dir / "verified_invariants.json"
    verified = _load_json(verified_path) if verified_path.is_file() else {}
    checkpoints = build_context_checkpoints(gt, verified)
    paths = _result_context_paths(result_dir)
    write_breakpoint_spec(checkpoints, paths["breakpoints"])
    return gt, checkpoints, paths


def _write_context_gt_report(
    *,
    result_dir: Path,
    gt: dict[str, Any],
    checkpoints: list[dict[str, Any]],
    hits_path: Path,
    report_path: Path,
    command: list[str],
    command_env: dict[str, str],
    returncode: int,
    timeout: int,
    max_events: int,
    backtrace_limit: int,
    codebase: Path | None,
) -> dict[str, Any]:
    hits = _load_context_hits(hits_path)
    events = [item for item in hits.get("events", []) if isinstance(item, dict)]
    context = _context_from_events(events, codebase=codebase)
    report = {
        "schema_version": "gt-context-v1",
        "sample_id": str(gt.get("sample_id") or result_dir.name),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collection": {
            "mode": "anchor_backtrace",
            "timeout_seconds": timeout,
            "max_events": max_events,
            "backtrace_limit": backtrace_limit,
            "anchor_count": len(checkpoints),
            "event_count": len(events),
            "truncated": bool(hits.get("truncated")),
        },
        "debug_command": {
            "command": command,
            "argv": command[command.index("--args") + 1:] if "--args" in command else command,
            "environment": command_env,
            "returncode": returncode,
        },
        "anchors": checkpoints,
        "events": events,
        "context": context,
        "files": _files_summary(context),
        "final_stop": hits.get("final_stop") if isinstance(hits.get("final_stop"), dict) else {},
    }
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def _run_repo_result_dir(result_dir: Path, timeout: int, max_events: int, backtrace_limit: int) -> int:
    build_sh = result_dir / "build.sh"
    if not build_sh.is_file():
        print(json.dumps({"context_gt": "skipped", "reason": "no build.sh"}))
        return 0
    command = _debug_command_for_result_dir(result_dir)
    if not command:
        print(json.dumps({"context_gt": "skipped", "reason": "no debug command"}))
        return 0
    restored = _restore_vulnerable_source(result_dir)
    arguments = [
        "PYTHONPATH=/repo/gt_generation:/repo",
        "python3",
        "-m",
        "gt_toolkit",
        "context",
        "--gt",
        "/gt/ground_truth.json",
        "--codebase",
        "/gt/_work/src",
        "--verified-invariants",
        "/gt/verified_invariants.json",
        "--debug-command",
        shlex.quote(command),
        "--poc",
        "/gt/poc",
        "--out-dir",
        "/gt/context",
        "--timeout",
        str(timeout),
        "--max-events",
        str(max_events),
        "--backtrace-limit",
        str(backtrace_limit),
    ]
    proc = subprocess.run(
        [str(build_sh), " ".join(arguments)],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=max(timeout + 60, 180),
    )
    ok = _copy_context_output(result_dir)
    print(json.dumps({
        "context_gt": "ran" if ok else "failed",
        "track": "repo",
        "returncode": proc.returncode,
        "source_restored": restored,
        "stderr": "" if ok else proc.stderr[-600:],
    }))
    return 0 if ok else 1


def _run_arvo_result_dir(result_dir: Path, timeout: int, max_events: int, backtrace_limit: int) -> int:
    context = _arvo_context(result_dir)
    if context is None:
        return 1
    target = context["target"]
    binary = f"/out/{target}" if target else (
        "$(set -- /out/*; for f; do [ -x \"$f\" ] && [ -f \"$f\" ] && echo \"$f\"; done | head -1)"
    )
    repo_root = _repo_root()
    gt, checkpoints, paths = _prepare_result_context(result_dir)
    gdb_command = [
        "gdb",
        "--batch",
        "-q",
        "-x",
        "/repo/evaluator/reachability/gdb_context_trace.py",
        "--args",
        binary,
        "/gt/poc",
    ]
    inner = (
        "set -e\n"
        "if ! command -v gdb >/dev/null 2>&1; then\n"
        "  apt-get update -qq >/dev/null 2>&1 || true\n"
        "  apt-get install -y -qq gdb >/dev/null 2>&1 || true\n"
        "fi\n"
        "command -v gdb >/dev/null 2>&1 || { echo 'no gdb in image' >&2; exit 3; }\n"
        f"SRC_ROOT=/src/{shlex.quote(context['project'])}\n"
        'if [ ! -d \"$SRC_ROOT\" ]; then\n'
        "  SRC_ROOT=$(for d in /src/*/; do [ -d \"$d/.git\" ] && { echo \"${d%/}\"; break; }; done)\n"
        "fi\n"
        'if [ -z \"$SRC_ROOT\" ] || [ ! -d \"$SRC_ROOT\" ]; then SRC_ROOT=/src; fi\n'
        "export CONTEXT_BREAKPOINTS=/gt/context/context_breakpoints.json\n"
        "export CONTEXT_OUTPUT=/gt/context/context_hits.json\n"
        f"export CONTEXT_MAX_EVENTS={max_events}\n"
        f"export CONTEXT_BACKTRACE_LIMIT={backtrace_limit}\n"
        "export CONTEXT_SOURCE_ROOT=\"$SRC_ROOT\"\n"
        + _shell_join(gdb_command) + "\n"
        f"chown -R {os.getuid()}:{os.getgid()} /gt/context 2>/dev/null || true\n"
    )
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "bash",
            *_proxy_environment(),
            "-v",
            f"{repo_root}:/repo:ro",
            "-v",
            f"{result_dir}:/gt",
            context["image"],
            "-lc",
            inner,
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=max(timeout + 600, 900),
    )
    paths["stdout"].write_text(proc.stdout, encoding="utf-8", errors="replace")
    paths["stderr"].write_text(proc.stderr, encoding="utf-8", errors="replace")
    report = _write_context_gt_report(
        result_dir=result_dir,
        gt=gt,
        checkpoints=checkpoints,
        hits_path=paths["hits"],
        report_path=paths["report"],
        command=[
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "bash",
            "-v",
            f"{repo_root}:/repo:ro",
            "-v",
            f"{result_dir}:/gt",
            context["image"],
            "-lc",
            inner,
        ],
        command_env={},
        returncode=proc.returncode,
        timeout=timeout,
        max_events=max_events,
        backtrace_limit=backtrace_limit,
        codebase=None,
    )
    ok = bool(report.get("context")) and _copy_context_output(result_dir)
    print(json.dumps({
        "context_gt": "ran" if ok else "failed",
        "track": "arvo",
        "returncode": proc.returncode,
        "stderr": "" if ok else proc.stderr[-600:],
    }))
    return 0 if ok else 1


def run_for_result_dir(
    result_dir: Path,
    *,
    timeout: int = 120,
    max_events: int = 200,
    backtrace_limit: int = 32,
) -> int:
    result_dir = result_dir.resolve()
    build_sh = result_dir / "build.sh"
    if not build_sh.is_file():
        print(json.dumps({"context_gt": "skipped", "reason": "no build.sh"}))
        return 0
    text = build_sh.read_text(encoding="utf-8", errors="replace")
    if "gt-memory-env" not in text:
        return _run_arvo_result_dir(result_dir, timeout, max_events, backtrace_limit)
    return _run_repo_result_dir(result_dir, timeout, max_events, backtrace_limit)


def context_gt_errors(result_dir: Path, context_gt: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if context_gt.get("schema_version") != "gt-context-v1":
        errors.append("context_gt.json has unsupported schema_version")
    sample_id = str(context_gt.get("sample_id") or "")
    if sample_id and sample_id != result_dir.name:
        errors.append("context_gt.json sample_id does not match package")
    collection = context_gt.get("collection")
    if not isinstance(collection, dict):
        errors.append("context_gt.json collection must be an object")
    context = context_gt.get("context")
    if not isinstance(context, list):
        errors.append("context_gt.json context must be a list")
    elif not context:
        errors.append("context_gt.json context is empty")
    else:
        for index, item in enumerate(context):
            if not isinstance(item, dict):
                errors.append(f"context_gt.json context[{index}] must be an object")
                continue
            if not str(item.get("file") or ""):
                errors.append(f"context_gt.json context[{index}] missing file")
            if not str(item.get("function") or ""):
                errors.append(f"context_gt.json context[{index}] missing function")
            line = _to_int(item.get("line"))
            if line is None or line < 1:
                errors.append(f"context_gt.json context[{index}] missing positive line")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gt-toolkit context",
        description="Collect PoC execution context as context_gt.json.",
    )
    parser.add_argument("--gt", type=Path)
    parser.add_argument("--poc", type=Path)
    parser.add_argument("--debug-command")
    parser.add_argument("--codebase", type=Path)
    parser.add_argument("--verified-invariants", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("context"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-events", type=int, default=200)
    parser.add_argument("--backtrace-limit", type=int, default=32)
    parser.add_argument(
        "--for-result-dir",
        type=Path,
        help="Run context collection for one completed GT result package.",
    )
    args = parser.parse_args(argv)
    if args.for_result_dir:
        return run_for_result_dir(
            args.for_result_dir,
            timeout=args.timeout,
            max_events=args.max_events,
            backtrace_limit=args.backtrace_limit,
        )
    if not args.gt or not args.debug_command:
        parser.error("--gt and --debug-command are required unless --for-result-dir is given")
    try:
        report = collect_context_gt(
            gt_path=args.gt,
            debug_command=args.debug_command,
            poc=args.poc,
            out_dir=args.out_dir,
            codebase=args.codebase,
            verified_invariants_path=args.verified_invariants,
            timeout=args.timeout,
            max_events=args.max_events,
            backtrace_limit=args.backtrace_limit,
        )
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
