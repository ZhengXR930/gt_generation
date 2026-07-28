"""gt-toolkit prepare: deterministic per-sample material FETCH (NO LLM).

Pulls the ARVO vulnerable image, extracts the source tree, and stages
poc/patch/sample_state. It does NOT reproduce or build — reproduction (which may require
compiling or fixing a target that is not pre-built) stays in the GT-generator session
that runs against these already-pulled local images. This replaces only the deterministic
"materialize" work whose slow docker PULL, when entangled with the agent's turns, caused
the Claude API to drop mid-response (the dominant v1 failure). Moving the pull here — a
retryable script holding no API session — removes that failure mode.

    gt-toolkit prepare --sample sample.json --result-dir gt_results/arvo_<id>

Exit 0 when the source tree was extracted (writes prepare_report.json).
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

_CRASH_MARKERS = (
    "ERROR: AddressSanitizer", "ERROR: LeakSanitizer", "SUMMARY: ", "runtime error:",
    "MemorySanitizer", "use-of-uninitialized-value", "SEGV on unknown", "attempting double-free",
    "heap-use-after-free", "heap-buffer-overflow", "stack-buffer-overflow",
)
_CRASH_MARKER_RE = re.compile(
    r"(==\d+==\s*)?ERROR: (AddressSanitizer|MemorySanitizer|LeakSanitizer|UndefinedBehaviorSanitizer)"
    r"|AddressSanitizer:DEADLYSIGNAL"
    r"|SUMMARY: (AddressSanitizer|MemorySanitizer|UndefinedBehaviorSanitizer)"
    r"|runtime error:"
    r"|use-of-uninitialized-value"
    r"|heap-use-after-free|heap-buffer-overflow|stack-buffer-overflow",
    re.IGNORECASE,
)


def _sh(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _pull(img: str, retries: int = 3) -> bool:
    for _ in range(retries):
        try:
            if _sh(["docker", "pull", img], timeout=2400).returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
    return False


def _arvo_id(sample: dict[str, Any]) -> str:
    return str(sample.get("benchmark_id")
               or str(sample.get("sample_id", "")).replace("arvo_", "")).strip()


def _is_arvo(sample: dict[str, Any]) -> bool:
    """True for any ARVO-sourced sample -> use the prebuilt n132/arvo:<id> images.

    `source_family == "arvo"` is the reliable signal: it covers both ARVO-Meta
    samples and CyberGym samples (CyberGym repackages ARVO under the same
    benchmark ids, so those also resolve to n132/arvo:<benchmark_id>). The older
    checks (arvo_image_vul, an ARVO source_dataset prefix, an arvo_ sample_id)
    are kept as fallbacks for entries that predate source_family. Everything
    else (osv / secbench / nvd / ghsa) takes the gt-memory-env repo track.
    """
    if str(sample.get("source_family", "")).strip().lower() == "arvo":
        return True
    if sample.get("arvo_image_vul"):
        return True
    if str(sample.get("source_dataset", "")).upper().startswith("ARVO"):
        return True
    return str(sample.get("sample_id", "")).startswith("arvo_")


def prepare(sample_path: str, result_dir: str) -> dict[str, Any]:
    """Two tracks, dispatched by sample source:
      ARVO         -> pull n132/arvo:<id>-vul (fixed validation applies patch in one workspace)
      repo/secbench -> ensure the gt-memory-env image + clone repo@vulnerable_commit
                       (target is built later by the GT-generator session)
    Either way the SLOW deterministic fetch happens here without holding an agent session."""
    source_sample_path = Path(sample_path)
    sample = json.loads(source_sample_path.read_text())
    d = Path(result_dir)
    (d / "_work").mkdir(parents=True, exist_ok=True)
    sample_info_path = d / "sample_info.json"
    if source_sample_path.resolve() != sample_info_path.resolve():
        # Required-output freshness is part of runner.py's stale-file gate.
        # Preserve the public JSON contents, but refresh the staged copy's mtime
        # so Stage 00 can prove it materialized this run.
        shutil.copy(source_sample_path, sample_info_path)
    else:
        # The result-local asset may also be used as the next run's immutable input.
        # Refresh only its timestamp so the runner's stale-output gate can distinguish
        # this Stage 00 execution without rewriting its exact public contents.
        sample_info_path.touch()
    public_context = _stage_default_crash_trace(sample, source_sample_path, d)
    report = _prepare_arvo(sample, d) if _is_arvo(sample) else _prepare_repo(sample, d)
    if (
        not public_context.get("default_crash_trace_staged")
        and _is_arvo(sample)
        and report.get("prepared")
    ):
        public_context = _capture_arvo_default_crash_trace(sample, d)
    report["public_context"] = public_context
    (d / "prepare_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _stage_default_crash_trace(
    sample: dict[str, Any], source_sample_path: Path, result_dir: Path
) -> dict[str, Any]:
    """Preserve the exact crash context originally visible to the evaluated agent."""
    destination = result_dir / "default_crash_trace.txt"
    destination.unlink(missing_ok=True)
    inline = str(sample.get("default_crash_trace") or "")
    source = ""
    if inline.strip():
        destination.write_text(inline, encoding="utf-8")
        source = "sample.default_crash_trace"
    else:
        repo_root = Path(__file__).resolve().parents[2]
        raw_candidates = [
            sample.get("default_crash_trace_path"),
            sample.get("crash_trace_path"),
            sample.get("error_path"),
            source_sample_path.parent / "error.txt",
        ]
        arvo_id = _arvo_id(sample)
        if arvo_id:
            raw_candidates.append(
                repo_root
                / "external"
                / "cybergym_data_subset"
                / "data"
                / "arvo"
                / arvo_id
                / "error.txt"
            )
        for raw_candidate in raw_candidates:
            if not raw_candidate:
                continue
            candidate = Path(str(raw_candidate)).expanduser()
            if not candidate.is_absolute():
                local_candidate = source_sample_path.parent / candidate
                candidate = local_candidate if local_candidate.is_file() else repo_root / candidate
            if candidate.is_file() and candidate.stat().st_size:
                # shutil.copy (not copy2) so the staged trace gets this run's mtime;
                # copy2 preserves the source error.txt's old mtime, which trips the
                # runner's required-output freshness gate and fails Stage 00.
                shutil.copy(candidate, destination)
                source = str(candidate)
                break
    if not destination.is_file() or not destination.stat().st_size:
        public_poc = _stage_default_crash_trace_from_public_poc(
            sample, source_sample_path, result_dir
        )
        if public_poc.get("default_crash_trace_staged"):
            return public_poc
    if not destination.is_file() or not destination.stat().st_size:
        return {"default_crash_trace_staged": False}
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "default_crash_trace_staged": True,
        "source": source,
        "sha256": f"sha256:{digest}",
    }


def _stage_default_crash_trace_from_public_poc(
    sample: dict[str, Any], source_sample_path: Path, result_dir: Path
) -> dict[str, Any]:
    """Infer public crash context from local public PoC/bug-report artifacts.

    SEC-bench-style samples often store the public issue/report text as the PoC
    artifact; many include fenced sanitizer output. Use this only when a real
    sanitizer marker is present, so samples with binary inputs or command-only
    PoCs still fail the Stage 00 public-context gate instead of getting a
    synthetic placeholder.
    """
    destination = result_dir / "default_crash_trace.txt"
    for candidate in _public_poc_trace_candidates(sample, source_sample_path):
        if not candidate.is_file():
            continue
        try:
            raw = candidate.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
        trace = _extract_sanitizer_trace_text(text)
        if not trace:
            continue
        destination.write_text(trace, encoding="utf-8")
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return {
            "default_crash_trace_staged": True,
            "source": str(candidate),
            "source_kind": "public_poc_sanitizer_report",
            "sha256": f"sha256:{digest}",
        }
    return {"default_crash_trace_staged": False}


def _public_poc_trace_candidates(
    sample: dict[str, Any], source_sample_path: Path
) -> list[Path]:
    repo_root = Path(__file__).resolve().parents[2]
    sid = str(sample.get("sample_id") or "")
    candidates: list[Path] = []
    raw_paths = [sample.get("poc_path")]
    if sid:
        poc_dir = repo_root / "dataset" / "pocs" / sid
        if poc_dir.is_dir():
            raw_paths.extend(
                sorted(
                    (path for path in poc_dir.iterdir()
                     if path.is_file() and path.name != "patch.diff"),
                    key=lambda path: (path.name != "poc", path.name),
                )
            )
    for raw in raw_paths:
        if not raw:
            continue
        path = raw if isinstance(raw, Path) else Path(str(raw)).expanduser()
        path_candidates = [path]
        if not path.is_absolute():
            path_candidates.extend((source_sample_path.parent / path, repo_root / path, repo_root / "dataset" / path))
        for resolved in path_candidates:
            if resolved not in candidates:
                candidates.append(resolved)
    return candidates


def _extract_sanitizer_trace_text(text: str) -> str:
    """Return a sanitizer crash block from plain text or saved GitHub HTML."""
    for candidate in _possible_trace_texts(text):
        normalized = _normalize_trace_text(candidate)
        match = _CRASH_MARKER_RE.search(normalized)
        if not match:
            continue
        start = normalized.rfind("\n", 0, match.start()) + 1
        previous = normalized.rfind("\n", 0, max(0, start - 1))
        if previous >= 0:
            previous_line = normalized[previous + 1:start].strip()
            if previous_line and set(previous_line) <= {"="}:
                start = previous + 1
        end = len(normalized)
        for marker in ("==ABORTING", "ABORTING"):
            pos = normalized.find(marker, match.end())
            if pos >= 0:
                end = normalized.find("\n", pos)
                end = len(normalized) if end < 0 else end + 1
                break
        trace = normalized[start:end].strip()
        if _CRASH_MARKER_RE.search(trace):
            return trace + "\n"
    return ""


def _possible_trace_texts(text: str) -> list[str]:
    """Prefer explicit GitHub clipboard snippets before the whole saved page."""
    values: list[str] = []
    for match in re.finditer(r'data-snippet-clipboard-copy-content="([^"]*)"', text, re.DOTALL):
        snippet = html.unescape(match.group(1))
        try:
            snippet = bytes(snippet, "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            pass
        if _CRASH_MARKER_RE.search(snippet):
            values.append(snippet)
    values.append(text)
    return values


def _normalize_trace_text(text: str) -> str:
    decoded = html.unescape(text)
    if "\\n" in decoded and decoded.count("\\n") > decoded.count("\n"):
        try:
            decoded = bytes(decoded, "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            pass
    if "<" in decoded and ">" in decoded:
        decoded = re.sub(r"<[^>]+>", "", decoded)
        decoded = html.unescape(decoded)
    return decoded.replace("\r\n", "\n").replace("\r", "\n")


def _capture_arvo_default_crash_trace(
    sample: dict[str, Any], result_dir: Path
) -> dict[str, Any]:
    """Capture CyberGym's level-2 public `error.txt` equivalent from the stock image."""
    arvo_id = _arvo_id(sample)
    image = str(sample.get("arvo_image_vul") or f"n132/arvo:{arvo_id}-vul")
    poc = (result_dir / "poc").resolve()
    if not arvo_id or not poc.is_file():
        return {"default_crash_trace_staged": False}
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        f"{poc}:/tmp/poc:ro",
        "--entrypoint",
        "/bin/bash",
        image,
        "-c",
        "/bin/arvo run",
    ]
    try:
        completed = _sh(command, timeout=600)
    except subprocess.TimeoutExpired:
        return {
            "default_crash_trace_staged": False,
            "capture_error": "stock vulnerable run timed out",
        }
    output = completed.stdout + completed.stderr
    if not output.strip():
        return {
            "default_crash_trace_staged": False,
            "capture_error": "stock vulnerable run produced no output",
            "returncode": completed.returncode,
        }
    destination = result_dir / "default_crash_trace.txt"
    destination.write_text(output, encoding="utf-8")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "default_crash_trace_staged": True,
        "source": "stock vulnerable image run with staged PoC",
        "sha256": f"sha256:{digest}",
        "returncode": completed.returncode,
    }


