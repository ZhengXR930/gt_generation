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
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request
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
    return subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", timeout=timeout
    )


def _repo_clone_candidates(repo: str) -> list[str]:
    mirror_prefix = "https://gitlab.gnome.org/GNOME/"
    if repo.startswith(mirror_prefix) and repo.endswith(".git"):
        project = repo[len(mirror_prefix): -len(".git")]
        # GNOME GitLab can be very slow from this environment; GitHub is the
        # official project mirror and is much more reliable for bulk cloning.
        return [f"https://github.com/GNOME/{project}.git", repo]
    return [repo]


def _repo_cache_root() -> Path:
    return Path(
        os.environ.get("GT_REPO_CACHE_DIR", "")
        or Path(__file__).resolve().parents[2] / "external" / "repo-cache"
    )


def _repo_cache_dir(repo: str) -> Path:
    parsed = urllib.parse.urlparse(repo)
    if parsed.scheme and parsed.netloc:
        key = f"{parsed.netloc}{parsed.path}"
    else:
        key = repo
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key.strip("/"))
    if not key:
        key = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:16]
    return _repo_cache_root() / f"{key}.git"


def _commitish_exists(repo: Path, commitish: str) -> bool:
    if not commitish:
        return True
    return _sh(["git", "-C", str(repo), "cat-file", "-e", f"{commitish}^{{commit}}"], timeout=30).returncode == 0


def _ensure_repo_commit(repo: Path, remote: str, commitish: str) -> dict[str, Any]:
    status: dict[str, Any] = {"commit": commitish}
    shallow = _sh(["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"], timeout=30)
    status["was_shallow"] = shallow.stdout.strip() == "true"
    if status["was_shallow"]:
        unshallow = _sh(["git", "-C", str(repo), "fetch", "--unshallow", "--no-tags", "origin"], timeout=2400)
        status["unshallow_returncode"] = unshallow.returncode
        if unshallow.returncode != 0:
            status["unshallow_stderr"] = (unshallow.stderr or "")[-2000:]
    status["present"] = _commitish_exists(repo, commitish)
    if not commitish or status["present"]:
        if commitish:
            _sh(["git", "-C", str(repo), "update-ref", f"refs/gt-cache/{commitish}", commitish], timeout=30)
        return status
    fetched = _sh(["git", "-C", str(repo), "fetch", "--no-tags", "origin", commitish], timeout=1800)
    status["fetch_returncode"] = fetched.returncode
    status["present"] = _commitish_exists(repo, commitish)
    if status["present"]:
        _sh(["git", "-C", str(repo), "update-ref", f"refs/gt-cache/{commitish}", commitish], timeout=30)
    if not status["present"]:
        status["stderr"] = (fetched.stderr or "")[-2000:]
    return status


