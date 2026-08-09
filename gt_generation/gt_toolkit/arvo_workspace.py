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
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", timeout=timeout
    )


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
    """Image, container and target for an ARVO package.

    prepare_report.json is authoritative but optional: compact_result used to
    delete it once a package completed, so a repair re-run of an older package
    finds it missing. Everything in it can be recovered from what survives, and
    refusing to do so turns every workspace command into a ValueError -- which
    is how arvo_375220555 lost a confirmed differential on a re-run.
    """
    report_path = result_dir / "prepare_report.json"
    report = _load(report_path) if report_path.is_file() else {}
    track = str(report.get("track") or "")
    if report and track and track != "arvo":
        raise ValueError("arvo-workspace requires an ARVO prepare_report.json")
    aid = str(report.get("arvo_id") or "")
    if not aid:
        name = result_dir.resolve().name
        info = result_dir / "sample_info.json"
        if info.is_file():
            name = str(_load(info).get("sample_id") or name)
        if not name.startswith("arvo_"):
            raise ValueError("arvo-workspace requires an ARVO package")
        aid = name[len("arvo_"):]
    if not aid:
        raise ValueError("arvo-workspace cannot determine the ARVO id")
    sample_id = f"arvo_{aid}"
    return {
        "sample_id": sample_id,
        "arvo_id": aid,
        "container": str(report.get("workspace_container") or _safe_name(sample_id)),
        "vul_image": str(report.get("vul_image") or f"n132/arvo:{aid}-vul"),
        "fix_image": str(report.get("fix_image") or f"n132/arvo:{aid}-fix"),
        "target": str(report.get("target") or "") or _target_from_traces(result_dir),
    }


def _target_from_traces(result_dir: Path) -> str:
    """The binary libFuzzer reported running, from a saved trace."""
    for name in ("sanitizer_trace.txt", "default_crash_trace.txt",
                 "vulnerable_assertion_trace.txt"):
        path = result_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found = re.search(r"/out/([A-Za-z0-9_.-]+):\s+Running", text)
        if not found:
            found = re.search(r"/out/([A-Za-z0-9_.-]+)(?:\+0x[0-9a-f]+)?\b", text)
        if found:
            return found.group(1)
    return ""


def _state_path(result_dir: Path) -> Path:
    return result_dir / "arvo_workspace.json"


def _detect_source_root(result_dir: Path, context: dict[str, Any]) -> str:
    """Find the project git checkout under /src without reading patch.diff.

    ARVO images clone the project under /src/<name>. Enumerate the git checkouts there
    and, when several exist (helper repos or submodules), prefer the one whose basename
    matches the sample's project. Returns "" when it cannot be decided, so the caller
    can fall back to the legacy patch-path resolution.
    """
    listing = _docker_exec(
        context["container"],
        "find /src -maxdepth 4 -type d -name .git 2>/dev/null "
        '| while read g; do dirname "$g"; done',
    )
    tops: list[str] = []
    for d in (ln.strip() for ln in listing.stdout.splitlines() if ln.strip()):
        top = _docker_exec(
            context["container"],
            f"git -C {shlex.quote(d)} rev-parse --show-toplevel 2>/dev/null",
        )
        lines = top.stdout.strip().splitlines()
        if top.returncode == 0 and lines:
            tops.append(lines[-1])
    tops = list(dict.fromkeys(tops))
    if not tops:
        return ""
    if len(tops) == 1:
        return tops[0]
    project = ""
    try:
        project = str(
            json.loads((result_dir / "sample_info.json").read_text(encoding="utf-8")).get("project") or ""
        ).strip().lower()
    except Exception:
        project = ""
    if project:
        for t in tops:  # exact basename match first
            if t.rstrip("/").rsplit("/", 1)[-1].lower() == project:
                return t
        for t in tops:  # then substring
            if project in t.lower():
                return t
    return ""  # ambiguous -> let the patch-path fallback decide