def _prepare_arvo(sample: dict[str, Any], d: Path) -> dict[str, Any]:
    aid = _arvo_id(sample)
    if not aid:
        return {"prepared": False, "reason": "no arvo/benchmark id in sample"}
    vul, fix = f"n132/arvo:{aid}-vul", f"n132/arvo:{aid}-fix"
    if not _pull(vul):
        return {"prepared": False, "track": "arvo", "reason": f"pull failed: {vul}"}
    cid = _sh(["docker", "create", vul]).stdout.strip()
    src = d / "_work" / "src"
    shutil.rmtree(src, ignore_errors=True)
    if cid:
        _sh(["docker", "cp", f"{cid}:/src", str(src)])
        _sh(["docker", "cp", f"{cid}:/tmp/poc", str(d / "poc")])
        _sh(["docker", "rm", cid])
    if (d / "poc").is_file():
        # docker cp preserves the image's historical mtime; refresh only the staged
        # copy so the runner can prove this Stage 00 materialized it in this run.
        (d / "poc").touch()
    (d / "build.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        'ASSET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        f'docker run --rm --platform linux/amd64 -v "${{ASSET_DIR}}/poc:/tmp/poc:ro" '
        f"--entrypoint /bin/bash {vul} -c '/bin/arvo run'\n"
    )
    (d / "build.sh").chmod(0o755)
    _stage_patch(sample, d, aid)
    _init_state(f"arvo_{aid}", d)
    target = _detect_arvo_target(vul)
    fix_available = bool(_sh(["docker", "images", "-q", fix]).stdout.strip())
    return {"track": "arvo", "arvo_id": aid, "vul_image": vul, "fix_image": fix,
            "fix_image_pulled": False, "fix_image_available": fix_available,
            "fix_strategy": "patch_incremental", "target": target,
            "workspace_container": f"gt-arvo_{aid}-workspace",
            "source": src.exists(), "poc": (d / "poc").exists(),
            "patch": (d / "patch.diff").exists(), "prepared": bool(src.exists())}


