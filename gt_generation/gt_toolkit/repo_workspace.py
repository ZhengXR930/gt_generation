"""Repo-track instrumentation gates run through a result directory build.sh."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_to_or_none(path: Path, parent: Path) -> Path | None:
    try:
        return path.relative_to(parent)
    except ValueError:
        return None


def _remote_result_path(result_dir: Path, path: Path) -> str:
    relative = _relative_to_or_none(path.resolve(), result_dir.resolve())
    if relative is None:
        raise ValueError(f"path must be inside result_dir so build.sh can mount it: {path}")
    return f"/gt/{relative.as_posix()}"


def _write_log(result_dir: Path, name: str, proc: subprocess.CompletedProcess[str]) -> None:
    log_dir = result_dir / "repo_workspace"
    log_dir.mkdir(parents=True, exist_ok=True)
    text = (
        f"returncode={proc.returncode}\n\n"
        f"## stdout\n{proc.stdout or ''}\n\n"
        f"## stderr\n{proc.stderr or ''}"
    )
    (log_dir / name).write_text(text, encoding="utf-8", errors="replace")


def _inner_from_recorded_build_command(recorded: str) -> str:
    if not recorded.strip():
        return ""
    try:
        parts = shlex.split(recorded)
    except ValueError:
        return recorded
    if parts and parts[0].endswith("build.sh") and len(parts) >= 2:
        return parts[1]
    return recorded


def _run_build_sh(result_dir: Path, inner: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(result_dir / "build.sh"), inner],
        cwd=result_dir,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _crashed(proc: subprocess.CompletedProcess[str]) -> bool:
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode != 0 and any(
        marker in combined
        for marker in (
            "AddressSanitizer",
            "MemorySanitizer",
            "runtime error:",
            "SEGV",
            "ABORTING",
        )
    )


def _trace_result(proc: subprocess.CompletedProcess[str]) -> str:
    if _crashed(proc):
        return "crash"
    if proc.returncode == 0:
        return "clean"
    return "error"


def _preserved_setup_sources(setup: str) -> list[str]:
    """Untracked source helpers created by Stage 01 are part of the build recipe."""
    suffixes = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")
    found: list[str] = []
    for match in re.finditer(r"/gt/_work/src/([^\s'\"`$;|&<>]+)", setup):
        path = match.group(1)
        if path.endswith(suffixes) and path not in found:
            found.append(path)
    return found


def _strip_patch_header_path(raw: str) -> tuple[str, str]:
    path, separator, suffix = raw.strip().partition("\t")
    if not separator:
        path, separator, suffix = raw.strip().partition(" ")
    return path.strip(), separator, suffix


def _find_unique_source_by_basename(repo_root: Path, basename: str) -> str | None:
    candidates = [
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob(basename)
        if path.is_file()
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _repo_relative_patch_path(result_dir: Path, raw_path: str) -> str | None:
    path = raw_path.strip().strip('"')
    if not path or path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        return path[2:]
    if path.startswith("./"):
        path = path[2:]

    repo_root = result_dir / "_work" / "src"
    prefixes = ("/gt/_work/src/", "_work/src/", "repo-vul/src-vul/", "src-vul/")
    for prefix in prefixes:
        if prefix in path:
            candidate = path.split(prefix, 1)[1]
            if (repo_root / candidate).exists():
                return candidate
    if not Path(path).is_absolute() and (repo_root / path).exists():
        return path

    parts = Path(path).parts
    for start in range(len(parts)):
        candidate = Path(*parts[start:]).as_posix()
        if candidate.startswith("/"):
            continue
        if candidate and (repo_root / candidate).exists():
            return candidate

    basename = Path(path).name
    for suffix in (".orig", ".original", ".old", ".new", ".tmp"):
        if basename.endswith(suffix):
            basename = basename[: -len(suffix)]
            break
    if basename:
        return _find_unique_source_by_basename(repo_root, basename)
    return None


def _normalize_repo_patch(result_dir: Path, patch: Path) -> Path:
    text = patch.read_text(encoding="utf-8", errors="replace")
    changed = False
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(("--- ", "+++ ")):
            prefix = line[:4]
            raw_path, separator, suffix = _strip_patch_header_path(line[4:])
            relative = _repo_relative_patch_path(result_dir, raw_path)
            if relative and relative != raw_path:
                if relative == "/dev/null":
                    patch_path = relative
                elif prefix == "--- ":
                    patch_path = f"a/{relative}"
                else:
                    patch_path = f"b/{relative}"
                line = prefix + patch_path + (separator + suffix if separator else "")
                changed = True
        elif line.startswith("diff --git "):
            parts = line.split()
            if len(parts) == 4:
                left = _repo_relative_patch_path(result_dir, parts[2])
                right = _repo_relative_patch_path(result_dir, parts[3])
                if left and right:
                    line = f"diff --git a/{left} b/{right}"
                    changed = True
        lines.append(line)

    if not changed:
        return patch
    normalized_dir = result_dir / "repo_workspace"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    normalized = normalized_dir / f"{patch.stem}.repo-normalized{patch.suffix}"
    normalized.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return normalized


def _clean_command(preserved_sources: list[str]) -> str:
    excludes = " ".join(
        f"-e {shlex.quote(path)}" for path in preserved_sources
    )
    return f"git reset --hard HEAD && git clean -fdq {excludes}".strip()


def _changed_sources_from_patch(patch: Path) -> list[str]:
    try:
        text = patch.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    result: list[str] = []
    for line in text.splitlines():
        if not line.startswith("+++ "):
            continue
        path, _separator, _suffix = _strip_patch_header_path(line[len("+++ "):])
        if path.startswith("b/"):
            path = path[len("b/"):]
        if path and path != "/dev/null" and path not in result:
            result.append(path)
    return result


def _stale_build_cleanup_command(changed_sources: list[str]) -> str:
    commands: list[str] = []
    for source in changed_sources:
        path = Path(source)
        suffix = path.suffix
        if suffix not in {".c", ".cc", ".cpp", ".cxx"}:
            continue
        obj = str(path.with_suffix(".o"))
        commands.append(f"rm -f {shlex.quote(obj)}")
    # Common static libraries and fuzzer binaries keep old objects even when the
    # project setup command only rebuilds a leaf target.
    commands.append(
        "(find fuzz -maxdepth 1 -type f -perm -111 -name 'fuzz*' -delete 2>/dev/null || true)"
    )
    return " && ".join(commands) if commands else ":"


def _reset_command(
    version: str,
    vulnerable_commit: str,
    fix_commit: str,
    preserved_sources: list[str],
) -> str:
    if not vulnerable_commit:
        raise ValueError("repo instrumentation requires sample_info.vulnerable_commit")
    if version == "vulnerable":
        return (
            f"{_clean_command(preserved_sources)} && "
            f"git checkout -q {shlex.quote(vulnerable_commit)}"
        )
    if not fix_commit:
        raise ValueError("fixed repo instrumentation requires sample_info.fix_commit")
    return (
        f"{_clean_command(preserved_sources)} && "
        f"git checkout -q {shlex.quote(fix_commit)}"
    )


def _report_template(
    *,
    result_dir: Path,
    version: str,
    patch: Path,
    spec: dict[str, Any],
    apply_proc: subprocess.CompletedProcess[str],
    compile_proc: subprocess.CompletedProcess[str],
    track: str,
) -> dict[str, Any]:
    return {
        "schema_version": "instrumentation-side-preflight-v1",
        "sample_id": str(spec.get("sample_id") or result_dir.name),
        "version": version,
        "track": track,
        "assertion_content_hash": spec.get("content_hash"),
        "ok": apply_proc.returncode == 0 and compile_proc.returncode == 0,
        "check": {
            "patch": patch.name,
            "patch_sha256": _sha256(patch),
            "apply_returncode": apply_proc.returncode,
            "compile_returncode": compile_proc.returncode,
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def validate_instrumentation_side(
    result_dir: Path,
    version: str,
    patch: Path,
    out: Path,
    timeout: int = 7200,
) -> dict[str, Any]:
    result_dir = result_dir.resolve()
    patch = patch.resolve()
    out = out.resolve()
    prepare = _load(result_dir / "prepare_report.json")
    track = str(prepare.get("track") or "")
    if not track.startswith("repo/"):
        raise ValueError("repo-workspace requires a repo-track prepare_report.json")
    sample_info = _load(result_dir / "sample_info.json")
    reproduction = _load(result_dir / "reproduction_report.json")
    spec = _load(result_dir / "candidate_assertions.json")

    build_sh = result_dir / "build.sh"
    if not build_sh.is_file():
        raise ValueError("repo-workspace requires build.sh")

    fix_commit = str(
        sample_info.get("fix_commit")
        or sample_info.get("fixed_commit")
        or ""
    ).strip()
    vulnerable_commit = str(
        sample_info.get("vulnerable_commit")
        or sample_info.get("vul_commit")
        or ""
    ).strip()
    setup = _inner_from_recorded_build_command(
        str(reproduction.get("setup_command") or "")
    )
    if not setup.strip():
        raise ValueError("repo-workspace requires reproduction_report.setup_command")
    preserved_sources = _preserved_setup_sources(setup)

    apply_patch = _normalize_repo_patch(result_dir, patch)
    relative_patch = _relative_to_or_none(apply_patch, result_dir)
    remote_patch = f"/gt/{relative_patch.as_posix()}" if relative_patch else f"/gt/{apply_patch.name}"
    cleanup_changed = _stale_build_cleanup_command(_changed_sources_from_patch(apply_patch))
    apply_inner = " && ".join(
        [
            "set -euo pipefail",
            _reset_command(version, vulnerable_commit, fix_commit, preserved_sources),
            f"git apply --check {shlex.quote(remote_patch)}",
            f"git apply {shlex.quote(remote_patch)}",
            cleanup_changed,
        ]
    )
    apply_proc = _run_build_sh(result_dir, apply_inner, timeout)
    _write_log(result_dir, f"plan_{version}_apply.log", apply_proc)

    if apply_proc.returncode == 0:
        compile_proc = _run_build_sh(result_dir, setup, timeout)
    else:
        compile_proc = subprocess.CompletedProcess(
            args=[str(build_sh), setup],
            returncode=1,
            stdout="",
            stderr="not started because apply failed",
        )
    _write_log(result_dir, f"plan_{version}_compile.log", compile_proc)

    report = _report_template(
        result_dir=result_dir,
        version=version,
        patch=patch,
        spec=spec,
        apply_proc=apply_proc,
        compile_proc=compile_proc,
        track=track,
    )
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def run_case(
    result_dir: Path,
    version: str,
    patch: Path,
    expect: str,
    case_name: str = "original",
    append_trace: bool = False,
    poc: Path | None = None,
    timeout: int = 7200,
) -> dict[str, Any]:
    result_dir = result_dir.resolve()
    patch = patch.resolve()
    prepare = _load(result_dir / "prepare_report.json")
    track = str(prepare.get("track") or "")
    if not track.startswith("repo/"):
        raise ValueError("repo-workspace requires a repo-track prepare_report.json")
    sample_info = _load(result_dir / "sample_info.json")
    reproduction = _load(result_dir / "reproduction_report.json")

    setup = _inner_from_recorded_build_command(
        str(reproduction.get("setup_command") or "")
    )
    command = _inner_from_recorded_build_command(
        str(reproduction.get("command") or "")
    )
    if not setup.strip():
        raise ValueError("repo-workspace requires reproduction_report.setup_command")
    if not command.strip():
        raise ValueError("repo-workspace requires reproduction_report.command")

    fix_commit = str(
        sample_info.get("fix_commit")
        or sample_info.get("fixed_commit")
        or ""
    ).strip()
    vulnerable_commit = str(
        sample_info.get("vulnerable_commit")
        or sample_info.get("vul_commit")
        or ""
    ).strip()
    preserved_sources = _preserved_setup_sources(setup)
    apply_patch = _normalize_repo_patch(result_dir, patch)
    relative_patch = _relative_to_or_none(apply_patch, result_dir)
    remote_patch = f"/gt/{relative_patch.as_posix()}" if relative_patch else f"/gt/{apply_patch.name}"
    poc_name = "poc"
    if poc is not None:
        poc = poc.resolve()
        if not poc.is_file():
            raise ValueError(f"PoC override does not exist: {poc}")
        relative_poc = _relative_to_or_none(poc, result_dir)
        if relative_poc is None:
            raise ValueError(f"PoC override must be inside result_dir: {poc}")
        remote_poc = _remote_result_path(result_dir, poc)
        command = command.replace("/gt/poc", shlex.quote(remote_poc))
        poc_name = relative_poc.as_posix()
    cleanup_changed = _stale_build_cleanup_command(_changed_sources_from_patch(apply_patch))
    inner = " && ".join(
        [
            "set -euo pipefail",
            _reset_command(version, vulnerable_commit, fix_commit, preserved_sources),
            f"git apply {shlex.quote(remote_patch)}",
            cleanup_changed,
            setup,
            command,
        ]
    )
    proc = _run_build_sh(result_dir, inner, timeout)
    _write_log(result_dir, f"{version}_{case_name}_run.log", proc)

    trace_path = result_dir / f"{version}_assertion_trace.txt"
    trace_mode = "a" if append_trace else "w"
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    result = _trace_result(proc)
    with trace_path.open(trace_mode, encoding="utf-8") as trace:
        trace.write(f"CASE name={case_name} rc={proc.returncode} result={result}\n")
        trace.write(combined)
        if combined and not combined.endswith("\n"):
            trace.write("\n")
        trace.write("ENDCASE\n")

    matched = (
        expect == "any"
        or (expect == "crash" and result == "crash")
        or (expect == "clean" and result == "clean")
    )
    report = {
        "schema_version": "repo-run-v1",
        "sample_id": str(sample_info.get("sample_id") or result_dir.name),
        "version": version,
        "track": track,
        "case_name": case_name,
        "expect": expect,
        "returncode": proc.returncode,
        "result": result,
        "matched": matched,
        "patch": patch.name,
        "patch_sha256": _sha256(patch),
        "poc": poc_name,
        "trace": trace_path.name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    (result_dir / "repo_workspace" / f"{version}_{case_name}_run.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gt-toolkit repo-workspace", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p_side = sub.add_parser("validate-instrumentation-side")
    p_side.add_argument("--result-dir", required=True, type=Path)
    p_side.add_argument("--version", required=True, choices=["vulnerable", "fixed"])
    p_side.add_argument("--patch", required=True, type=Path)
    p_side.add_argument("--out", required=True, type=Path)
    p_side.add_argument("--timeout", type=int, default=7200)
    p_run = sub.add_parser("run")
    p_run.add_argument("--result-dir", required=True, type=Path)
    p_run.add_argument("--version", required=True, choices=["vulnerable", "fixed"])
    p_run.add_argument("--patch", required=True, type=Path)
    p_run.add_argument("--expect", default="any", choices=["crash", "clean", "any"])
    p_run.add_argument("--case-name", default="original")
    p_run.add_argument("--append-trace", action="store_true")
    p_run.add_argument(
        "--poc",
        type=Path,
        help="Optional result-dir-local PoC override for a single perturbation case.",
    )
    p_run.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args(argv)
    if args.action == "validate-instrumentation-side":
        report = validate_instrumentation_side(
            args.result_dir,
            args.version,
            args.patch,
            args.out,
            timeout=args.timeout,
        )
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1
    if args.action == "run":
        report = run_case(
            args.result_dir,
            args.version,
            args.patch,
            args.expect,
            case_name=args.case_name,
            append_trace=args.append_trace,
            poc=args.poc,
            timeout=args.timeout,
        )
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["matched"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