def _source_root(result_dir: Path) -> str:
    """Resolve the project checkout inside a generic ARVO image.

    ARVO projects are not all mounted at the historical `/src/readstat` path.  Detect
    the checkout under /src (patch-independent) and persist it so every Stage 04
    operation uses the same root; fall back to the first path in patch.diff only if
    detection cannot decide.
    """
    context = _context(result_dir)
    state_path = _state_path(result_dir)
    state = _load(state_path) if state_path.is_file() else {}
    persisted = str(state.get("source_root") or "").strip()
    if persisted:
        return persisted
    # Prefer detecting the project checkout directly (independent of patch.diff, which
    # for ARVO is often an unrelated commit). Fall back to the legacy patch-path scan.
    detected = _detect_source_root(result_dir, context)
    if detected:
        _update_state(result_dir, context, source_root=detected)
        return detected
    patch_text = (result_dir / "patch.diff").read_text(
        encoding="utf-8", errors="replace"
    ) if (result_dir / "patch.diff").is_file() else ""
    match = re.search(r"^diff --git a/(.+?) b/", patch_text, re.MULTILINE)
    if not match:
        raise RuntimeError("cannot resolve ARVO source root: no /src git checkout found "
                           "and patch.diff has no diff path")
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


def _require_runtime_disambiguation(result_dir: Path) -> None:
    """Authorize bounded pre-Stage-04 execution from existing control files."""
    flags_path = result_dir / "run_flags.json"
    feedback_path = result_dir / "trace_feedback.json"
    if not flags_path.is_file() or not feedback_path.is_file():
        raise RuntimeError(
            "runtime disambiguation requires run_flags.json and trace_feedback.json"
        )
    flags = _load(flags_path)
    feedback = _load(feedback_path)
    if not flags.get("runtime_disambiguation"):
        raise RuntimeError("runtime disambiguation is disabled for this run")
    if not feedback.get("needs_runtime_disambiguation"):
        raise RuntimeError("reviewer did not request runtime disambiguation")
    if not str(feedback.get("observe") or "").strip():
        raise RuntimeError("runtime disambiguation request has no observe question")


def _require_execution_authorization(
    result_dir: Path, runtime_disambiguation: bool
) -> dict[str, Any] | None:
    if runtime_disambiguation:
        _require_runtime_disambiguation(result_dir)
        return None
    return _require_frozen_spec(result_dir)


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


def _require_persisted_instrumentation_patch(result_dir: Path, patch: Path) -> tuple[Path, str]:
    resolved = patch.resolve()
    vulnerable = (result_dir / "vulnerable-instrumentation.patch").resolve()
    fixed = (result_dir / "fixed-instrumentation.patch").resolve()
    if resolved == vulnerable:
        version = "vulnerable"
    elif resolved == fixed:
        version = "fixed"
    else:
        raise RuntimeError(
            "instrumentation must use a persisted vulnerable or fixed result patch"
        )
    expected = (result_dir / f"{version}-instrumentation.patch").resolve()
    if not expected.is_file():
        raise RuntimeError(
            f"{version} instrumentation must be persisted exactly at {expected}"
        )
    return expected, version


def _docker_exec(container: str, command: str, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "exec", container, "/bin/bash", "-lc", command], timeout)


def _remove_stale_plan_containers(container_prefix: str) -> list[str]:
    listed = _run([
        "docker",
        "ps",
        "-a",
        "--filter",
        f"name=^{container_prefix}",
        "--format",
        "{{.Names}}",
    ])
    if listed.returncode != 0:
        return []
    names = [
        name.strip()
        for name in listed.stdout.splitlines()
        if name.strip().startswith(container_prefix)
    ]
    for name in names:
        _run(["docker", "rm", "-f", name])
    return names


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


def ensure_vulnerable_workspace(result_dir: Path) -> int:
    """Reuse a live vulnerable workspace or deterministically recreate its build."""
    context = _context(result_dir)
    inspected = _run([
        "docker",
        "inspect",
        "-f",
        "{{.State.Running}}",
        context["container"],
    ])
    if inspected.returncode == 0 and inspected.stdout.strip() == "true":
        _update_state(result_dir, context, container_running=True)
        return 0
    pulled = _run(["docker", "pull", context["vul_image"]], timeout=2400)
    if pulled.returncode != 0:
        return pulled.returncode
    if create(result_dir) != 0:
        return 1
    return compile_vulnerable(result_dir)