def _detect_arvo_target(image: str) -> str:
    script = _sh([
        "docker", "run", "--rm", "--entrypoint", "/bin/cat", image, "/bin/arvo"
    ]).stdout
    match = re.search(r"/out/([A-Za-z0-9_.-]+)\s+/tmp/poc", script)
    return match.group(1) if match else ""


def _ensure_memory_env(tag: str | None = None, context: str | Path | None = None) -> bool:
    tag = tag or os.environ.get("GT_REPO_DOCKER_IMAGE", "gt-memory-env:latest")
    context = Path(
        context
        or os.environ.get("GT_REPO_DOCKER_CONTEXT", "")
        or Path(__file__).resolve().parents[2] / "docker" / "gt-memory-env"
    ).resolve()
    if _sh(["docker", "images", "-q", tag]).stdout.strip():
        return True
    return _sh(["docker", "build", "-t", tag, str(context)], timeout=3000).returncode == 0


def _prepare_repo(sample: dict[str, Any], d: Path) -> dict[str, Any]:
    """SEC-bench / OSS-Fuzz / repo-based: no pre-built image. Build the shared
    gt-memory-env, clone the repo at the vulnerable commit, stage poc + patch. The
    GT-generator session then builds the target (install project deps, sanitizer, fix)
    inside gt-memory-env and reproduces it."""
    sid = str(sample.get("sample_id") or "")
    repo = sample.get("repo") or sample.get("repo_url")
    vcommit = sample.get("vulnerable_commit")
    if not repo:
        return {"prepared": False, "track": "repo", "reason": "no repo url"}
    env_image = os.environ.get("GT_REPO_DOCKER_IMAGE", "gt-memory-env:latest")
    env_context = Path(
        os.environ.get("GT_REPO_DOCKER_CONTEXT", "")
        or Path(__file__).resolve().parents[2] / "docker" / "gt-memory-env"
    ).resolve()
    env_ok = _ensure_memory_env(env_image, env_context)
    src = d / "_work" / "src"
    shutil.rmtree(src, ignore_errors=True)
    if _sh(["git", "clone", str(repo), str(src)], timeout=1800).returncode != 0:
        return {"prepared": False, "track": "repo", "reason": f"clone failed: {repo}", "env": env_ok}
    if vcommit:
        _sh(["git", "-C", str(src), "checkout", "-q", str(vcommit)])
    # patch = diff between vulnerable and fix commit (deterministic)
    fcommit = sample.get("fix_commit")
    if vcommit and fcommit:
        diff = _sh(["git", "-C", str(src), "diff", str(vcommit), str(fcommit)])
        if diff.stdout.strip():
            (d / "patch.diff").write_text(diff.stdout)
    if not (d / "patch.diff").exists():
        _stage_patch(sample, d, sid)
    staged_poc = _stage_repo_poc(sample, d, sid)
    (d / "build.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        'ASSET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        f"IMAGE={shlex.quote(env_image)}\n"
        'if [[ $# -eq 0 ]]; then\n'
        '  echo "usage: $0 <build-or-reproduction command>" >&2\n'
        '  exit 2\n'
        'fi\n'
        'exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp '
        '-v "${ASSET_DIR}:/gt" -w /gt/_work/src "${IMAGE}" '
        'bash -lc "$*"\n'
    )
    (d / "build.sh").chmod(0o755)
    _init_state(sid, d)
    return {"track": "repo/secbench", "sample_id": sid, "env": env_image,
            "env_context": str(env_context), "env_ok": env_ok,
            "repo": repo, "vulnerable_commit": vcommit, "source": src.exists(),
            "poc": (d / "poc").exists(), "poc_source": staged_poc,
            "patch": (d / "patch.diff").exists(),
            "prepared": bool(src.exists())}


