"""gt-toolkit prepare: deterministic per-sample material FETCH (NO LLM).

Pulls the ARVO images (vul + fix, with retries), extracts the source tree, and stages
poc/patch/sample_state. It does NOT reproduce or build — reproduction (which may require
compiling or fixing a target that is not pre-built) stays an AGENT stage (01_reproducer)
that runs against these already-pulled local images. This replaces only the deterministic
"materialize" work whose slow docker PULL, when entangled with the agent's turns, caused
the Claude API to drop mid-response (the dominant v1 failure). Moving the pull here — a
retryable script holding no API session — removes that failure mode.

    gt-toolkit prepare --sample sample.json --result-dir gt_results/arvo_<id>

Exit 0 when the source tree was extracted (writes prepare_report.json).
"""
from __future__ import annotations

import argparse
import json
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
      ARVO         -> pull n132/arvo:<id>-vul/fix (target pre-built in the image)
      repo/secbench -> ensure the gt-memory-env image + clone repo@vulnerable_commit
                       (target is built later by the AGENT stage 01 inside gt-memory-env)
    Either way the SLOW deterministic fetch happens here (no LLM); 01 only reproduces."""
    sample = json.loads(Path(sample_path).read_text())
    d = Path(result_dir)
    (d / "_work").mkdir(parents=True, exist_ok=True)
    report = _prepare_arvo(sample, d) if _is_arvo(sample) else _prepare_repo(sample, d)
    (d / "prepare_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _prepare_arvo(sample: dict[str, Any], d: Path) -> dict[str, Any]:
    aid = _arvo_id(sample)
    if not aid:
        return {"prepared": False, "reason": "no arvo/benchmark id in sample"}
    vul, fix = f"n132/arvo:{aid}-vul", f"n132/arvo:{aid}-fix"
    if not _pull(vul):
        return {"prepared": False, "track": "arvo", "reason": f"pull failed: {vul}"}
    fix_ok = _pull(fix)                                   # best-effort; 04 differential needs it
    cid = _sh(["docker", "create", vul]).stdout.strip()
    src = d / "_work" / "src"
    shutil.rmtree(src, ignore_errors=True)
    if cid:
        _sh(["docker", "cp", f"{cid}:/src", str(src)])
        _sh(["docker", "cp", f"{cid}:/tmp/poc", str(d / "poc")])
        _sh(["docker", "rm", cid])
    (d / "build.sh").write_text(
        f"docker run --rm --entrypoint /bin/bash {vul} -c '/bin/arvo run'\n")
    _stage_patch(sample, d, aid)
    _init_state(f"arvo_{aid}", d)
    return {"track": "arvo", "arvo_id": aid, "vul_image": vul, "fix_image_pulled": fix_ok,
            "source": src.exists(), "poc": (d / "poc").exists(),
            "patch": (d / "patch.diff").exists(), "prepared": bool(src.exists())}


def _ensure_memory_env(tag: str = "gt-memory-env:latest") -> bool:
    if _sh(["docker", "images", "-q", tag]).stdout.strip():
        return True
    return _sh(["docker", "build", "-t", tag, "docker/gt-memory-env"], timeout=3000).returncode == 0


def _prepare_repo(sample: dict[str, Any], d: Path) -> dict[str, Any]:
    """SEC-bench / OSS-Fuzz / repo-based: no pre-built image. Build the shared
    gt-memory-env, clone the repo at the vulnerable commit, stage poc + patch. The
    AGENT stage 01 then builds the target (install project deps, sanitizer, fix) inside
    gt-memory-env and reproduces — the adaptive per-project build is why 01 is an agent."""
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
        f"# secbench/repo sample {sid}: 01_reproducer builds this in gt-memory-env\n"
        f"# repo={repo} vulnerable_commit={vcommit}\n")
    _init_state(sid, d)
    return {"track": "repo/secbench", "sample_id": sid, "env": "gt-memory-env", "env_ok": env_ok,
            "repo": repo, "vulnerable_commit": vcommit, "source": src.exists(),
            "poc": (d / "poc").exists(), "patch": (d / "patch.diff").exists(),
            "prepared": bool(src.exists())}


def _stage_patch(sample: dict[str, Any], d: Path, sid: str) -> None:
    for p in (sample.get("patch_diff_path"), f"arvo_patches/{sid}.diff",
              f"final_dataset/arvo_patches/{sid}.diff", f"final_dataset/pocs/{sid}/patch.diff"):
        if p and Path(str(p)).exists():
            shutil.copy(str(p), d / "patch.diff")
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