def _restore_workspace_for_new_attempt(result_dir: Path) -> bool:
    """Rebuild the workspace when a previous run's cleanup removed it.

    Returns whether a restore happened; a live container is left untouched.

    Recreating always produces the vulnerable baseline, which is where Stage 04
    begins. That also resets the phase, so a caller that lost its container
    mid-differential and asks to apply the fixed patch is turned away by the
    patch contract rather than having fixed instrumentation applied to
    vulnerable source.
    """
    context = _context(result_dir)
    inspected = _run([
        "docker", "inspect", "-f", "{{.State.Running}}", context["container"],
    ])
    if inspected.returncode == 0 and inspected.stdout.strip() == "true":
        return False
    if ensure_vulnerable_workspace(result_dir) != 0:
        raise RuntimeError(
            "the ARVO workspace was removed and could not be rebuilt; "
            "re-run the sample from 01_reproducer"
        )
    return True


def _reset_side_for_instrumentation_retry(
    result_dir: Path, context: dict[str, Any], version: str
) -> bool:
    """Restore the requested side before reapplying a frozen patch.

    A stage retry reuses the live container. The previous attempt may have left
    its instrumentation in the source tree or advanced to the fixed side. In
    either case a second `git apply --check` is guaranteed to fail unless the
    tracked tree is restored first. Build outputs stay in place for incremental
    reuse.
    """
    state_path = _state_path(result_dir)
    state = _load(state_path) if state_path.is_file() else {}
    phase = str(state.get("phase") or "")
    prior_patch = str(state.get("instrumentation_patch") or "")
    if not prior_patch and not phase.startswith("fixed"):
        return False
    if version == "fixed":
        if not phase.startswith("fixed"):
            raise RuntimeError(
                "fixed instrumentation requires switch-fixed before application"
            )
        return False
    root = _source_root(result_dir)
    proc = _docker_exec(
        context["container"], f"git -C {shlex.quote(root)} reset --hard HEAD"
    )
    _write_log(result_dir, "instrumentation_retry_reset.log", proc)
    if proc.returncode != 0:
        raise RuntimeError("could not restore vulnerable source for instrumentation retry")
    _update_state(
        result_dir,
        context,
        phase="vulnerable_source_reset",
        instrumentation_patch="",
        instrumentation_patch_sha256="",
        instrumentation_source_sha256="",
        workspace_reset_for_instrumentation_retry=True,
    )
    return True


def apply_instrumentation(
    result_dir: Path, patch: Path, *, runtime_disambiguation: bool = False
) -> int:
    marker = _require_execution_authorization(result_dir, runtime_disambiguation)
    # A re-run of Stage 04 meets a workspace the previous run's cleanup removed.
    # Restore it before the patch contract is checked: recreating resets the
    # phase to a vulnerable one, which is the side the stage opens with.
    restored = _restore_workspace_for_new_attempt(result_dir)
    patch, version = _require_persisted_instrumentation_patch(result_dir, patch)
    context = _context(result_dir)
    if restored:
        _update_state(result_dir, context, workspace_restored_for_attempt=True)
    _reset_side_for_instrumentation_retry(result_dir, context, version)
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
        updates: dict[str, Any] = {
            "instrumentation_patch": str(patch.resolve()),
            "instrumentation_patch_sha256": (
                "sha256:" + hashlib.sha256(patch.read_bytes()).hexdigest()
            ),
            "instrumentation_source_sha256": _source_fingerprint(result_dir),
        }
        if marker is not None:
            updates["assertion_content_hash"] = marker["content_hash"]
        _update_state(result_dir, context, **updates)
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