def _materialize_repo_checkout(
    repo: str, src: Path, commits: list[str]
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Checkout repo into src using a reusable local object cache.

    Repo-track samples often share large upstream projects. A normal full clone
    per sample is slow and repeatedly redownloads the same history. Keep a bare
    cache per remote URL, fetch only the required commits, and clone locally.
    """
    clone_errors: list[dict[str, Any]] = []
    for clone_repo in _repo_clone_candidates(str(repo)):
        cache = _repo_cache_dir(clone_repo)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache_status: dict[str, Any] = {
            "cache": str(cache),
            "source": clone_repo,
            "reused": cache.is_dir(),
        }
        if not (cache / "objects").is_dir():
            shutil.rmtree(cache, ignore_errors=True)
            created = _sh(["git", "init", "--bare", str(cache)], timeout=120)
            if created.returncode != 0:
                clone_errors.append({
                    "repo": clone_repo,
                    "reason": "cache init failed",
                    "stderr": (created.stderr or "")[-2000:],
                })
                continue
            remote = _sh(["git", "-C", str(cache), "remote", "add", "origin", clone_repo], timeout=60)
            if remote.returncode != 0:
                clone_errors.append({
                    "repo": clone_repo,
                    "reason": "cache remote add failed",
                    "stderr": (remote.stderr or "")[-2000:],
                })
                continue
        else:
            _sh(["git", "-C", str(cache), "remote", "set-url", "origin", clone_repo], timeout=60)

        fetch_reports = []
        ok = True
        for commit in [c for c in commits if c]:
            report = _ensure_repo_commit(cache, clone_repo, commit)
            fetch_reports.append(report)
            if not report.get("present"):
                ok = False
        cache_status["fetches"] = fetch_reports
        if not ok:
            clone_errors.append({
                "repo": clone_repo,
                "reason": "required commit fetch failed",
                "cache": str(cache),
                "fetches": fetch_reports,
            })
            continue

        shutil.rmtree(src, ignore_errors=True)
        cloned = _sh(["git", "clone", "--no-checkout", str(cache), str(src)], timeout=600)
        if cloned.returncode != 0:
            clone_errors.append({
                "repo": clone_repo,
                "reason": "local clone from cache failed",
                "cache": str(cache),
                "stderr": (cloned.stderr or "")[-2000:],
            })
            continue
        _sh(["git", "-C", str(src), "remote", "set-url", "origin", clone_repo], timeout=60)
        for commit in [c for c in commits if c]:
            fetched_local = _sh(
                [
                    "git", "-C", str(src), "fetch", "--no-tags", str(cache),
                    f"refs/gt-cache/{commit}:refs/gt-cache/{commit}",
                ],
                timeout=300,
            )
            if fetched_local.returncode != 0:
                clone_errors.append({
                    "repo": clone_repo,
                    "reason": "local fetch from cache failed",
                    "cache": str(cache),
                    "commit": commit,
                    "stderr": (fetched_local.stderr or "")[-2000:],
                })
                shutil.rmtree(src, ignore_errors=True)
                break
        if not src.is_dir():
            continue
        first_commit = next((c for c in commits if c), "")
        if first_commit:
            checked = _sh(["git", "-C", str(src), "checkout", "-q", first_commit], timeout=300)
            if checked.returncode != 0:
                clone_errors.append({
                    "repo": clone_repo,
                    "reason": "checkout required commit failed",
                    "cache": str(cache),
                    "commit": first_commit,
                    "stderr": (checked.stderr or "")[-2000:],
                })
                shutil.rmtree(src, ignore_errors=True)
                continue
            cache_status["checked_out"] = first_commit
        return clone_repo, clone_errors, cache_status
    return "", clone_errors, {}


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
    source_family = str(sample.get("source_family", "")).strip().lower()
    if source_family and source_family != "arvo":
        return False
    if source_family == "arvo":
        return True
    if sample.get("arvo_image_vul"):
        return True
    if str(sample.get("source_dataset", "")).upper().startswith("ARVO"):
        return True
    return str(sample.get("sample_id", "")).startswith("arvo_")


def is_arvo_sample(sample: dict[str, Any]) -> bool:
    """Public source-family predicate used by resume orchestration."""
    return _is_arvo(sample)


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


def hydrate_runtime(result_dir: str | Path, *, force: bool = False) -> dict[str, Any]:
    """Restore the local execution workspace for a compact GT package.

    Compact GT packages intentionally do not commit `_work/` or compiled output.
    This helper rebuilds the deterministic source/material scaffold from the
    package's `sample_info.json` so local PoC evaluation can run on another
    machine without depending on stale absolute paths.
    """
    result_path = Path(result_dir)
    sample_info = result_path / "sample_info.json"
    if not sample_info.is_file():
        return {
            "prepared": False,
            "hydrated": False,
            "reason": f"missing sample_info.json: {sample_info}",
        }
    src = result_path / "_work" / "src"
    if not force and src.is_dir() and any(src.iterdir()):
        return {
            "prepared": True,
            "hydrated": False,
            "reused": True,
            "source": str(src),
        }
    preserved = _snapshot_existing_durable_files(result_path)
    report = prepare(str(sample_info), str(result_path))
    _restore_existing_durable_files(result_path, preserved)
    report["hydrated"] = bool(report.get("prepared"))
    return report


_HYDRATE_PRESERVED_FILES = (
    "build.sh",
    "poc",
    "ground_truth.json",
    "verified_invariants.json",
    "verified_assertions.json",
    "field_bindings.json",
    "assertion_results.json",
    "perturbation_results.json",
    "event_locations.json",
    "evidence_commitment.json",
    "generation_provenance.json",
    "generation_timing.json",
    "reachability_report.json",
    "context_trace.json",
    "default_crash_trace.txt",
    "sanitizer_trace.txt",
    "reproduction_report.json",
    "runtime_spec.json",
)


def _snapshot_existing_durable_files(result_path: Path) -> dict[str, bytes]:
    saved: dict[str, bytes] = {}
    for name in _HYDRATE_PRESERVED_FILES:
        path = result_path / name
        if path.is_file():
            saved[name] = path.read_bytes()
    return saved


def _restore_existing_durable_files(
    result_path: Path, saved: dict[str, bytes]
) -> None:
    for name, data in saved.items():
        path = result_path / name
        if not path.is_file() or path.read_bytes() != data:
            path.write_bytes(data)


def _stage_default_crash_trace(
    sample: dict[str, Any], source_sample_path: Path, result_dir: Path
) -> dict[str, Any]:
    """Preserve the exact crash context originally visible to the evaluated agent."""
    destination = result_dir / "default_crash_trace.txt"
    if destination.is_file() or destination.is_symlink():
        destination.unlink()
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
        issue_context = _public_issue_context(sample)
        if issue_context:
            destination.write_text(issue_context, encoding="utf-8")
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            return {
                "default_crash_trace_staged": True,
                "source": "sample.issue_description",
                "source_kind": "public_issue_context",
                "sha256": f"sha256:{digest}",
            }
    if not destination.is_file() or not destination.stat().st_size:
        return {"default_crash_trace_staged": False}
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "default_crash_trace_staged": True,
        "source": source,
        "sha256": f"sha256:{digest}",
    }


def _public_issue_context(sample: dict[str, Any]) -> str:
    """Fallback public context for repo samples that ship a PoC but no trace.

    Some OSV.dev Git/NVD records in this dataset have a local PoC and fix diff
    but no saved sanitizer log. Stage 01 still has to reproduce the crash and
    write the real sanitizer trace; this text only preserves the public problem
    statement so Stage 01/02 do not start from an empty context.
    """
    for key in ("original_bug_description", "issue_description", "summary", "details"):
        value = sample.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip() + "\n"
        if isinstance(value, dict):
            text = value.get("original") or value.get("summary") or value.get("details")
            if isinstance(text, str) and text.strip():
                return text.strip() + "\n"
    return ""


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
    patch_applies = _patch_applies(d / "patch.diff", src)
    return {"track": "arvo", "arvo_id": aid, "vul_image": vul, "fix_image": fix,
            "fix_image_pulled": False, "fix_image_available": fix_available,
            "patch_applies": patch_applies,
            "fix_strategy": "patch_incremental" if patch_applies else "fix_image_required",
            "target": target,
            "workspace_container": f"gt-arvo_{aid}-workspace",
            "source": src.exists(), "poc": (d / "poc").exists(),
            "patch": (d / "patch.diff").exists(), "prepared": bool(src.exists())}


def _patch_applies(patch: Path, src_root: Path) -> bool:
    """Static check: can the staged fix commit actually apply to this source?

    ARVO records a fix commit that is frequently an unrelated build/docs/version
    change or targets a different subsystem. A patch that cannot apply means the
    incremental fixed rebuild would silently produce a binary identical to the
    vulnerable one, so Stage 04 must use the prebuilt -fix image instead.
    """
    if not patch.is_file() or not src_root.is_dir():
        return False
    roots = [src_root] + [c for c in sorted(src_root.iterdir()) if c.is_dir()]
    for root in roots[:8]:
        for strip in ("-p1", "-p2"):
            probe = _sh(["git", "-C", str(root), "apply", "--check", strip,
                         str(patch.resolve())])
            if probe.returncode == 0:
                return True
    return False


def ensure_arvo_resume_source(sample: dict[str, Any], result_dir: str | Path) -> dict[str, Any]:
    """Restore only the exact ARVO source needed by a resumed agent stage.

    Copy through a temporary directory so an interrupted Docker copy can never
    be mistaken for a complete source tree. Public PoC, patch, and GT artifacts
    are deliberately left untouched.
    """
    d = Path(result_dir)
    src = d / "_work" / "src"
    if src.is_dir() and any(src.iterdir()):
        return {"prepared": True, "source": str(src), "reused": True}
    if not _is_arvo(sample):
        return {
            "prepared": False,
            "reason": "resume source hydration currently requires an ARVO sample",
        }
    aid = _arvo_id(sample)
    if not aid:
        return {"prepared": False, "reason": "no arvo/benchmark id in sample"}

    image = f"n132/arvo:{aid}-vul"
    if not _pull(image):
        return {"prepared": False, "reason": f"pull failed: {image}"}

    work = d / "_work"
    work.mkdir(parents=True, exist_ok=True)
    temporary = work / f".resume-src-{os.getpid()}"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir()
    cid = ""
    try:
        created = _sh(["docker", "create", image])
        cid = created.stdout.strip()
        if created.returncode != 0 or not cid:
            return {
                "prepared": False,
                "reason": f"docker create failed: {image}",
                "stderr": created.stderr.strip(),
            }
        copied = _sh(["docker", "cp", f"{cid}:/src/.", str(temporary)])
        if copied.returncode != 0:
            return {
                "prepared": False,
                "reason": f"docker cp failed: {image}:/src",
                "stderr": copied.stderr.strip(),
            }
        if not any(temporary.iterdir()):
            return {"prepared": False, "reason": f"empty source tree: {image}:/src"}
        shutil.rmtree(src, ignore_errors=True)
        temporary.replace(src)
        return {
            "prepared": True,
            "source": str(src),
            "reused": False,
            "image": image,
        }
    finally:
        if cid:
            _sh(["docker", "rm", "-f", cid])
        shutil.rmtree(temporary, ignore_errors=True)


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


def repo_track_build_sh(env_image: str | None = None) -> str:
    """Return the portable repo-track Docker wrapper used by non-ARVO samples."""
    image = shlex.quote(env_image or os.environ.get("GT_REPO_DOCKER_IMAGE", "gt-memory-env:latest"))
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        'ASSET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        f"IMAGE={image}\n"
        'REPO_ROOT="${GT_REPO_ROOT:-}"\n'
        'if [[ -z "${REPO_ROOT}" ]]; then\n'
        '  if git -C "${ASSET_DIR}" rev-parse --show-toplevel >/dev/null 2>&1; then\n'
        '    REPO_ROOT="$(git -C "${ASSET_DIR}" rev-parse --show-toplevel)"\n'
        '  else\n'
        '    REPO_ROOT="$(cd "${ASSET_DIR}/../.." && pwd)"\n'
        '  fi\n'
        'fi\n'
        'if [[ ! -d "${REPO_ROOT}/gt_generation" ]]; then\n'
        '  echo "cannot locate gt_generation repo root; set GT_REPO_ROOT" >&2\n'
        '  exit 2\n'
        'fi\n'
        'if [[ $# -eq 0 ]]; then\n'
        '  echo "usage: $0 <build-or-reproduction command>" >&2\n'
        '  exit 2\n'
        'fi\n'
        'PROXY_ENV=()\n'
        'for _v in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do\n'
        '  if [[ -n "${!_v:-}" ]]; then PROXY_ENV+=(-e "${_v}=${!_v}"); fi\n'
        'done\n'
        'USER_ENV=(--user "$(id -u):$(id -g)")\n'
        'if [[ "${GT_BUILD_AS_ROOT:-0}" == "1" ]]; then USER_ENV=(); fi\n'
        'exec docker run --rm "${USER_ENV[@]}" -e HOME=/tmp '
        '"${PROXY_ENV[@]}" '
        '-v "${ASSET_DIR}:/gt" '
        '-v "${REPO_ROOT}:/repo:ro" '
        '-w /gt/_work/src "${IMAGE}" '
        'bash -lc "$*"\n'
    )


def _poc_source_dir(sample: dict[str, Any], sid: str) -> Path | None:
    root = Path(__file__).resolve().parents[2] / "dataset"
    candidates = []
    poc_path = str(sample.get("poc_path") or "")
    if poc_path:
        candidate = (root / poc_path).resolve()
        for parent in [candidate] + list(candidate.parents):
            if parent.name == sid and parent.parent.name == "pocs":
                candidates.append(parent)
                break
    candidates.append(root / "pocs" / sid)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _oss_fuzz_checkout_root() -> Path:
    return Path(__file__).resolve().parents[2] / "external" / "oss-fuzz"


def _ensure_oss_fuzz_project_dir(project: str) -> tuple[Path | None, dict[str, Any]]:
    """Return the official google/oss-fuzz projects/<project> directory.

    The dataset config stores snippets such as build.sh, but some projects rely
    on Dockerfile context, helper scripts, or project-level metadata. Keep a
    sparse checkout of the official repository under external/oss-fuzz and stage
    the project directory from there.
    """
    project = project.strip()
    status: dict[str, Any] = {"project": project, "available": False}
    if not project:
        status["reason"] = "no oss-fuzz project name"
        return None, status
    checkout = _oss_fuzz_checkout_root()
    project_dir = checkout / "projects" / project
    if (checkout / ".git").is_dir():
        rev = _sh(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], timeout=30)
        exists = _sh(
            ["git", "-C", str(checkout), "cat-file", "-e", f"HEAD:projects/{project}"],
            timeout=30,
        )
        if exists.returncode == 0:
            status.update({
                "available": True,
                "path": str(project_dir),
                "checkout": str(checkout),
                "commit": rev.stdout.strip() if rev.returncode == 0 else "",
                "reused": project_dir.is_dir(),
                "tree_available": True,
            })
            return project_dir, status
    if project_dir.is_dir():
        rev = _sh(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], timeout=30)
        status.update({
            "available": True,
            "path": str(project_dir),
            "checkout": str(checkout),
            "commit": rev.stdout.strip() if rev.returncode == 0 else "",
            "reused": True,
        })
        return project_dir, status

    checkout.parent.mkdir(parents=True, exist_ok=True)
    if not (checkout / ".git").is_dir():
        shutil.rmtree(checkout, ignore_errors=True)
        cloned = _sh([
            "git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout",
            "https://github.com/google/oss-fuzz.git", str(checkout)
        ], timeout=1800)
        if cloned.returncode != 0:
            status.update({
                "reason": "clone google/oss-fuzz failed",
                "stderr": cloned.stderr[-2000:],
            })
            return None, status
    rev = _sh(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], timeout=30)
    exists = _sh(
        ["git", "-C", str(checkout), "cat-file", "-e", f"HEAD:projects/{project}"],
        timeout=30,
    )
    if exists.returncode == 0:
        status.update({
            "available": True,
            "path": str(project_dir),
            "checkout": str(checkout),
            "commit": rev.stdout.strip() if rev.returncode == 0 else "",
            "reused": False,
            "tree_available": True,
        })
        return project_dir, status

    _sh(["git", "-C", str(checkout), "config", "core.sparseCheckout", "true"], timeout=30)
    sparse_file = checkout / ".git" / "info" / "sparse-checkout"
    existing = sparse_file.read_text(encoding="utf-8").splitlines() if sparse_file.is_file() else []
    wanted = f"projects/{project}/*"
    if wanted not in existing:
        sparse_file.parent.mkdir(parents=True, exist_ok=True)
        sparse_file.write_text("\n".join(existing + [wanted]) + "\n", encoding="utf-8")
    checked = _sh(["git", "-C", str(checkout), "checkout", "HEAD"], timeout=1800)
    if checked.returncode != 0:
        status.update({
            "reason": "sparse checkout failed",
            "stderr": checked.stderr[-2000:],
        })
        return None, status
    if not project_dir.is_dir():
        status["reason"] = f"projects/{project} not present in google/oss-fuzz"
        return None, status
    rev = _sh(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], timeout=30)
    status.update({
        "available": True,
        "path": str(project_dir),
        "checkout": str(checkout),
        "commit": rev.stdout.strip() if rev.returncode == 0 else "",
        "reused": False,
    })
    return project_dir, status


def _oss_fuzz_project_commit_before(
    checkout: Path, project: str, reference_time: str
) -> tuple[str, dict[str, Any]]:
    status: dict[str, Any] = {"reference_time": reference_time}
    if not reference_time:
        return "", status
    unshallow = _sh(["git", "-C", str(checkout), "rev-parse", "--is-shallow-repository"], timeout=30)
    if unshallow.stdout.strip() == "true":
        fetched = _sh(
            ["git", "-C", str(checkout), "fetch", "--unshallow", "--filter=blob:none", "origin"],
            timeout=2400,
        )
        status["unshallow_fetch"] = fetched.returncode == 0
        if fetched.returncode != 0:
            status["unshallow_fetch_error"] = fetched.stderr[-1000:]
    rev = _sh(
        [
            "git", "-C", str(checkout), "rev-list", "-1",
            f"--before={reference_time}", "HEAD", "--", f"projects/{project}",
        ],
        timeout=120,
    )
    commit = rev.stdout.strip()
    status["historical_commit"] = commit[:12] if commit else ""
    if rev.returncode != 0 or not commit:
        status["historical_commit_error"] = (rev.stderr or "no project commit before reference time")[-1000:]
        return "", status
    exists = _sh(
        ["git", "-C", str(checkout), "cat-file", "-e", f"{commit}:projects/{project}"],
        timeout=30,
    )
    if exists.returncode != 0:
        status["historical_commit_error"] = f"projects/{project} absent at {commit[:12]}"
        return "", status
    return commit, status


def _export_oss_fuzz_project_at_commit(
    checkout: Path, project: str, commit: str, target: Path
) -> dict[str, Any]:
    export_status: dict[str, Any] = {
        "source": "google/oss-fuzz",
        "historical_commit": commit[:12],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    archived = subprocess.run(
        ["git", "-C", str(checkout), "archive", "--format=tar", f"{commit}:projects/{project}"],
        capture_output=True,
        timeout=300,
    )
    if archived.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        export_status.update({
            "exported": False,
            "checkout_error": archived.stderr.decode("utf-8", errors="replace")[-1000:],
        })
        return export_status
    try:
        with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as tar:
            tar.extractall(tmp)
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        export_status.update({
            "exported": False,
            "tar_error": str(exc),
        })
        return export_status
    if not any(tmp.iterdir()):
        shutil.rmtree(tmp, ignore_errors=True)
        export_status.update({
            "exported": False,
            "tar_error": f"projects/{project} archive was empty",
        })
        return export_status
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(tmp), str(target))
    export_status["exported"] = True
    return export_status


def _stage_oss_fuzz_project_context(
    d: Path, project: str, reference_time: str = ""
) -> dict[str, Any]:
    project_dir, status = _ensure_oss_fuzz_project_dir(project)
    target = d / "oss_fuzz_project"
    shutil.rmtree(target, ignore_errors=True)
    if not project_dir:
        return status
    historical_status: dict[str, Any] = {}
    commit = ""
    checkout = Path(str(status.get("checkout") or _oss_fuzz_checkout_root()))
    if reference_time and checkout.is_dir():
        commit, historical_status = _oss_fuzz_project_commit_before(
            checkout, project, reference_time
        )
        status["historical_checkout"] = historical_status
    if commit:
        exported = _export_oss_fuzz_project_at_commit(checkout, project, commit, target)
        status["historical_export"] = exported
        if not exported.get("exported"):
            current_export = _export_oss_fuzz_project_at_commit(checkout, project, "HEAD", target)
            status["historical_export_fallback"] = current_export
            if not current_export.get("exported"):
                shutil.copytree(project_dir, target)
                status["historical_export_fallback_copytree"] = "current_sparse_checkout"
    else:
        current_export = _export_oss_fuzz_project_at_commit(checkout, project, "HEAD", target)
        status["current_export"] = current_export
        if not current_export.get("exported"):
            shutil.copytree(project_dir, target)
            status["current_export_fallback"] = "current_sparse_checkout"
    file_count = sum(1 for path in target.rglob("*") if path.is_file())
    status.update({
        "staged": True,
        "staged_path": str(target),
        "files": file_count,
        "has_dockerfile": (target / "Dockerfile").is_file(),
        "has_build_sh": (target / "build.sh").is_file(),
        "has_project_yaml": (target / "project.yaml").is_file(),
    })
    status["dockerfile_git_clones"] = _stage_oss_fuzz_dockerfile_clones(
        d, target, project, reference_time
    )
    status["dockerfile_setup_script"] = _stage_oss_fuzz_dockerfile_setup_script(
        d, target, project
    )
    status["dockerfile_workdir"] = _oss_fuzz_dockerfile_workdir(target / "Dockerfile")
    return status


def _stage_oss_fuzz_dockerfile_clones(
    d: Path, project_dir: Path, project: str, reference_time: str = ""
) -> list[dict[str, Any]]:
    """Stage helper repositories cloned by an OSS-Fuzz Dockerfile.

    The vulnerable project itself is already checked out at its benchmark commit
    under _work/src. Helper repos such as curl-fuzzer are not, and staging only
    projects/<project>/build.sh leaves the agent without scripts referenced by
    the official recipe. Clone those helpers deterministically from the URLs in
    Dockerfile and record the resulting commit.
    """
    dockerfile = project_dir / "Dockerfile"
    if not dockerfile.is_file():
        return []
    clone_specs = _oss_fuzz_dockerfile_clone_specs(dockerfile)
    staged_root = d / "oss_fuzz_src"
    shutil.rmtree(staged_root, ignore_errors=True)
    results: list[dict[str, Any]] = []
    normalized_project = _normalize_repo_name(project)
    for spec in clone_specs:
        raw_repo_name = _repo_basename_from_url(spec["url"])
        repo_name = _normalize_repo_name(raw_repo_name)
        dest_name = _oss_fuzz_src_dest_name(spec["dest"], raw_repo_name)
        if _normalize_repo_name(dest_name) == normalized_project or repo_name == normalized_project:
            results.append({**spec, "staged": False, "skipped": "benchmark source is _work/src"})
            continue
        target = staged_root / dest_name
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--filter=blob:none", spec["url"], str(target)]
        if not reference_time:
            cmd.insert(2, "--depth")
            cmd.insert(3, "1")
        cloned = _sh(cmd, timeout=1800)
        item = {**spec, "staged": cloned.returncode == 0, "path": str(target)}
        if cloned.returncode == 0:
            if reference_time:
                checkout = _checkout_repo_before(target, reference_time)
                item.update(checkout)
            rev = _sh(["git", "-C", str(target), "rev-parse", "--short", "HEAD"], timeout=30)
            item["commit"] = rev.stdout.strip() if rev.returncode == 0 else ""
        else:
            item["stderr"] = cloned.stderr[-2000:]
            shutil.rmtree(target, ignore_errors=True)
        results.append(item)
    if not any(item.get("staged") for item in results):
        shutil.rmtree(staged_root, ignore_errors=True)
    return results


def _checkout_repo_before(repo: Path, reference_time: str) -> dict[str, Any]:
    """Check a helper repo out to the latest commit before the sample commit time."""
    status: dict[str, Any] = {"reference_time": reference_time}
    rev = _sh(
        ["git", "-C", str(repo), "rev-list", "-1", f"--before={reference_time}", "HEAD"],
        timeout=120,
    )
    target = rev.stdout.strip()
    if rev.returncode != 0 or not target:
        status["checkout_before"] = False
        status["checkout_before_error"] = (rev.stderr or "no commit before reference time")[-1000:]
        return status
    checked = _sh(["git", "-C", str(repo), "checkout", "-q", target], timeout=300)
    status["checkout_before"] = checked.returncode == 0
    status["checkout_before_commit"] = target[:12]
    if checked.returncode != 0:
        status["checkout_before_error"] = checked.stderr[-1000:]
    return status


def _oss_fuzz_dockerfile_clone_specs(dockerfile: Path) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for line in _dockerfile_logical_lines(dockerfile):
        if not line.upper().startswith("RUN ") or "git clone" not in line:
            continue
        command = line[4:].strip()
        for part in re.split(r"\s+(?:&&|;)\s+", command):
            if "git clone" not in part:
                continue
            try:
                tokens = shlex.split(part)
            except ValueError:
                continue
            if len(tokens) < 3 or tokens[0:2] != ["git", "clone"]:
                continue
            url_index = next(
                (i for i, token in enumerate(tokens[2:], start=2)
                 if token.startswith("http://") or token.startswith("https://")),
                -1,
            )
            if url_index < 0:
                continue
            url = tokens[url_index]
            dest = ""
            if url_index + 1 < len(tokens) and not tokens[url_index + 1].startswith("-"):
                dest = tokens[url_index + 1]
            specs.append({"url": url, "dest": dest, "dockerfile": str(dockerfile)})
    return specs


def _dockerfile_logical_lines(dockerfile: Path) -> list[str]:
    try:
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    logical_lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if pending:
            pending += " " + line
        else:
            pending = line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical_lines.append(pending)
        pending = ""
    if pending:
        logical_lines.append(pending)
    return logical_lines


def _oss_fuzz_dockerfile_workdir(dockerfile: Path) -> str:
    """Return the final WORKDIR from an OSS-Fuzz Dockerfile, if present."""
    workdir = ""
    if not dockerfile.is_file():
        return workdir
    for line in _dockerfile_logical_lines(dockerfile):
        if not line.upper().startswith("WORKDIR "):
            continue
        raw = line.split(None, 1)[1].strip()
        try:
            tokens = shlex.split(raw)
            if tokens:
                raw = tokens[0]
        except ValueError:
            pass
        workdir = raw
    return workdir


def _oss_fuzz_runtime_workdir(d: Path, fallback: str = "$SRC") -> str:
    """Translate the staged Dockerfile WORKDIR into the runtime /gt/_work layout."""
    dockerfile = d / "oss_fuzz_project" / "Dockerfile"
    workdir = _oss_fuzz_dockerfile_workdir(dockerfile) or fallback
    replacements = {
        "${SRC}": "/gt/_work",
        "$SRC": "/gt/_work",
        "/src": "/gt/_work",
        "${OUT}": "/gt/_out",
        "$OUT": "/gt/_out",
        "/out": "/gt/_out",
        "${WORK}": "/gt/_work",
        "$WORK": "/gt/_work",
        "/work": "/gt/_work",
    }
    for old, new in replacements.items():
        if workdir == old:
            return new
        if workdir.startswith(old + "/"):
            return new + workdir[len(old):]
    if workdir and not workdir.startswith("/"):
        return "/gt/_work/" + workdir.strip("/")
    return workdir


def _setup_command_local_scripts(d: Path, project: str, command: str) -> list[Path]:
    """Return prepared project scripts invoked by a Dockerfile RUN command."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    scripts: list[Path] = []
    source_root = d / "_work" / "src"
    for token in tokens:
        cleaned = token.strip("'\"")
        if not cleaned or cleaned.startswith("-"):
            continue
        relative_candidates: list[str] = []
        if "/" in cleaned or cleaned.endswith(".sh"):
            relative_candidates.append(cleaned)
            for prefix in (f"{project}/", f"./{project}/"):
                if cleaned.startswith(prefix):
                    relative_candidates.append(cleaned[len(prefix):])
        for relative in relative_candidates:
            candidate = source_root / relative.lstrip("./")
            if candidate.is_file():
                scripts.append(candidate)
        for prefix in (
            f"${{SRC}}/{project}/",
            f"$SRC/{project}/",
            f"/src/{project}/",
            f"/gt/_work/{project}/",
        ):
            if cleaned.startswith(prefix):
                rel = cleaned[len(prefix):]
                candidate = source_root / rel
                if candidate.is_file():
                    scripts.append(candidate)
                break
    return scripts


def _setup_script_needs_root(path: Path) -> bool:
    """Conservatively detect official setup scripts that install system deps."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    low = text.lower()
    return (
        "apt-get " in low
        or re.search(r"(^|\s)apt\s+", low) is not None
        or "/usr/local" in low
        or re.search(r"(^|\s)make\s+install(\s|$)", low) is not None
        or re.search(r"(^|\s)(install|cp|mv|rm|ln|chmod|chown)\s+/(?!gt\b|tmp\b)", low) is not None
    )


def _translate_oss_fuzz_path(path: str) -> str:
    """Translate common OSS-Fuzz Docker paths into the staged /gt layout."""
    path = path.strip()
    replacements = {
        "${SRC}": "/gt/_work",
        "$SRC": "/gt/_work",
        "/src": "/gt/_work",
        "${OUT}": "/gt/_out",
        "$OUT": "/gt/_out",
        "/out": "/gt/_out",
        "${WORK}": "/gt/_work",
        "$WORK": "/gt/_work",
        "/work": "/gt/_work",
    }
    for old, new in replacements.items():
        if path == old:
            return new
        if path.startswith(old + "/"):
            return new + path[len(old):]
    if path and not path.startswith("/"):
        return "/gt/_work/" + path.strip("/")
    return path


def _dockerfile_env_exports(dockerfile: Path) -> list[str]:
    """Return shell exports for Dockerfile ENV instructions."""
    exports: list[str] = []
    if not dockerfile.is_file():
        return exports
    for line in _dockerfile_logical_lines(dockerfile):
        if not line.upper().startswith("ENV "):
            continue
        raw = line[4:].strip()
        try:
            tokens = shlex.split(raw)
        except ValueError:
            continue
        if not tokens:
            continue
        pairs: list[tuple[str, str]] = []
        if any("=" in token for token in tokens):
            for token in tokens:
                key, sep, value = token.partition("=")
                if sep and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                    pairs.append((key, value))
        elif len(tokens) >= 2 and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", tokens[0]):
            pairs.append((tokens[0], " ".join(tokens[1:])))
        for key, value in pairs:
            exports.append(f"export {key}={shlex.quote(value)}")
    return exports


def _download_oss_fuzz_dockerfile_add_urls(d: Path, project_dir: Path) -> list[dict[str, Any]]:
    """Download remote Dockerfile ADD artifacts into the per-sample result dir.

    Docker would fetch these URLs while building the official OSS-Fuzz builder
    image. Stage 01 runs in gt-memory-env instead, so the files must be staged
    explicitly from the Dockerfile URL for this sample.
    """
    dockerfile = project_dir / "Dockerfile"
    if not dockerfile.is_file():
        return []
    downloads_root = d / "oss_fuzz_downloads"
    results: list[dict[str, Any]] = []
    for line in _dockerfile_logical_lines(dockerfile):
        if not line.upper().startswith("ADD "):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if len(tokens) < 3:
            continue
        args = tokens[1:]
        if args[0].startswith("--"):
            continue
        dest = _translate_oss_fuzz_path(args[-1])
        for source in args[:-1]:
            if not source.startswith(("http://", "https://")):
                continue
            filename = Path(urllib.parse.urlparse(source).path).name
            if not filename:
                filename = hashlib.sha256(source.encode("utf-8")).hexdigest()
            target = downloads_root / filename
            item: dict[str, Any] = {
                "url": source,
                "dest": dest,
                "filename": filename,
                "path": str(target),
                "staged": False,
            }
            try:
                downloads_root.mkdir(parents=True, exist_ok=True)
                if not target.is_file() or target.stat().st_size == 0:
                    tmp = target.with_name(f".{target.name}.tmp")
                    with urllib.request.urlopen(source, timeout=600) as response:
                        with tmp.open("wb") as out:
                            shutil.copyfileobj(response, out)
                    tmp.replace(target)
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                item.update({
                    "staged": target.is_file() and target.stat().st_size > 0,
                    "bytes": target.stat().st_size,
                    "sha256": f"sha256:{digest}",
                })
            except Exception as exc:  # noqa: BLE001 - record per-sample fetch failure.
                item["error"] = str(exc)
                try:
                    target.unlink()
                except OSError:
                    pass
            results.append(item)
    if not any(item.get("staged") for item in results):
        shutil.rmtree(downloads_root, ignore_errors=True)
    return results


def _dockerfile_copy_commands(
    project_dir: Path, remote_adds: list[dict[str, Any]] | None = None
) -> list[str]:
    """Replay simple Dockerfile COPY/ADD instructions from staged project files."""
    dockerfile = project_dir / "Dockerfile"
    if not dockerfile.is_file():
        return []
    remote_by_url = {
        str(item.get("url")): str(item.get("filename"))
        for item in (remote_adds or [])
        if item.get("staged") and item.get("url") and item.get("filename")
    }
    commands: list[str] = []
    for line in _dockerfile_logical_lines(dockerfile):
        upper = line.upper()
        if not (upper.startswith("COPY ") or upper.startswith("ADD ")):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if len(tokens) < 3:
            continue
        args = tokens[1:]
        if args[0].startswith("--"):
            # Cross-stage COPY and other advanced Dockerfile forms are not
            # needed for the OSS-Fuzz project scripts we replay here.
            continue
        dest = _translate_oss_fuzz_path(args[-1])
        sources = args[:-1]
        dest_is_dir = len(sources) > 1 or dest.endswith("/")
        for source in sources:
            if source.startswith("--"):
                continue
            if source.startswith(("http://", "https://")):
                remote_name = remote_by_url.get(source)
                if not remote_name:
                    continue
                source_path = f"/gt/oss_fuzz_downloads/{remote_name}"
            else:
                source = source.lstrip("/")
                source_path = f"/gt/oss_fuzz_project/{source}"
            quoted_source = shlex.quote(source_path)
            has_glob = any(ch in source_path for ch in "*?[")
            if dest_is_dir:
                quoted_dest = shlex.quote(dest.rstrip("/") or dest)
                if has_glob:
                    commands.append(
                        f"shopt -s nullglob; _gt_copy_matches=({source_path}); "
                        f"if (( ${{#_gt_copy_matches[@]}} )); then "
                        f"mkdir -p {quoted_dest}; cp -a \"${{_gt_copy_matches[@]}}\" {quoted_dest}/; fi; "
                        "unset _gt_copy_matches"
                    )
                else:
                    commands.append(
                        f"if [[ -e {quoted_source} ]]; then "
                        f"mkdir -p {quoted_dest}; cp -a {quoted_source} {quoted_dest}/; fi"
                    )
            else:
                quoted_dest = shlex.quote(dest)
                quoted_parent = shlex.quote(str(Path(dest).parent))
                if has_glob:
                    commands.append(
                        f"shopt -s nullglob; _gt_copy_matches=({source_path}); "
                        f"if (( ${{#_gt_copy_matches[@]}} == 1 )); then "
                        f"mkdir -p {quoted_parent}; cp -a \"${{_gt_copy_matches[0]}}\" {quoted_dest}; fi; "
                        "unset _gt_copy_matches"
                    )
                else:
                    commands.append(
                        f"if [[ -e {quoted_source} ]]; then "
                        f"mkdir -p {quoted_parent}; cp -a {quoted_source} {quoted_dest}; fi"
                    )
    return commands


def _stage_oss_fuzz_dockerfile_setup_script(
    d: Path, project_dir: Path, project: str
) -> dict[str, Any]:
    """Write a runnable subset of the official Dockerfile dependency setup.

    Stage 01 runs inside the shared gt-memory-env image rather than building the
    exact OSS-Fuzz Docker image. Still, several projects keep required setup in
    Dockerfile RUN lines: curl runs curl-fuzzer's dependency script, clamav
    builds /mussels, and POCO copies its upstream build script. Preserve those
    official commands as an explicit artifact instead of expecting the agent to
    rediscover them.
    """
    dockerfile = project_dir / "Dockerfile"
    status: dict[str, Any] = {"staged": False}
    if not dockerfile.is_file():
        status["reason"] = "no Dockerfile"
        return status

    dockerfile_env = _dockerfile_env_exports(dockerfile)
    remote_adds = _download_oss_fuzz_dockerfile_add_urls(d, project_dir)
    copy_commands = _dockerfile_copy_commands(project_dir, remote_adds)
    commands: list[str] = []
    for line in _dockerfile_logical_lines(dockerfile):
        upper = line.upper()
        if not upper.startswith("RUN "):
            continue
        command = line[4:].strip()
        if _skip_dockerfile_setup_command(command):
            continue
        commands.append(command)
    workdir = _oss_fuzz_runtime_workdir(d)
    command_scripts = [
        script
        for command in commands
        for script in _setup_command_local_scripts(d, project, command)
    ]
    needs_root = any(
        re.search(r"(^|\s)(mkdir|install|cp|mv|rm|ln|chmod|chown)\s+/(?!gt\b|tmp\b)", command)
        or "/mussels" in command
        for command in commands
    ) or any(_setup_script_needs_root(script) for script in command_scripts)
    dockerfile_text = dockerfile.read_text(encoding="utf-8", errors="replace")
    rust_base_image = "base-builder-rust" in dockerfile_text
    needs_rustup = (
        rust_base_image
        or project == "clamav"
        or any(re.search(r"(^|\s)rustup(\s|$)", command) for command in commands)
    )
    rustup_bootstrap = ""
    if needs_rustup:
        nightly_setup = ""
        if project == "clamav" and "RUSTUP_TOOLCHAIN" not in "\n".join(dockerfile_env):
            nightly_setup = (
                "rustup toolchain install nightly --profile minimal || rustup update nightly\n"
                "export RUSTUP_TOOLCHAIN=nightly-x86_64-unknown-linux-gnu\n"
            )
        rustup_bootstrap = (
            "\n"
            "# The official OSS-Fuzz base-builder-rust image already provides rustup.\n"
            "# gt-memory-env may only have distro cargo/rustc, so bootstrap rustup\n"
            "# into HOME before replaying official rustup commands.\n"
            "if ! command -v rustup >/dev/null 2>&1; then\n"
            "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | "
            "sh -s -- -y --profile minimal --default-toolchain stable\n"
            "  export PATH=\"${HOME:-/tmp}/.cargo/bin:$PATH\"\n"
            "fi\n"
            + nightly_setup
        )

    env_block = (
        "\n".join(dockerfile_env) + "\n\n"
        if dockerfile_env
        else "# No Dockerfile ENV instructions.\n\n"
    )
    copy_block = (
        "\n".join(copy_commands) + "\n\n"
        if copy_commands
        else "# No project-local COPY/ADD scripts.\n\n"
    )
    run_block = (
        "\n".join(commands) + "\n"
        if commands
        else "# No extra RUN setup commands; layout only.\n"
    )
    setup_parts = [
        "#!/usr/bin/env bash\n",
        "set -euo pipefail\n\n",
        'export SRC="${SRC:-/gt/_work}"\n',
        'export OUT="${OUT:-/gt/_out}"\n',
        'export WORK="${WORK:-/gt/_work}"\n',
        'export PATH="${HOME:-/tmp}/.cargo/bin:$PATH"\n',
        'export PIP_BREAK_SYSTEM_PACKAGES="${PIP_BREAK_SYSTEM_PACKAGES:-1}"\n',
        'mkdir -p "$SRC" "$OUT" "$WORK"\n\n',
        "# Recreate the official OSS-Fuzz /src layout from prepared local material.\n",
        f'if [[ ! -e "$SRC/{project}" && -d /gt/_work/src ]]; then\n',
        f'  ln -s /gt/_work/src "$SRC/{project}"\n',
        "fi\n",
        'if [[ -d /gt/oss_fuzz_src ]]; then\n',
        '  cp -a -n /gt/oss_fuzz_src/. "$SRC"/\n',
        "fi\n",
        f'mkdir -p {shlex.quote(workdir)} 2>/dev/null || true\n\n',
        "# Make official dependency scripts retry-safe in the persistent /gt/_work.\n",
        "_gt_real_git() { command git \"$@\"; }\n",
        "git() {\n",
        "  if [[ \"${1:-}\" == \"clone\" ]]; then\n",
        "    local _gt_url=\"\" _gt_dest=\"\" _gt_i\n",
        "    local _gt_args=(\"$@\")\n",
        "    for ((_gt_i=1; _gt_i<${#_gt_args[@]}; _gt_i++)); do\n",
        "      case \"${_gt_args[$_gt_i]}\" in\n",
        "        --branch|--depth|--filter|--origin|--reference|--template|--upload-pack|--config|--separate-git-dir|--jobs)\n",
        "          ((_gt_i++)) || true\n",
        "          ;;\n",
        "        http://*|https://*|git@*|*.git)\n",
        "          _gt_url=\"${_gt_args[$_gt_i]}\"\n",
        "          if (( _gt_i + 1 < ${#_gt_args[@]} )) && [[ \"${_gt_args[$((_gt_i+1))]}\" != -* ]]; then\n",
        "            _gt_dest=\"${_gt_args[$((_gt_i+1))]}\"\n",
        "          fi\n",
        "          ;;\n",
        "      esac\n",
        "    done\n",
        "    if [[ -z \"$_gt_dest\" && -n \"$_gt_url\" ]]; then\n",
        "      _gt_dest=\"${_gt_url##*/}\"\n",
        "      _gt_dest=\"${_gt_dest%.git}\"\n",
        "    fi\n",
        "    if [[ -n \"$_gt_dest\" ]]; then\n",
        "      case \"$_gt_dest\" in\n",
        f"        \"$SRC/{project}\"|\"$SRC/{project}/\"*) ;;\n",
        "        /gt/_work/*|\"$SRC\"/*|\"$WORK\"/*) rm -rf \"$_gt_dest\" ;;\n",
        "      esac\n",
        "    fi\n",
        "  fi\n",
        "  _gt_real_git \"$@\"\n",
        "}\n",
        "export -f git _gt_real_git\n\n",
        "# Dockerfile ENV instructions required by the official build.\n",
        env_block,
        "# Dockerfile COPY/ADD instructions for project-local helper scripts.\n",
        copy_block,
        rustup_bootstrap,
        f"# Official non-clone RUN commands from projects/{project}/Dockerfile.\n",
        run_block,
    ]
    setup_text = "".join(setup_parts)
    script = d / "oss_fuzz_setup.sh"
    script.write_text(setup_text, encoding="utf-8")
    script.chmod(0o755)
    status.update({
        "staged": True,
        "path": str(script),
        "commands": len(commands),
        "env_exports": len(dockerfile_env),
        "copy_commands": len(copy_commands),
        "remote_adds": remote_adds,
        "workdir": workdir,
        "needs_root": needs_root,
        "inspected_setup_scripts": [str(path) for path in command_scripts],
    })
    return status


def _skip_dockerfile_setup_command(command: str) -> bool:
    """True when prepare or the base image already covers a Dockerfile RUN line.

    The repositories themselves are staged separately under oss_fuzz_src, while
    non-clone commands such as dependency builds must be preserved. Package
    installs are deliberately not replayed from Stage 01; if a project needs a
    new system package, that belongs in the shared gt-memory-env image.
    """
    parts = [part.strip() for part in re.split(r"\s+(?:&&|;)\s+", command) if part.strip()]
    if not parts:
        return False
    low = command.lower()
    if "apt-get " in low or "apt " in low:
        return True
    if re.search(r"\bcp\s+.*oss-fuzz-build\.sh\b", command):
        return True
    for part in parts:
        try:
            tokens = shlex.split(part)
        except ValueError:
            return False
        if len(tokens) < 3 or tokens[0:2] != ["git", "clone"]:
            return False
    return True


def _oss_fuzz_src_dest_name(dest: str, repo_name: str) -> str:
    if dest:
        cleaned = dest.rstrip("/")
        for prefix in ("/src/", "$SRC/", "${SRC}/"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        if cleaned in ("/src", "$SRC", "${SRC}"):
            cleaned = repo_name
        name = Path(cleaned).name
        if name:
            return name
    return repo_name


def _repo_basename_from_url(url: str) -> str:
    name = Path(url.rstrip("/")).name
    if name.endswith(".git"):
        name = name[:-4]
    return name


def _normalize_repo_name(name: str) -> str:
    name = name.strip().lower()
    if name.endswith(".git"):
        name = name[:-4]
    return re.sub(r"[^a-z0-9]+", "", name)


# A crash type implies which sanitizer produced it, and therefore which build
# flags reproduce it. Projects are usually fuzzed under several, so the crash
# record is what narrows it down. Ordered: first match wins.
_CRASH_TYPE_SANITIZER = (
    ("use-of-uninitialized-value", "MemorySanitizer", "-fsanitize=memory"),
    ("uninitialized", "MemorySanitizer", "-fsanitize=memory"),
    ("direct-leak", "LeakSanitizer", "-fsanitize=address"),
    ("indirect-leak", "LeakSanitizer", "-fsanitize=address"),
    ("memory leak", "LeakSanitizer", "-fsanitize=address"),
    ("data race", "ThreadSanitizer", "-fsanitize=thread"),
    ("undefined", "UndefinedBehaviorSanitizer", "-fsanitize=undefined"),
    ("integer-overflow", "UndefinedBehaviorSanitizer", "-fsanitize=undefined"),
    ("shift", "UndefinedBehaviorSanitizer", "-fsanitize=undefined"),
    ("divide-by-zero", "UndefinedBehaviorSanitizer", "-fsanitize=undefined"),
    ("misaligned", "UndefinedBehaviorSanitizer", "-fsanitize=undefined"),
    ("index out of bounds", "UndefinedBehaviorSanitizer", "-fsanitize=undefined"),
    ("overflow", "AddressSanitizer", "-fsanitize=address"),
    ("use-after-free", "AddressSanitizer", "-fsanitize=address"),
    ("use-after-poison", "AddressSanitizer", "-fsanitize=address"),
    ("double-free", "AddressSanitizer", "-fsanitize=address"),
    ("bad-free", "AddressSanitizer", "-fsanitize=address"),
    ("alloc-dealloc-mismatch", "AddressSanitizer", "-fsanitize=address"),
    ("negative-size-param", "AddressSanitizer", "-fsanitize=address"),
    ("segv", "AddressSanitizer", "-fsanitize=address"),
    ("null-dereference", "AddressSanitizer", "-fsanitize=address"),
)


def _crash_record(sid: str) -> str:
    """The benchmark's recorded crash type/state for this sample, if any."""
    root = Path(__file__).resolve().parents[2] / "dataset" / "crash_traces"
    for candidate in sorted(root.glob(f"*/{sid}.txt")):
        try:
            return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return ""


def _checkout_harnesses(src: Path, limit: int = 40) -> list[str]:
    """Files in the checkout that define a libFuzzer entry point.

    build.sh often builds its targets through a loop or a helper function, so
    the binary name is not textually present; the harness source always is.
    """
    if not src.is_dir():
        return []
    found = _sh(
        ["git", "-C", str(src), "grep", "-l", "-I", "--", "LLVMFuzzerTestOneInput"],
        timeout=180,
    )
    if found.returncode == 0 and found.stdout.strip():
        return sorted(found.stdout.split())[:limit]
    # Untracked or non-git trees: fall back to walking the source extensions.
    hits = []
    for ext in ("*.c", "*.cc", "*.cpp", "*.cxx"):
        for f in src.rglob(ext):
            try:
                if "LLVMFuzzerTestOneInput" in f.read_text(encoding="utf-8", errors="replace"):
                    hits.append(str(f.relative_to(src)))
            except OSError:
                continue
            if len(hits) >= limit:
                return sorted(hits)
    return sorted(hits)


def _synthesize_ossfuzz_bug_report(sample: dict[str, Any], d: Path, sid: str) -> bool:
    """Write the bug_report.md an OSS-Fuzz-derived sample never shipped.

    Returns False and writes nothing when the sample carries no OSS-Fuzz
    identity and no crash record, so samples from other benchmarks are
    unaffected.
    """
    crash = _crash_record(sid)
    project = str(sample.get("project") or "").strip()
    target = str(sample.get("oss_fuzz_target") or "").strip()
    engine = str(sample.get("oss_fuzz_engine") or "").strip()
    job = str(sample.get("oss_fuzz_job") or "").strip()
    declared_sanitizer = str(sample.get("oss_fuzz_sanitizer") or "").strip()
    testcase = str(sample.get("testcase_filename") or "").strip()

    cfg_path = (
        Path(__file__).resolve().parents[2]
        / "dataset" / "ossfuzz_project_config" / f"{project}.json"
    )
    cfg: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    if cfg.get("build_sh"):
        _stage_ossfuzz_build_recipe(d, cfg)

    if not crash and not target and not cfg:
        return False

    crash_type = ""
    match = re.search(r"^Crash type:\s*(.+)$", crash, re.M)
    if match:
        crash_type = match.group(1).strip()
    issue_url = ""
    match = re.search(r"https?://\S*oss-fuzz\S*", crash)
    if match:
        issue_url = match.group(0).rstrip(".,)")

    # The sample's own sanitizer field wins; fall back to inferring it from the
    # crash type, which is what the detector implies.
    sanitizer_note = ""
    if declared_sanitizer:
        flag = ""
        low = declared_sanitizer.lower()
        for key, build_flag in (("address", "-fsanitize=address"),
                                ("memory", "-fsanitize=memory"),
                                ("undefined", "-fsanitize=undefined"),
                                ("thread", "-fsanitize=thread")):
            if key in low:
                flag = build_flag
                break
        sanitizer_note = f"{declared_sanitizer}" + (f" -- build with {flag}" if flag else "")
    else:
        low = crash_type.lower()
        for needle, name, build_flag in _CRASH_TYPE_SANITIZER:
            if needle in low:
                sanitizer_note = (
                    f"not stated for this sample; the crash type ({crash_type}) is "
                    f"reported by {name} -- build with {build_flag}"
                )
                break

    lines = [
        "================= Bug Report (1/1) ==================",
        "## Source: OSS-Fuzz",
        "## Assembled from: this sample's OSS-Fuzz record, its crash record, the",
        "## project's configuration in google/oss-fuzz, and the harness sources",
        "## present in this checkout. Not a verbatim upstream report; the fields",
        "## under \'Reproduction target\' and \'Crash record\' are the sample\'s own",
        "## and are authoritative.",
    ]
    if issue_url:
        lines.append(f"## URL: {issue_url}")
    if project:
        name = cfg.get("oss_fuzz_project") or project
        suffix = "" if name == project else f"  (oss-fuzz project: {name})"
        lines.append(f"## Project: {project}{suffix}")
    lines.append("")

    lines.append("## Reproduction target")
    if target or engine or job or declared_sanitizer:
        if engine:
            lines.append(f"Fuzzing Engine: {engine}")
        if target:
            lines.append(f"Fuzz Target: {target}")
        if job:
            lines.append(f"Job Type: {job}")
        if sanitizer_note:
            lines.append(f"Sanitizer: {sanitizer_note}")
        if testcase:
            lines.append(f"ClusterFuzz testcase: {testcase}")
        lines.append("")
        lines.append(
            f"Build {target or 'the fuzz target'} and run it as "
            f"`{target or '<fuzz_target>'} <poc>`. The PoC is a libFuzzer "
            "testcase; feeding it to a command line tool reproduces nothing."
        )
    else:
        lines.append(
            "This sample does not record a fuzz target. Choose one using the "
            "crash state below and the project configuration further down, and "
            "state which you chose."
        )
        if sanitizer_note:
            lines.append(f"Sanitizer: {sanitizer_note}")
    lines.append("")

    lines.append("## Crash record")
    lines.append(crash.strip() if crash.strip() else "(none recorded)")
    lines.append("")

    # Locate the named target's source; its absence is the useful signal.
    harnesses = _checkout_harnesses(d / "_work" / "src")
    lines.append("## Harness sources in this checkout")
    if harnesses:
        matched = [h for h in harnesses if target and Path(h).stem == target]
        if matched:
            lines.append(f"{target} is defined here:")
            lines += [f"  - {x}" for x in matched]
            others = [h for h in harnesses if h not in matched]
            if others:
                lines.append("")
                lines.append("Other targets in this repository:")
                lines += [f"  - {x}" for x in others]
        else:
            if target:
                lines.append(
                    f"No file in this checkout defines {target}. Its harness most "
                    "likely lives in the google/oss-fuzz project directory "
                    "(see the build.sh below), not in this repository."
                )
                lines.append("")
            lines.append("Files here that define LLVMFuzzerTestOneInput:")
            lines += [f"  - {x}" for x in harnesses]
    else:
        lines.append(
            "None found. The harnesses are supplied by the google/oss-fuzz "
            "project directory; the build.sh below shows how they are compiled."
        )
    lines.append("")

    declared = cfg.get("sanitizers") or []
    binaries = cfg.get("fuzz_target_binaries") or []
    if declared or binaries:
        lines.append("## Project configuration (google/oss-fuzz)")
        if declared:
            lines.append(f"Sanitizers this project is fuzzed under: {', '.join(declared)}")
        if binaries:
            lines.append("Targets build.sh installs into $OUT: " + ", ".join(binaries))
        if cfg.get("language"):
            lines.append(f"Language: {cfg['language']}")
        lines.append("")
    if cfg.get("build_sh"):
        lines.append("## How OSS-Fuzz builds this project (projects/"
                     f"{cfg.get('oss_fuzz_project', project)}/build.sh)")
        lines.append("")
        lines.append("```bash")
        lines.append(cfg["build_sh"].rstrip())
        lines.append("```")
        lines.append("")
    staged_project = d / "oss_fuzz_project"
    if staged_project.is_dir():
        lines.append("## Official OSS-Fuzz project context staged by prepare")
        lines.append("")
        staged_files = sorted(
            str(path.relative_to(staged_project))
            for path in staged_project.rglob("*")
            if path.is_file()
        )
        if staged_files:
            lines.append("Files staged from google/oss-fuzz:")
            lines += [f"  - {path}" for path in staged_files[:40]]
            if len(staged_files) > 40:
                lines.append(f"  - ... {len(staged_files) - 40} more")
            lines.append("")
        dockerfile = staged_project / "Dockerfile"
        if dockerfile.is_file():
            lines.append("Dockerfile:")
            lines.append("")
            lines.append("```Dockerfile")
            lines.append(dockerfile.read_text(encoding="utf-8", errors="replace").rstrip())
            lines.append("```")
            lines.append("")
        setup_script = d / "oss_fuzz_setup.sh"
        if setup_script.is_file():
            lines.append("Dockerfile dependency/setup commands staged by prepare:")
            lines.append("  - /gt/oss_fuzz_setup.sh")
            lines.append("")
            lines.append(
                "Run this setup script before /gt/oss_fuzz_build.sh when the "
                "Dockerfile installs project dependencies, builds helper "
                "prefixes such as /mussels, or runs companion-repository "
                "dependency scripts."
            )
            lines.append("")
        staged_helpers = d / "oss_fuzz_src"
        if staged_helpers.is_dir():
            helper_dirs = sorted(
                path.name for path in staged_helpers.iterdir() if path.is_dir()
            )
            if helper_dirs:
                lines.append("Helper repositories staged from Dockerfile git clones:")
                lines += [f"  - /gt/oss_fuzz_src/{name}" for name in helper_dirs]
                lines.append("")
    if cfg.get("source"):
        lines.append(f"## Project configuration: {cfg['source']}")
        lines.append("")

    (d / "bug_report.md").write_text("\n".join(lines), encoding="utf-8")
    return True


def _write_oss_fuzz_build_wrapper(d: Path, body: str, workdir: str) -> bool:
    body = body.rstrip()
    if not body:
        return False
    recipe = d / "oss_fuzz_build.sh"
    recipe.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'export SRC="${SRC:-/gt/_work}"\n'
        'export OUT="${OUT:-/gt/_out}"\n'
        'export WORK="${WORK:-/gt/_work}"\n'
        f'cd "${{GT_OSS_FUZZ_WORKDIR:-{workdir}}}"\n'
        "\n"
        + body
        + "\n",
        encoding="utf-8",
    )
    recipe.chmod(0o755)
    return True