def _stage_repo_poc(sample: dict[str, Any], d: Path, sid: str) -> str:
    """Stage the actual runnable testcase as /gt/poc for repo-based samples.

    Older SEC-bench imports stored the public issue/report text at
    dataset/pocs/<sid>/poc. If prepare blindly picks the largest file in that
    directory, a report or downloaded archive can be staged instead of the
    testcase. Prefer explicit sample metadata and the testcase/ subdirectory;
    only then fall back to legacy flat files while filtering obvious metadata.
    """
    destination = d / "poc"
    destination.unlink(missing_ok=True)
    repo_root = Path(__file__).resolve().parents[2]
    raw_candidates = (
        sample.get("poc_artifact_path"),
        sample.get("poc_path"),
    )
    for raw in raw_candidates:
        for candidate in _resolve_asset_candidates(raw, repo_root):
            if candidate.is_file() and candidate.stat().st_size:
                shutil.copy(candidate, destination)
                return str(candidate)

    pocdir = repo_root / "dataset" / "pocs" / sid
    testcase_dir = pocdir / "testcase"
    if testcase_dir.is_dir():
        files = sorted((path for path in testcase_dir.rglob("*") if path.is_file()))
        if files:
            chosen = max(files, key=lambda path: path.stat().st_size)
            shutil.copy(chosen, destination)
            return str(chosen)

    if pocdir.is_dir():
        ignored_names = {
            "patch.diff",
            "bug_report.md",
            "public_report.md",
            "README",
            "README.md",
            "sample_info.json",
            "default_crash_trace.txt",
        }
        ignored_suffixes = {".zip", ".tar", ".gz", ".tgz", ".xz", ".bz2", ".html", ".md", ".txt"}
        files = [
            path for path in pocdir.iterdir()
            if (
                path.is_file()
                and path.name not in ignored_names
                and path.suffix.lower() not in ignored_suffixes
            )
        ]
        if files:
            chosen = max(files, key=lambda path: path.stat().st_size)
            shutil.copy(chosen, destination)
            return str(chosen)
    return ""