def validate_instrumentation_plan(
    result_dir: Path,
    vulnerable_patch: Path,
    fixed_patch: Path,
    out: Path,
) -> int:
    """Compile both frozen instrumentation patches in disposable ARVO containers."""
    marker = _require_frozen_spec(result_dir)
    context = _context(result_dir)
    checks: dict[str, Any] = {}
    for version, patch, image in (
        ("vulnerable", vulnerable_patch, context["vul_image"]),
        ("fixed", fixed_patch, context["fix_image"]),
    ):
        expected, patch_version = _require_persisted_instrumentation_patch(
            result_dir, patch
        )
        if patch_version != version:
            raise RuntimeError(
                f"{version} build check received {patch_version} instrumentation"
            )
        container = f"{context['container']}-plan-{version}-{os.getpid()}"
        side_context = {**context, "container": container}
        applied = subprocess.CompletedProcess([], 1, "", "not started")
        compiled = subprocess.CompletedProcess([], 1, "", "not started")
        try:
            pulled = _run(["docker", "pull", image], timeout=2400)
            if pulled.returncode != 0:
                compiled = pulled
                continue
            created = _run(["docker", "create", "--name", container, image])
            if created.returncode != 0:
                compiled = created
                continue
            started = _run(["docker", "start", container])
            if started.returncode != 0:
                compiled = started
                continue
            root = _detect_source_root(result_dir, side_context)
            if not root:
                compiled = subprocess.CompletedProcess(
                    [], 1, "", "cannot resolve project source root"
                )
                continue
            remote = f"/tmp/gt_{version}_instrumentation.patch"
            copied = _run(["docker", "cp", str(expected), f"{container}:{remote}"])
            if copied.returncode != 0:
                applied = copied
                continue
            applied = _docker_exec(
                container,
                f"git -C {shlex.quote(root)} apply --check {remote} && "
                f"git -C {shlex.quote(root)} apply {remote}",
            )
            if applied.returncode == 0:
                compiled = _docker_exec(container, "/bin/arvo compile", timeout=7200)
        finally:
            _run(["docker", "rm", "-f", container])
            _write_log(result_dir, f"plan_{version}_apply.log", applied)
            _write_log(result_dir, f"plan_{version}_compile.log", compiled)
            checks[version] = {
                "image": image,
                "patch": expected.name,
                "patch_sha256": "sha256:" + hashlib.sha256(
                    expected.read_bytes()
                ).hexdigest(),
                "apply_returncode": applied.returncode,
                "compile_returncode": compiled.returncode,
            }
    report = {
        "schema_version": "instrumentation-build-preflight-v1",
        "sample_id": context["sample_id"],
        "assertion_content_hash": marker["content_hash"],
        "ok": all(
            check["apply_returncode"] == 0 and check["compile_returncode"] == 0
            for check in checks.values()
        ) and set(checks) == {"vulnerable", "fixed"},
        "checks": checks,
    }
    _write(out, report)
    return 0 if report["ok"] else 1


def validate_instrumentation_side(
    result_dir: Path,
    version: str,
    patch: Path,
    out: Path,
) -> int:
    """Compile one frozen observation patch in its stock ARVO image."""
    if version not in {"vulnerable", "fixed"}:
        raise ValueError(f"unsupported instrumentation side: {version}")
    marker = _require_frozen_spec(result_dir)
    context = _context(result_dir)
    expected, patch_version = _require_persisted_instrumentation_patch(
        result_dir, patch
    )
    if patch_version != version:
        raise RuntimeError(
            f"{version} build check received {patch_version} instrumentation"
        )
    image = context["vul_image"] if version == "vulnerable" else context["fix_image"]
    _remove_stale_plan_containers(
        f"{context['container']}-plan-{version}-"
    )
    container = f"{context['container']}-plan-{version}-{os.getpid()}"
    side_context = {**context, "container": container}
    applied = subprocess.CompletedProcess([], 1, "", "not started")
    compiled = subprocess.CompletedProcess([], 1, "", "not started")
    try:
        pulled = _run(["docker", "pull", image], timeout=2400)
        if pulled.returncode != 0:
            compiled = pulled
        else:
            created = _run(["docker", "create", "--name", container, image])
            if created.returncode != 0:
                compiled = created
            else:
                started = _run(["docker", "start", container])
                if started.returncode != 0:
                    compiled = started
                else:
                    root = _detect_source_root(result_dir, side_context)
                    if not root:
                        compiled = subprocess.CompletedProcess(
                            [], 1, "", "cannot resolve project source root"
                        )
                    else:
                        remote = f"/tmp/gt_{version}_instrumentation.patch"
                        copied = _run([
                            "docker", "cp", str(expected), f"{container}:{remote}"
                        ])
                        if copied.returncode != 0:
                            applied = copied
                        else:
                            applied = _docker_exec(
                                container,
                                f"git -C {shlex.quote(root)} apply --check {remote} && "
                                f"git -C {shlex.quote(root)} apply {remote}",
                            )
                            if applied.returncode == 0:
                                compiled = _docker_exec(
                                    container, "/bin/arvo compile", timeout=7200
                                )
    finally:
        _run(["docker", "rm", "-f", container])
        _write_log(result_dir, f"plan_{version}_apply.log", applied)
        _write_log(result_dir, f"plan_{version}_compile.log", compiled)
    check = {
        "image": image,
        "patch": expected.name,
        "patch_sha256": "sha256:" + hashlib.sha256(
            expected.read_bytes()
        ).hexdigest(),
        "apply_returncode": applied.returncode,
        "compile_returncode": compiled.returncode,
    }
    report = {
        "schema_version": "instrumentation-side-preflight-v1",
        "sample_id": context["sample_id"],
        "version": version,
        "assertion_content_hash": marker["content_hash"],
        "ok": (
            check["apply_returncode"] == 0
            and check["compile_returncode"] == 0
        ),
        "check": check,
    }
    _write(out, report)
    return 0 if report["ok"] else 1