def _stage_ossfuzz_build_recipe(d: Path, cfg: dict[str, Any]) -> bool:
    build_sh = str(cfg.get("build_sh") or "").rstrip()
    if not build_sh:
        return False
    return _write_oss_fuzz_build_wrapper(d, build_sh, _oss_fuzz_runtime_workdir(d))


def _stage_official_oss_fuzz_build_recipe(d: Path) -> bool:
    """Stage projects/<project>/build.sh from official google/oss-fuzz."""
    candidate = d / "oss_fuzz_project" / "build.sh"
    if not candidate.is_file():
        return False
    return _write_oss_fuzz_build_wrapper(
        d,
        "# Staged from official google/oss-fuzz projects/<project>/build.sh\n"
        + candidate.read_text(encoding="utf-8", errors="replace"),
        _oss_fuzz_runtime_workdir(d),
    )


def _stage_upstream_ossfuzz_build_recipe(d: Path) -> bool:
    """Stage a project-local OSS-Fuzz build script from the vulnerable checkout."""
    src = d / "_work" / "src"
    for rel in (
        "build/script/oss-fuzz-build.sh",
        "build/oss-fuzz-build.sh",
        "fuzzer/ossfuzz.sh",
        "tests/ossfuzz.sh",
    ):
        candidate = src / rel
        if candidate.is_file():
            return _write_oss_fuzz_build_wrapper(
                d,
                candidate.read_text(encoding="utf-8", errors="replace"),
                _oss_fuzz_runtime_workdir(d),
            )
    for rel in (
        "build/script/oss-fuzz-build.sh",
        "build/oss-fuzz-build.sh",
        "fuzzer/ossfuzz.sh",
        "tests/ossfuzz.sh",
    ):
        body, source = _read_upstream_repo_file(src, rel)
        if body:
            return _write_oss_fuzz_build_wrapper(
                d,
                f"# Staged from upstream project repository: {source}\n" + body,
                _oss_fuzz_runtime_workdir(d),
            )
    return False


