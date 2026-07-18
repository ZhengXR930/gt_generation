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
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

_CRASH_MARKERS = (
    "ERROR: AddressSanitizer", "ERROR: LeakSanitizer", "SUMMARY: ", "runtime error:",
    "MemorySanitizer", "use-of-uninitialized-value", "SEGV on unknown", "attempting double-free",
    "heap-use-after-free", "heap-buffer-overflow", "stack-buffer-overflow",
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
    return bool(sample.get("arvo_image_vul")
                or str(sample.get("source_dataset", "")).upper().startswith("ARVO"))


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
        shutil.copy2(source_sample_path, sample_info_path)
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
        return {"default_crash_trace_staged": False}
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "default_crash_trace_staged": True,
        "source": source,
        "sha256": f"sha256:{digest}",
    }


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


def _ensure_memory_env(tag: str = "gt-memory-env:latest") -> bool:
    if _sh(["docker", "images", "-q", tag]).stdout.strip():
        return True
    return _sh(["docker", "build", "-t", tag, "docker/gt-memory-env"], timeout=3000).returncode == 0


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
    env_ok = _ensure_memory_env()
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
    # poc from final_dataset/pocs/<sample_id>/
    pocdir = Path(f"final_dataset/pocs/{sid}")
    if pocdir.is_dir():
        files = [f for f in pocdir.iterdir() if f.is_file() and f.name != "patch.diff"]
        if files:
            shutil.copy(str(max(files, key=lambda f: f.stat().st_size)), d / "poc")
    (d / "build.sh").write_text(
        f"# secbench/repo sample {sid}: gt_generator builds this in gt-memory-env\n"
        f"# repo={repo} vulnerable_commit={vcommit}\n")
    _init_state(sid, d)
    return {"track": "repo/secbench", "sample_id": sid, "env": "gt-memory-env", "env_ok": env_ok,
            "repo": repo, "vulnerable_commit": vcommit, "source": src.exists(),
            "poc": (d / "poc").exists(), "patch": (d / "patch.diff").exists(),
            "prepared": bool(src.exists())}


def _stage_patch(sample: dict[str, Any], d: Path, sid: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    raw_candidates = (
        sample.get("patch_diff_path"),
        sample.get("patch_path"),
        f"arvo_patches/{sid}.diff",
        f"final_dataset/arvo_patches/{sid}.diff",
        f"final_dataset/arvo/{sid}/patch.diff",
        f"final_dataset/pocs/{sid}/patch.diff",
    )
    for raw in raw_candidates:
        if not raw:
            continue
        candidate = Path(str(raw))
        candidates = [candidate]
        if not candidate.is_absolute():
            candidates.extend((repo_root / candidate, repo_root / "final_dataset" / candidate))
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
