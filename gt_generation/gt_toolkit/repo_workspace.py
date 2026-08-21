"""Repo-track instrumentation gates run through a result directory build.sh."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .instrumentation_quality import validate_instrumentation_runtime_fields


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


def _write_log(
    result_dir: Path,
    name: str,
    proc: subprocess.CompletedProcess[str],
    *,
    header: str = "",
) -> None:
    log_dir = result_dir / "repo_workspace"
    log_dir.mkdir(parents=True, exist_ok=True)
    prefix = header.rstrip() + "\n\n" if header.strip() else ""
    text = "".join([
        prefix,
        f"returncode={proc.returncode}\n\n",
        f"## stdout\n{proc.stdout or ''}\n\n",
        f"## stderr\n{proc.stderr or ''}",
    ])
    (log_dir / name).write_text(text, encoding="utf-8", errors="replace")


def _current_checkout_commit(result_dir: Path) -> str:
    src = result_dir / "_work" / "src"
    if not (src / ".git").exists():
        return ""
    proc = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


@contextmanager
def _workspace_lock(result_dir: Path):
    """Serialize repo-track operations that mutate the shared checkout."""
    lock_dir = result_dir / "repo_workspace"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".repo_workspace.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


_ENV_ASSIGNMENT_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _inner_from_recorded_build_command(recorded: str) -> str:
    if not recorded.strip():
        return ""
    stripped = recorded.strip()
    build_match = re.search(r"(?:^|\s)(?:\S*/)?build\.sh(?:\s|$)", stripped)
    if build_match:
        after = stripped[build_match.end():].strip()
        if len(after) >= 2 and after[0] == after[-1] and after[0] in {"'", '"'}:
            return after[1:-1]
    try:
        parts = shlex.split(recorded)
    except ValueError:
        return recorded
    while parts and _ENV_ASSIGNMENT_WORD.match(parts[0]):
        parts.pop(0)
    if parts and parts[0].endswith("build.sh") and len(parts) >= 2:
        return parts[1]
    return recorded


def _setup_requires_root_build(setup: str) -> bool:
    """Return whether build.sh must run the container as root.

    The build.sh wrapper decides whether to pass Docker --user before the
    recorded setup command is evaluated inside the container, so an inner
    `export GT_BUILD_AS_ROOT=1` has to be promoted to the host environment.
    """
    return bool(re.search(r"(?:^|[\s;'\"`])(?:export\s+)?GT_BUILD_AS_ROOT=1(?:$|[\s;'\"`])", setup))


def _recorded_command_requires_root(recorded: str, inner: str) -> bool:
    """Return whether either the wrapper invocation or inner command asks for root."""
    return _setup_requires_root_build(recorded) or _setup_requires_root_build(inner)


def _run_build_sh(
    result_dir: Path,
    inner: str,
    timeout: int,
    *,
    build_as_root: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if build_as_root:
        env["GT_BUILD_AS_ROOT"] = "1"
        inner = "git config --global --add safe.directory /gt/_work/src && " + inner
    return subprocess.run(
        [str(result_dir / "build.sh"), inner],
        cwd=result_dir,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _case_output_from_result_file(
    result_dir: Path, before: dict[str, Any]
) -> str:
    """Capture target output redirected to a result-dir file.

    Some reproduction commands intentionally run the fuzzer as
    `target /gt/poc > /gt/sanitizer_trace.txt 2>&1`. In that case Docker stdout
    contains no ASSERT_EVT records even though the target emitted them. Preserve
    the deterministic CASE framing by appending the redirected file contents
    that were produced by this run.
    """
    candidates = (result_dir / "sanitizer_trace.txt",)
    chunks: list[str] = []
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        key = str(path)
        old = before.get(key)
        if (
            old
            and old.get("size") == stat.st_size
            and old.get("mtime_ns") == stat.st_mtime_ns
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _snapshot_result_files(result_dir: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for path in (result_dir / "sanitizer_trace.txt",):
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return snapshot


def _crashed(proc: subprocess.CompletedProcess[str], extra_output: str = "") -> bool:
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "") + "\n" + extra_output
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


def _trace_result(proc: subprocess.CompletedProcess[str], extra_output: str = "") -> str:
    if _crashed(proc, extra_output):
        return "crash"
    if proc.returncode == 0:
        return "clean"
    return "error"


def _expectation_matches(expect: str, result: str) -> bool:
    if expect == "any":
        return True
    if expect == "crash":
        return result == "crash"
    if expect == "clean":
        # Stage 01's fixed oracle defines success as "the target sanitizer
        # finding is gone". Many real fixed targets reject the original PoC with
        # a domain error and non-zero status; that is still an acceptable fixed
        # oracle as long as it is not a sanitizer crash.
        return result != "crash"
    return False


def command_masks_failures(command: str) -> bool:
    """Return whether a recorded build command can hide a failed subprocess."""
    if re.search(r"\|\|\s*(?:true|:)(?:\s|$|[;&|)'\"`])", command):
        return True
    if re.search(r"(?:^|[\s;'\"`])set\s+\+e(?:$|[\s;'\"`])", command):
        return True
    return False


_BUILD_FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.MULTILINE)
    for pattern in (
        r"^FAILED:\s",
        r"\bninja: build stopped: subcommand failed\b",
        r"\b(?:g?make|make)(?:\[\d+\])?: \*\*\* .*(?:Error|Stop)",
        r"\bCMake Error\b",
        r"\bconfigure: error:",
        r"\berror: (?:use of undeclared identifier|offset of on non-standard-layout type|no member named|unknown type name|invalid operands|expected|cannot find|undefined reference|linker command failed)",
        r"\bfatal error:",
        r"\b(?:ld|ld\.lld): error:",
        r"\bcollect2: error:",
        r"\bNo rule to make target\b",
        r"\b(?:bash|sh): line \d+: .*: No such file or directory\b",
        r"\b(?:bash|sh): .*: command not found\b",
    )
)


def _build_failure_markers(proc: subprocess.CompletedProcess[str]) -> list[str]:
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    markers: list[str] = []
    for pattern in _BUILD_FAILURE_PATTERNS:
        match = pattern.search(combined)
        if match:
            markers.append(match.group(0).strip())
    return markers


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
    return f"rm -f .git/index.lock && git reset --hard HEAD && git clean -fdq {excludes}".strip()


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
    target_commit = _target_commit(version, vulnerable_commit, fix_commit)
    marker = (
        "printf 'GT_REPO_WORKSPACE_COMMIT version=%s target_commit=%s observed_commit=' "
        f"{shlex.quote(version)} {shlex.quote(target_commit)} && git rev-parse HEAD"
    )
    if not vulnerable_commit:
        raise ValueError("repo instrumentation requires sample_info.vulnerable_commit")
    if version == "vulnerable":
        return (
            f"{_clean_command(preserved_sources)} && "
            f"git checkout -q {shlex.quote(vulnerable_commit)} && {marker}"
        )
    if not fix_commit:
        raise ValueError("fixed repo instrumentation requires sample_info.fix_commit")
    return (
        f"{_clean_command(preserved_sources)} && "
        f"git checkout -q {shlex.quote(fix_commit)} && {marker}"
    )


def _target_commit(version: str, vulnerable_commit: str, fix_commit: str) -> str:
    if version == "fixed":
        return fix_commit
    return vulnerable_commit


def _setup_command_for_version(
    setup: str,
    version: str,
    vulnerable_commit: str,
    fix_commit: str,
) -> str:
    """Make a recorded vulnerable-side setup command build the requested side.

    Repo-track Stage 01 records the command that reproduced the vulnerable side,
    and many such commands include an explicit `git checkout <vulnerable>`.
    The repo-workspace gate already resets to the requested side before applying
    instrumentation; running the raw setup afterward must not switch the tree
    back to the vulnerable commit during fixed-side validation/execution.
    """
    if version != "fixed":
        return setup
    if not vulnerable_commit or not fix_commit:
        return setup
    return setup.replace(vulnerable_commit, fix_commit)


_REPO_RESET_LINE = re.compile(
    r"^\s*git\s+(?:"
    r"reset\s+--hard(?:\s+\S+)?|"
    r"checkout(?:\s+--force|\s+-f|\s+-q)*\s+\S+|"
    r"clean\s+-[A-Za-z0-9]+(?:\s+.*)?"
    r")\s*$"
)


def _setup_command_for_replay(
    setup: str,
    version: str,
    vulnerable_commit: str,
    fix_commit: str,
) -> str:
    """Replay the recorded build setup without undoing instrumentation.

    Stage 01 records the command that made the original repo reproducible. Repo
    samples often include an explicit `git reset --hard <vuln>` and
    `git checkout <vuln>` in that setup. Repo-workspace validation/execution has
    already selected the requested commit and applied the instrumentation patch,
    so replaying those recorded git commands would silently erase the patch and
    build an uninstrumented target.
    """
    setup = _setup_command_for_version(setup, version, vulnerable_commit, fix_commit)
    kept: list[str] = []
    for line in setup.splitlines():
        if _REPO_RESET_LINE.match(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def _report_template(
    *,
    result_dir: Path,
    version: str,
    target_commit: str,
    patch: Path,
    spec: dict[str, Any],
    apply_proc: subprocess.CompletedProcess[str],
    compile_proc: subprocess.CompletedProcess[str],
    track: str,
    setup_masks_failures: bool,
) -> dict[str, Any]:
    compile_failure_markers = _build_failure_markers(compile_proc)
    field_bindings = _load(result_dir / "field_bindings.json")
    runtime_field_quality = validate_instrumentation_runtime_fields(
        spec=spec,
        field_bindings=field_bindings,
        patch_text=patch.read_text(encoding="utf-8", errors="replace"),
        patch_name=patch.name,
    )
    compile_ok = compile_proc.returncode == 0 and not setup_masks_failures and not compile_failure_markers
    return {
        "schema_version": "instrumentation-side-preflight-v1",
        "sample_id": str(spec.get("sample_id") or result_dir.name),
        "version": version,
        "target_commit": target_commit,
        "observed_commit_after_checkout": _current_checkout_commit(result_dir),
        "track": track,
        "assertion_content_hash": spec.get("content_hash"),
        "ok": (
            apply_proc.returncode == 0
            and compile_ok
            and runtime_field_quality["valid"]
        ),
        "check": {
            "patch": patch.name,
            "patch_sha256": _sha256(patch),
            "apply_returncode": apply_proc.returncode,
            "compile_returncode": compile_proc.returncode,
            "setup_masks_failures": setup_masks_failures,
            "compile_failure_markers": compile_failure_markers,
            "runtime_field_quality": runtime_field_quality,
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
    with _workspace_lock(result_dir):
        return _validate_instrumentation_side_locked(
            result_dir, version, patch, out, timeout=timeout
        )


def _validate_instrumentation_side_locked(
    result_dir: Path,
    version: str,
    patch: Path,
    out: Path,
    timeout: int = 7200,
) -> dict[str, Any]:
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
    target_commit = _target_commit(version, vulnerable_commit, fix_commit)
    raw_setup = str(reproduction.get("setup_command") or "")
    setup = _inner_from_recorded_build_command(raw_setup)
    if not setup.strip():
        raise ValueError("repo-workspace requires reproduction_report.setup_command")
    setup_masks_failures = command_masks_failures(setup)
    build_as_root = _recorded_command_requires_root(raw_setup, setup)
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
    apply_proc = _run_build_sh(result_dir, apply_inner, timeout, build_as_root=build_as_root)
    _write_log(result_dir, f"plan_{version}_apply.log", apply_proc)

    if apply_proc.returncode == 0:
        compile_proc = _run_build_sh(
            result_dir,
            _setup_command_for_replay(setup, version, vulnerable_commit, fix_commit),
            timeout,
            build_as_root=build_as_root,
        )
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
        target_commit=target_commit,
        patch=patch,
        spec=spec,
        apply_proc=apply_proc,
        compile_proc=compile_proc,
        track=track,
        setup_masks_failures=setup_masks_failures,
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
    with _workspace_lock(result_dir):
        return _run_case_locked(
            result_dir,
            version,
            patch,
            expect,
            case_name=case_name,
            append_trace=append_trace,
            poc=poc,
            timeout=timeout,
        )


def _run_case_locked(
    result_dir: Path,
    version: str,
    patch: Path,
    expect: str,
    case_name: str = "original",
    append_trace: bool = False,
    poc: Path | None = None,
    timeout: int = 7200,
) -> dict[str, Any]:
    prepare = _load(result_dir / "prepare_report.json")
    track = str(prepare.get("track") or "")
    if not track.startswith("repo/"):
        raise ValueError("repo-workspace requires a repo-track prepare_report.json")
    sample_info = _load(result_dir / "sample_info.json")
    reproduction = _load(result_dir / "reproduction_report.json")

    raw_setup = str(reproduction.get("setup_command") or "")
    setup = _inner_from_recorded_build_command(raw_setup)
    command = _inner_from_recorded_build_command(
        str(reproduction.get("command") or "")
    )
    if not setup.strip():
        raise ValueError("repo-workspace requires reproduction_report.setup_command")
    if command_masks_failures(setup):
        raise ValueError(
            "repo-workspace refuses reproduction_report.setup_command because it can mask build failures"
        )
    if not command.strip():
        raise ValueError("repo-workspace requires reproduction_report.command")
    build_as_root = _recorded_command_requires_root(raw_setup, setup)

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
    target_commit = _target_commit(version, vulnerable_commit, fix_commit)
    preserved_sources = _preserved_setup_sources(setup)
    apply_patch = _normalize_repo_patch(result_dir, patch)
    relative_patch = _relative_to_or_none(apply_patch, result_dir)
    remote_patch = f"/gt/{relative_patch.as_posix()}" if relative_patch else f"/gt/{apply_patch.name}"
    log_dir = result_dir / "repo_workspace"
    for stale in (
        log_dir / f"{version}_{case_name}_run.log",
        log_dir / f"{version}_{case_name}_run.json",
    ):
        if stale.is_file() or stale.is_symlink():
            stale.unlink()
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
            _setup_command_for_replay(setup, version, vulnerable_commit, fix_commit),
            command,
        ]
    )
    before_files = _snapshot_result_files(result_dir)
    proc = _run_build_sh(result_dir, inner, timeout, build_as_root=build_as_root)
    patch_sha = _sha256(patch)
    normalized_patch_sha = _sha256(apply_patch)
    _write_log(
        result_dir,
        f"{version}_{case_name}_run.log",
        proc,
        header=(
            f"version={version}\n"
            f"case_name={case_name}\n"
            f"patch={patch.name}\n"
            f"patch_sha256={patch_sha}\n"
            f"normalized_patch={apply_patch.name}\n"
            f"normalized_patch_sha256={normalized_patch_sha}\n"
        ),
    )

    trace_path = result_dir / f"{version}_assertion_trace.txt"
    trace_mode = "a" if append_trace else "w"
    redirected_output = _case_output_from_result_file(result_dir, before_files)
    combined_parts = [part for part in (proc.stdout or "", proc.stderr or "", redirected_output) if part]
    combined = "\n".join(combined_parts)
    result = _trace_result(proc, redirected_output)
    with trace_path.open(trace_mode, encoding="utf-8") as trace:
        trace.write(f"CASE name={case_name} rc={proc.returncode} result={result}\n")
        trace.write(combined)
        if combined and not combined.endswith("\n"):
            trace.write("\n")
        trace.write("ENDCASE\n")

    matched = _expectation_matches(expect, result)
    report = {
        "schema_version": "repo-run-v1",
        "sample_id": str(sample_info.get("sample_id") or result_dir.name),
        "version": version,
        "target_commit": target_commit,
        "observed_commit_after_checkout": _current_checkout_commit(result_dir),
        "track": track,
        "case_name": case_name,
        "expect": expect,
        "returncode": proc.returncode,
        "result": result,
        "matched": matched,
        "patch": patch.name,
        "patch_sha256": patch_sha,
        "normalized_patch": apply_patch.name,
        "normalized_patch_sha256": normalized_patch_sha,
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