def _read_upstream_repo_file(repo: Path, rel: str) -> tuple[str, str]:
    """Read a harness/build helper from the same upstream repo when the
    vulnerable checkout predates it.

    This is for OSS-Fuzz build material only. The vulnerable and fixed source
    used for GT semantics remains the checked-out benchmark commit.
    """
    if not (repo / ".git").exists():
        return "", ""
    for rev in ("origin/HEAD", "origin/main", "origin/master", "HEAD"):
        shown = _sh(["git", "-C", str(repo), "show", f"{rev}:{rel}"], timeout=60)
        if shown.returncode == 0 and shown.stdout.strip():
            return shown.stdout, f"{rev}:{rel}"
    return "", ""


def _stage_reproduction_config(
    sample: dict[str, Any], d: Path, sid: str, reference_time: str = ""
) -> dict[str, Any]:
    """Copy the benchmark's own reproduction material next to the PoC.

    SEC-bench records the fuzzing engine, fuzz target, job type and sanitizer in
    bug_report.md. Without it Stage 01 has to guess the entry point from the
    crash trace, and a libFuzzer testcase fed to a command line tool reproduces
    nothing.
    """
    staged: dict[str, Any] = {
        "bug_report": False,
        "harness_downloads": 0,
        "oss_fuzz_build_recipe": False,
    }
    project = str(sample.get("project") or sample.get("oss_fuzz_project") or "").strip()
    if project:
        staged["oss_fuzz_project"] = _stage_oss_fuzz_project_context(
            d, project, reference_time
        )
    pocdir = _poc_source_dir(sample, sid)
    if pocdir is None or not pocdir.is_dir():
        if _synthesize_ossfuzz_bug_report(sample, d, sid):
            staged["bug_report"] = True
            staged["bug_report_assembled"] = True
        return staged

    report = pocdir / "bug_report.md"
    if report.is_file():
        shutil.copy(report, d / "bug_report.md")
        staged["bug_report"] = True

    cfg_path = (
        Path(__file__).resolve().parents[2]
        / "dataset" / "ossfuzz_project_config" / f"{project}.json"
    )
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
        if cfg:
            staged["oss_fuzz_build_recipe"] = _stage_ossfuzz_build_recipe(d, cfg)
            if not staged["oss_fuzz_build_recipe"]:
                staged["oss_fuzz_build_recipe"] = _stage_official_oss_fuzz_build_recipe(d)
            if not staged["oss_fuzz_build_recipe"]:
                staged["oss_fuzz_build_recipe"] = _stage_upstream_ossfuzz_build_recipe(d)

    downloads = pocdir / "downloads"
    if downloads.is_dir():
        target = d / "harness_downloads"
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(downloads, target)
        staged["harness_downloads"] = sum(1 for _ in target.rglob("*") if _.is_file())

    if not staged["bug_report"] and _synthesize_ossfuzz_bug_report(sample, d, sid):
        staged["bug_report"] = True
        staged["bug_report_assembled"] = True
    if not staged["oss_fuzz_build_recipe"]:
        staged["oss_fuzz_build_recipe"] = _stage_official_oss_fuzz_build_recipe(d)
    if not staged["oss_fuzz_build_recipe"]:
        staged["oss_fuzz_build_recipe"] = _stage_upstream_ossfuzz_build_recipe(d)
    return staged


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
    repo_root = Path(__file__).resolve().parents[2]
    src = d / "_work" / "src"
    fcommit = sample.get("fix_commit")
    cloned_from, clone_errors, repo_cache = _materialize_repo_checkout(
        str(repo), src, [str(vcommit or ""), str(fcommit or "")]
    )
    if not cloned_from:
        return {
            "prepared": False,
            "track": "repo",
            "reason": f"clone failed: {repo}",
            "env": env_ok,
            "clone_errors": clone_errors,
        }
    if vcommit:
        _sh(["git", "-C", str(src), "checkout", "-q", str(vcommit)])
    source_ready = src.exists() and any(
        path.name != ".git" for path in src.iterdir()
    )
    if not source_ready:
        return {
            "prepared": False,
            "track": "repo",
            "reason": "source checkout is empty after vulnerable commit checkout",
            "env": env_ok,
            "repo": repo,
            "clone_repo": cloned_from,
            "vulnerable_commit": vcommit,
            "repo_cache": repo_cache,
            "clone_errors": clone_errors,
        }
    reference_time = _git_commit_time(src, str(vcommit)) if vcommit else ""
    # patch = diff between vulnerable and fix commit (deterministic)
    if vcommit and fcommit:
        diff = _sh(["git", "-C", str(src), "diff", str(vcommit), str(fcommit)])
        if diff.stdout.strip():
            (d / "patch.diff").write_text(diff.stdout)
    if not (d / "patch.diff").exists():
        _stage_patch(sample, d, sid)
    staged_poc = _stage_repo_poc(sample, d, sid)
    repro_config = _stage_reproduction_config(sample, d, sid, reference_time)
    (d / "build.sh").write_text(repo_track_build_sh(env_image))
    (d / "build.sh").chmod(0o755)
    _init_state(sid, d)
    report = {"track": "repo/secbench", "sample_id": sid, "env": env_image,
              "env_context": str(env_context), "env_ok": env_ok,
              "repo": repo, "clone_repo": cloned_from, "vulnerable_commit": vcommit, "source": src.exists(),
              "source_ready": source_ready,
              "repo_cache": repo_cache,
              "poc": (d / "poc").exists(), "poc_source": staged_poc,
              "bug_report": repro_config["bug_report"],
              "harness_downloads": repro_config["harness_downloads"],
              "oss_fuzz_project": repro_config.get("oss_fuzz_project"),
              "oss_fuzz_build_recipe": repro_config.get("oss_fuzz_build_recipe", False),
              "patch": (d / "patch.diff").exists(),
              "prepared": bool(src.exists())}
    for key in (
        "oss_fuzz_engine",
        "oss_fuzz_target",
        "oss_fuzz_job",
        "oss_fuzz_sanitizer",
        "oss_fuzz_platform",
        "oss_fuzz_fixed_range",
        "oss_fuzz_introduced_range",
        "oss_fuzz_last_known_bad_commit",
        "oss_fuzz_first_known_good_commit",
        "vulnerable_commit_source",
    ):
        if sample.get(key):
            report[key] = sample[key]
    return report