def switch_fixed(result_dir: Path, patch: Path) -> int:
    """Legacy helper for old runs; new ARVO validation uses switch_fixed_image."""
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


def switch_fixed_image(result_dir: Path) -> int:
    """Replace the vulnerable workspace with ARVO's published fixed image."""
    _require_frozen_spec(result_dir)
    context = _context(result_dir)
    pull = _run(["docker", "pull", context["fix_image"]], timeout=2400)
    if pull.returncode != 0:
        return pull.returncode
    _run(["docker", "rm", "-f", context["container"]])
    created = _run([
        "docker", "create", "--name", context["container"], context["fix_image"]
    ])
    if created.returncode != 0:
        return created.returncode
    started = _run(["docker", "start", context["container"]])
    _update_state(
        result_dir,
        context,
        phase="fixed_image_source" if started.returncode == 0 else "fixed_image_failed",
        container_running=started.returncode == 0,
        fixed_strategy="fixed_image",
        source_root="",
        instrumentation_patch="",
        instrumentation_patch_sha256="",
        instrumentation_source_sha256="",
    )
    return started.returncode


def reset_source(result_dir: Path) -> int:
    """Reset the workspace project tree to clean vulnerable source.

    Drops any throwaway instrumentation (e.g. a Stage 02 runtime-disambiguation marker)
    so a later stage starts from an unmodified tree. Additive: no default flow calls this;
    it exists for the bounded Stage 02 disambiguation escalation to hand a clean workspace
    back to Stage 04.
    """
    context = _context(result_dir)
    root = _source_root(result_dir)
    proc = _docker_exec(
        context["container"],
        f"git -C {shlex.quote(root)} reset --hard HEAD && "
        f"git -C {shlex.quote(root)} clean -fdq",
    )
    _write_log(result_dir, "reset_source.log", proc)
    _update_state(
        result_dir,
        context,
        phase="vulnerable_source_reset" if proc.returncode == 0 else "reset_failed",
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


def compile_target(
    result_dir: Path,
    *,
    version: str,
    target: str = "",
    runtime_disambiguation: bool = False,
) -> int:
    """Incrementally rebuild the active target in the existing configured tree."""
    _require_execution_authorization(result_dir, runtime_disambiguation)
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
    if proc.returncode == 0:
        updates["instrumentation_source_sha256"] = _source_fingerprint(result_dir)
    if version == "fixed":
        state_path = _state_path(result_dir)
        state = _load(state_path) if state_path.is_file() else {}
        fixed_strategy = str(state.get("fixed_strategy") or "fixed_image")
        updates.update(
            fixed_strategy=fixed_strategy,
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


def run_case(
    result_dir: Path,
    version: str,
    expect: str,
    *,
    runtime_disambiguation: bool = False,
    case_name: str = "original",
    append_trace: bool = False,
) -> int:
    context = _context(result_dir)
    state = _load(_state_path(result_dir))
    if runtime_disambiguation:
        _require_runtime_disambiguation(result_dir)
        _verify_source_fingerprint(result_dir)
    elif "instrumented" in str(state.get("phase") or "") or version == "fixed":
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
    trace_path = result_dir / f"{version}_assertion_trace.txt"
    trace_mode = "a" if append_trace else "w"
    trace_result = "crash" if crashed else ("clean" if proc.returncode == 0 else "error")
    with trace_path.open(trace_mode, encoding="utf-8") as trace:
        trace.write(
            f"CASE name={case_name} rc={proc.returncode} result={trace_result}\n"
        )
        trace.write(combined)
        if combined and not combined.endswith("\n"):
            trace.write("\n")
        trace.write("ENDCASE\n")
    # A fixed build that compiles cleanly but still reproduces the crash is not a
    # fixed-side witness: the staged patch did not remove the defect (commonly an
    # unrelated ARVO fix commit). Differential verification must swap to the
    # prebuilt -fix image instead of accepting this binary.
    fixed_side_invalid = version == "fixed" and crashed
    _update_state(
        result_dir,
        context,
        phase=f"{version}_ran",
        **{
            **({"fallback_required": True,
                "fixed_strategy": "fix_image_required"} if fixed_side_invalid else {}),
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
    sub.add_parser("ensure-vulnerable")
    p_apply = sub.add_parser("apply-instrumentation")
    p_apply.add_argument("--patch", required=True, type=Path)
    p_apply.add_argument("--runtime-disambiguation", action="store_true")
    sub.add_parser("compile-vulnerable")
    p_target = sub.add_parser("compile-target")
    p_target.add_argument("--version", required=True, choices=["vulnerable", "fixed"])
    p_target.add_argument("--target", default="")
    p_target.add_argument("--runtime-disambiguation", action="store_true")
    p_plan = sub.add_parser("validate-instrumentation-plan")
    p_plan.add_argument("--vulnerable-patch", required=True, type=Path)
    p_plan.add_argument("--fixed-patch", required=True, type=Path)
    p_plan.add_argument("--out", required=True, type=Path)
    p_side = sub.add_parser("validate-instrumentation-side")
    p_side.add_argument("--version", required=True, choices=["vulnerable", "fixed"])
    p_side.add_argument("--patch", required=True, type=Path)
    p_side.add_argument("--out", required=True, type=Path)
    p_switch = sub.add_parser("switch-fixed")
    p_switch.add_argument("--patch", required=True, type=Path)
    sub.add_parser("switch-fixed-image")
    sub.add_parser("reset-source")
    p_fixed = sub.add_parser("compile-fixed")
    p_fixed.add_argument("--target", default="")
    p_fixed.add_argument("--fallback-image", action="store_true")
    p_fixed.add_argument("--instrumentation-patch", type=Path)
    p_run = sub.add_parser("run")
    p_run.add_argument("--version", required=True, choices=["vulnerable", "fixed"])
    p_run.add_argument("--expect", default="any", choices=["crash", "clean", "any"])
    p_run.add_argument("--runtime-disambiguation", action="store_true")
    p_run.add_argument("--case-name", default="original")
    p_run.add_argument("--append-trace", action="store_true")
    p_cleanup = sub.add_parser("cleanup")
    p_cleanup.add_argument("--remove-images", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    if args.action == "create":
        return create(args.result_dir)
    if args.action == "ensure-vulnerable":
        return ensure_vulnerable_workspace(args.result_dir)
    if args.action == "apply-instrumentation":
        return apply_instrumentation(
            args.result_dir,
            args.patch,
            runtime_disambiguation=args.runtime_disambiguation,
        )
    if args.action == "compile-vulnerable":
        return compile_vulnerable(args.result_dir)
    if args.action == "compile-target":
        return compile_target(
            args.result_dir,
            version=args.version,
            target=args.target,
            runtime_disambiguation=args.runtime_disambiguation,
        )
    if args.action == "validate-instrumentation-plan":
        return validate_instrumentation_plan(
            args.result_dir,
            args.vulnerable_patch,
            args.fixed_patch,
            args.out,
        )
    if args.action == "validate-instrumentation-side":
        return validate_instrumentation_side(
            args.result_dir,
            args.version,
            args.patch,
            args.out,
        )
    if args.action == "switch-fixed":
        return switch_fixed(args.result_dir, args.patch)
    if args.action == "switch-fixed-image":
        return switch_fixed_image(args.result_dir)
    if args.action == "reset-source":
        return reset_source(args.result_dir)
    if args.action == "compile-fixed":
        return compile_fixed(
            args.result_dir,
            target=args.target,
            fallback_image=args.fallback_image,
            instrumentation_patch=args.instrumentation_patch,
        )
    if args.action == "run":
        return run_case(
            args.result_dir,
            args.version,
            args.expect,
            runtime_disambiguation=args.runtime_disambiguation,
            case_name=args.case_name,
            append_trace=args.append_trace,
        )
    if args.action == "cleanup":
        return cleanup(args.result_dir, args.remove_images)
    if args.action == "status":
        print(json.dumps(_load(_state_path(args.result_dir)), indent=2, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
