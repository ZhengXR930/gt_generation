"""Manage one serial ARVO container across vulnerable and fixed validation.

The expensive `/bin/arvo compile` runs once in Stage 01 for vulnerable reproduction.
Stage 04 reuses the configured tree for target-level vulnerable instrumentation and
fixed-patch rebuilds. A fixed image/full rebuild is an explicit fallback, never the
default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _safe_name(sample_id: str) -> str:
    return "gt-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", sample_id) + "-workspace"


def _context(result_dir: Path) -> dict[str, Any]:
    report = _load(result_dir / "prepare_report.json")
    if report.get("track") != "arvo":
        raise ValueError("arvo-workspace requires an ARVO prepare_report.json")
    aid = str(report.get("arvo_id") or "")
    sample_id = f"arvo_{aid}"
    return {
        "sample_id": sample_id,
        "arvo_id": aid,
        "container": str(report.get("workspace_container") or _safe_name(sample_id)),
        "vul_image": str(report.get("vul_image") or f"n132/arvo:{aid}-vul"),
        "fix_image": str(report.get("fix_image") or f"n132/arvo:{aid}-fix"),
        "target": str(report.get("target") or ""),
    }


def _state_path(result_dir: Path) -> Path:
    return result_dir / "arvo_workspace.json"


def _source_root(result_dir: Path) -> str:
    """Resolve the project checkout inside a generic ARVO image.

    ARVO projects are not all mounted at the historical `/src/readstat` path.  Bind
    the root deterministically from the first path in the official patch and persist
    it so every Stage 04 operation uses the same checkout.
    """
    context = _context(result_dir)
    state_path = _state_path(result_dir)
    state = _load(state_path) if state_path.is_file() else {}
    persisted = str(state.get("source_root") or "").strip()
    if persisted:
        return persisted
    patch_text = (result_dir / "patch.diff").read_text(
        encoding="utf-8", errors="replace"
    )
    match = re.search(r"^diff --git a/(.+?) b/", patch_text, re.MULTILINE)
    if not match:
        raise RuntimeError("cannot resolve ARVO source root: patch has no diff path")
    relative = match.group(1)
    command = (
        "find /src -type f -path "
        + shlex.quote("*/" + relative)
        + " -print | head -n 1"
    )
    found = _docker_exec(context["container"], command)
    path = found.stdout.strip().splitlines()
    if found.returncode != 0 or not path:
        raise RuntimeError(
            f"cannot resolve ARVO source root for patch path {relative!r}"
        )
    suffix = "/" + relative
    candidate = path[0]
    if not candidate.endswith(suffix):
        raise RuntimeError(f"unexpected ARVO source path: {candidate}")
    root = candidate[: -len(suffix)]
    checked = _docker_exec(
        context["container"], f"git -C {shlex.quote(root)} rev-parse --show-toplevel"
    )
    if checked.returncode != 0:
        raise RuntimeError(f"resolved ARVO source root is not a git checkout: {root}")
    root = checked.stdout.strip().splitlines()[-1]
    _update_state(result_dir, context, source_root=root)
    return root


def _update_state(result_dir: Path, context: dict[str, Any], **updates: Any) -> dict[str, Any]:
    path = _state_path(result_dir)
    state = _load(path) if path.exists() else dict(context)
    state.update(updates)
    _write(path, state)
    return state


def _require_frozen_spec(result_dir: Path) -> dict[str, Any]:
    from .assertions import validate_frozen_spec

    marker_path = result_dir / ".assertion_spec_frozen.json"
    expected_spec = (result_dir / "candidate_assertions.json").resolve()
    if not marker_path.is_file():
        raise RuntimeError(
            "Stage 04 execution is locked until candidate_assertions.json is frozen"
        )
    marker = _load(marker_path)
    spec_path = Path(str(marker.get("spec_path") or "")).resolve()
    if spec_path != expected_spec or not spec_path.is_file():
        raise RuntimeError("freeze marker does not bind result candidate_assertions.json")
    raw = spec_path.read_bytes()
    if marker.get("file_sha256") != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise RuntimeError("candidate_assertions.json changed after freeze")
    spec = json.loads(raw)
    validate_frozen_spec(spec)
    if marker.get("content_hash") != spec.get("content_hash"):
        raise RuntimeError("freeze marker content hash mismatch")
    return marker


def _source_fingerprint(result_dir: Path) -> str:
    context = _context(result_dir)
    root = _source_root(result_dir)
    proc = _docker_exec(
        context["container"], f"git -C {shlex.quote(root)} diff --binary"
    )
    if proc.returncode != 0:
        raise RuntimeError("cannot fingerprint instrumented source")
    return "sha256:" + hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()


def _verify_source_fingerprint(result_dir: Path) -> None:
    state = _load(_state_path(result_dir))
    expected = str(state.get("instrumentation_source_sha256") or "")
    if not expected:
        raise RuntimeError("no persisted instrumentation source fingerprint")
    if _source_fingerprint(result_dir) != expected:
        raise RuntimeError(
            "container source changed outside the persisted instrumentation patch"
        )


def _require_persisted_instrumentation_patch(result_dir: Path, patch: Path) -> Path:
    state = _load(_state_path(result_dir))
    phase = str(state.get("phase") or "")
    version = "fixed" if phase.startswith("fixed") else "vulnerable"
    expected = (result_dir / f"{version}-instrumentation.patch").resolve()
    if patch.resolve() != expected or not expected.is_file():
        raise RuntimeError(
            f"{version} instrumentation must be persisted exactly at {expected}"
        )
    return expected


def _docker_exec(container: str, command: str, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "exec", container, "/bin/bash", "-lc", command], timeout)


def create(result_dir: Path) -> int:
    context = _context(result_dir)
    container = context["container"]
    if _run(["docker", "inspect", container]).returncode == 0:
        _run(["docker", "rm", "-f", container])
    created = _run(["docker", "create", "--name", container, context["vul_image"]])
    if created.returncode != 0:
        return created.returncode
    started = _run(["docker", "start", container])
    _write(
        _state_path(result_dir),
        {
            **context,
            "phase": "vulnerable_created",
            "container_running": started.returncode == 0,
            "fixed_strategy": "patch_incremental",
        },
    )
    return started.returncode


def apply_instrumentation(result_dir: Path, patch: Path) -> int:
    marker = _require_frozen_spec(result_dir)
    patch = _require_persisted_instrumentation_patch(result_dir, patch)
    context = _context(result_dir)
    root = _source_root(result_dir)
    container = context["container"]
    remote = "/tmp/gt_instrumentation.patch"
    copied = _run(["docker", "cp", str(patch), f"{container}:{remote}"])
    if copied.returncode != 0:
        return copied.returncode
    applied = _docker_exec(
        container,
        f"git -C {shlex.quote(root)} apply --check {remote} && "
        f"git -C {shlex.quote(root)} apply {remote}",
    )
    _write_log(result_dir, "instrumentation_apply.log", applied)
    if applied.returncode == 0:
        _update_state(
            result_dir,
            context,
            instrumentation_patch=str(patch.resolve()),
            instrumentation_patch_sha256=(
                "sha256:" + hashlib.sha256(patch.read_bytes()).hexdigest()
            ),
            instrumentation_source_sha256=_source_fingerprint(result_dir),
            assertion_content_hash=marker["content_hash"],
        )
    return applied.returncode


def compile_vulnerable(result_dir: Path) -> int:
    context = _context(result_dir)
    proc = _docker_exec(context["container"], "/bin/arvo compile", timeout=7200)
    _write_log(result_dir, "vulnerable_compile.log", proc)
    _update_state(
        result_dir,
        context,
        phase="vulnerable_compiled" if proc.returncode == 0 else "vulnerable_compile_failed",
        vulnerable_compile_returncode=proc.returncode,
    )
    return proc.returncode


def switch_fixed(result_dir: Path, patch: Path) -> int:
    _require_frozen_spec(result_dir)
    context = _context(result_dir)
    root = _source_root(result_dir)
    container = context["container"]
    remote = "/tmp/gt_official_fix.patch"
    copied = _run(["docker", "cp", str(patch), f"{container}:{remote}"])
    if copied.returncode != 0:
        return copied.returncode
    proc = _docker_exec(
        container,
        f"git -C {shlex.quote(root)} reset --hard HEAD && "
        f"git -C {shlex.quote(root)} apply --check {remote} && "
        f"git -C {shlex.quote(root)} apply {remote}",
    )
    _write_log(result_dir, "fixed_patch_apply.log", proc)
    _update_state(
        result_dir,
        context,
        phase="fixed_source" if proc.returncode == 0 else "fixed_patch_failed",
        fixed_patch_returncode=proc.returncode,
        instrumentation_source_sha256="",
    )
    return proc.returncode


def compile_fixed(
    result_dir: Path,
    *,
    target: str = "",
    fallback_image: bool = False,
    instrumentation_patch: Path | None = None,
) -> int:
    returncode = compile_target(result_dir, version="fixed", target=target)
    context = _context(result_dir)
    if returncode == 0:
        return 0
    if not fallback_image:
        _update_state(
            result_dir,
            context,
            phase="fixed_incremental_failed",
            fixed_compile_returncode=returncode,
            fallback_required=True,
        )
        return returncode
    return _compile_fixed_fallback(result_dir, context, instrumentation_patch)


def compile_target(result_dir: Path, *, version: str, target: str = "") -> int:
    """Incrementally rebuild the active target in the existing configured tree."""
    _require_frozen_spec(result_dir)
    _verify_source_fingerprint(result_dir)
    context = _context(result_dir)
    root = _source_root(result_dir)
    target = target or context["target"]
    if not target:
        raise ValueError("ARVO target is unknown; pass --target")
    qroot = shlex.quote(root)
    qtarget = shlex.quote(target)
    command = (
        "build_dir=''; "
        f"for candidate in {qroot}/out/*/{qtarget}; do "
        "if [ -f \"$candidate\" ] && [ -f \"${candidate%/*}/build.ninja\" ]; "
        "then build_dir=${candidate%/*}; break; fi; done; "
        "if [ -n \"$build_dir\" ]; then "
        f"ninja -C \"$build_dir\" {qtarget} && "
        f"cp \"$build_dir\"/{qtarget} /out/{qtarget}; "
        f"elif [ -f {qroot}/Makefile ]; then "
        f"make -C {qroot} {qtarget} && cp {qroot}/{qtarget} /out/{qtarget}; "
        "else exit 127; fi"
    )
    incremental = _docker_exec(context["container"], command, timeout=3600)
    _write_log(result_dir, f"{version}_incremental_compile.log", incremental)
    proc = incremental
    fallback_used = incremental.returncode != 0
    if fallback_used:
        proc = _docker_exec(
            context["container"], "/bin/arvo compile", timeout=7200
        )
        _write_log(result_dir, f"{version}_fallback_compile.log", proc)
    updates: dict[str, Any] = {
        "phase": (
            f"{version}_instrumented_compiled"
            if proc.returncode == 0
            else f"{version}_incremental_failed"
        ),
        f"{version}_incremental_compile_returncode": incremental.returncode,
        f"{version}_compile_fallback_used": fallback_used,
        f"{version}_compile_returncode": proc.returncode,
    }
    if version == "fixed":
        updates.update(
            fixed_strategy="patch_incremental",
            fixed_compile_returncode=proc.returncode,
        )
    _update_state(result_dir, context, **updates)
    return proc.returncode


def _compile_fixed_fallback(
    result_dir: Path,
    context: dict[str, Any],
    instrumentation_patch: Path | None,
) -> int:
    pull = _run(["docker", "pull", context["fix_image"]], timeout=2400)
    if pull.returncode != 0:
        return pull.returncode
    _run(["docker", "rm", "-f", context["container"]])
    created = _run([
        "docker", "create", "--name", context["container"], context["fix_image"]
    ])
    if created.returncode != 0:
        return created.returncode
    if _run(["docker", "start", context["container"]]).returncode != 0:
        return 1
    if instrumentation_patch and apply_instrumentation(result_dir, instrumentation_patch) != 0:
        return 1
    proc = _docker_exec(context["container"], "/bin/arvo compile", timeout=7200)
    _write_log(result_dir, "fixed_fallback_compile.log", proc)
    _update_state(
        result_dir,
        context,
        phase="fixed_compiled" if proc.returncode == 0 else "fixed_fallback_failed",
        fixed_strategy="fixed_image_full_fallback",
        fixed_compile_returncode=proc.returncode,
    )
    return proc.returncode


def run_case(result_dir: Path, version: str, expect: str) -> int:
    context = _context(result_dir)
    state = _load(_state_path(result_dir))
    if "instrumented" in str(state.get("phase") or "") or version == "fixed":
        _require_frozen_spec(result_dir)
        _verify_source_fingerprint(result_dir)
    proc = _docker_exec(context["container"], "/bin/arvo run", timeout=300)
    _write_log(result_dir, f"{version}_run.log", proc)
    combined = proc.stdout + "\n" + proc.stderr
    crashed = proc.returncode != 0 and any(
        marker in combined for marker in (
            "AddressSanitizer", "MemorySanitizer", "runtime error:", "SEGV", "ABORTING"
        )
    )
    matched = expect == "any" or (expect == "crash" and crashed) or (
        expect == "clean" and proc.returncode == 0 and not crashed
    )
    _update_state(
        result_dir,
        context,
        phase=f"{version}_ran",
        **{
            f"{version}_run_returncode": proc.returncode,
            f"{version}_expectation": expect,
            f"{version}_expectation_matched": matched,
        },
    )
    return 0 if matched else 1


def cleanup(result_dir: Path, remove_images: bool) -> int:
    context = _context(result_dir)
    _run(["docker", "rm", "-f", context["container"]])
    removed = []
    if remove_images:
        for image in (context["vul_image"], context["fix_image"]):
            proc = _run(["docker", "image", "rm", image])
            if proc.returncode == 0:
                removed.append(image)
    _update_state(
        result_dir,
        context,
        phase="cleaned",
        container_running=False,
        removed_images=removed,
    )
    # The copied instrumented source and command logs are working state, not GT
    # assets.  Reachability persists its own compact breakpoints/hits before this.
    shutil.rmtree(result_dir / "_work", ignore_errors=True)
    shutil.rmtree(result_dir / "arvo_workspace", ignore_errors=True)
    return 0


def _write_log(result_dir: Path, name: str, proc: subprocess.CompletedProcess[str]) -> None:
    path = result_dir / "arvo_workspace" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"returncode={proc.returncode}\n{proc.stdout}\n{proc.stderr}",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gt-toolkit arvo-workspace", description=__doc__)
    parser.add_argument("--result-dir", required=True, type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("create")
    p_apply = sub.add_parser("apply-instrumentation")
    p_apply.add_argument("--patch", required=True, type=Path)
    sub.add_parser("compile-vulnerable")
    p_target = sub.add_parser("compile-target")
    p_target.add_argument("--version", required=True, choices=["vulnerable", "fixed"])
    p_target.add_argument("--target", default="")
    p_switch = sub.add_parser("switch-fixed")
    p_switch.add_argument("--patch", required=True, type=Path)
    p_fixed = sub.add_parser("compile-fixed")
    p_fixed.add_argument("--target", default="")
    p_fixed.add_argument("--fallback-image", action="store_true")
    p_fixed.add_argument("--instrumentation-patch", type=Path)
    p_run = sub.add_parser("run")
    p_run.add_argument("--version", required=True, choices=["vulnerable", "fixed"])
    p_run.add_argument("--expect", default="any", choices=["crash", "clean", "any"])
    p_cleanup = sub.add_parser("cleanup")
    p_cleanup.add_argument("--remove-images", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    if args.action == "create":
        return create(args.result_dir)
    if args.action == "apply-instrumentation":
        return apply_instrumentation(args.result_dir, args.patch)
    if args.action == "compile-vulnerable":
        return compile_vulnerable(args.result_dir)
    if args.action == "compile-target":
        return compile_target(args.result_dir, version=args.version, target=args.target)
    if args.action == "switch-fixed":
        return switch_fixed(args.result_dir, args.patch)
    if args.action == "compile-fixed":
        return compile_fixed(
            args.result_dir,
            target=args.target,
            fallback_image=args.fallback_image,
            instrumentation_patch=args.instrumentation_patch,
        )
    if args.action == "run":
        return run_case(args.result_dir, args.version, args.expect)
    if args.action == "cleanup":
        return cleanup(args.result_dir, args.remove_images)
    if args.action == "status":
        print(json.dumps(_load(_state_path(args.result_dir)), indent=2, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