def _git_commit_time(repo: Path, commit: str) -> str:
    if not commit:
        return ""
    shown = _sh(
        ["git", "-C", str(repo), "show", "-s", "--format=%cI", commit],
        timeout=60,
    )
    return shown.stdout.strip() if shown.returncode == 0 else ""


def _stage_repo_poc(sample: dict[str, Any], d: Path, sid: str) -> str:
    """Stage the actual runnable testcase as /gt/poc for repo-based samples.

    Older SEC-bench imports stored the public issue/report text at
    dataset/pocs/<sid>/poc. If prepare blindly picks the largest file in that
    directory, a report or downloaded archive can be staged instead of the
    testcase. Prefer explicit sample metadata and the testcase/ subdirectory;
    only then fall back to legacy flat files while filtering obvious metadata.
    """
    destination = d / "poc"
    if destination.is_file() or destination.is_symlink():
        destination.unlink()
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
        import contextlib
        from . import state as _state

        with contextlib.redirect_stdout(io.StringIO()):
            _state.main([
                "init",
                "--sample-id",
                sample_id,
                "--output",
                str(d / "sample_state.json"),
            ])
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


def hydrate_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gt-toolkit hydrate-runtime",
        description="Restore _work/src and repo-track runtime scaffold for a compact GT package.",
    )
    ap.add_argument("--result-dir", required=True)
    ap.add_argument("--force", action="store_true")
    ns = ap.parse_args(argv)
    res = hydrate_runtime(ns.result_dir, force=ns.force)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("prepared") else 1


if __name__ == "__main__":
    raise SystemExit(main())