def _resolve_asset_candidates(raw: Any, repo_root: Path) -> list[Path]:
    if not raw:
        return []
    candidate = Path(str(raw)).expanduser()
    if candidate.is_absolute():
        return [candidate]
    return [
        repo_root / candidate,
        repo_root / "dataset" / candidate,
    ]


def _stage_patch(sample: dict[str, Any], d: Path, sid: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    raw_candidates = (
        sample.get("patch_diff_path"),
        sample.get("patch_path"),
        f"arvo_patches/{sid}.diff",
        f"dataset/arvo_patches/{sid}.diff",
        f"dataset/arvo/{sid}/patch.diff",
        f"dataset/pocs/{sid}/patch.diff",
    )
    for raw in raw_candidates:
        if not raw:
            continue
        candidate = Path(str(raw))
        candidates = [candidate]
        if not candidate.is_absolute():
            candidates.extend((repo_root / candidate, repo_root / "dataset" / candidate))
        for resolved in candidates:
            if resolved.is_file():
                # Required-output freshness is part of the runner's stale-file gate;
                # preserve contents and mode, but give the staged copy this run's mtime.
                shutil.copy(resolved, d / "patch.diff")
                return


def _init_state(sample_id: str, d: Path) -> None:
    try:
        from . import state as _state
        _state.main(["init", "--sample-id", sample_id, "--output", str(d / "sample_state.json")])
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gt-toolkit prepare", description=__doc__)
    ap.add_argument("--sample", required=True)
    ap.add_argument("--result-dir", required=True)
    ns = ap.parse_args(argv)
    res = prepare(ns.sample, ns.result_dir)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("prepared") else 1


if __name__ == "__main__":
    raise SystemExit(main())
