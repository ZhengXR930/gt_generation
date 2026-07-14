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


def prepare(sample_path: str, result_dir: str) -> dict[str, Any]:
    sample = json.loads(Path(sample_path).read_text())
    aid = _arvo_id(sample)
    if not aid:
        return {"prepared": False, "reason": "no arvo/benchmark id in sample"}
    d = Path(result_dir)
    (d / "_work").mkdir(parents=True, exist_ok=True)
    vul, fix = f"n132/arvo:{aid}-vul", f"n132/arvo:{aid}-fix"

    if not _pull(vul):
        return {"prepared": False, "reason": f"pull failed: {vul}"}
    fix_ok = _pull(fix)                                   # best-effort; 04 differential needs it

    cid = _sh(["docker", "create", vul]).stdout.strip()
    src = d / "_work" / "src"
    shutil.rmtree(src, ignore_errors=True)
    if cid:
        _sh(["docker", "cp", f"{cid}:/src", str(src)])
        _sh(["docker", "cp", f"{cid}:/tmp/poc", str(d / "poc")])
        _sh(["docker", "rm", cid])

    # NOTE: prepare does NOT reproduce/build — reproduction (which may require compiling
    # or fixing a not-pre-built target) is the job of the AGENT stage 01_reproducer, which
    # runs against these already-pulled local images. prepare records the reproduce command
    # for 01 to use on the pre-built ARVO images.
    (d / "build.sh").write_text(
        f"docker run --rm --entrypoint /bin/bash {vul} -c '/bin/arvo run'\n")

    for p in (sample.get("patch_url_or_path"), sample.get("patch_diff_path"),
              f"arvo_patches/{aid}.diff", f"final_dataset/arvo_patches/{aid}.diff"):
        if p and Path(str(p)).exists():
            shutil.copy(str(p), d / "patch.diff")
            break
    try:
        from . import state as _state
        _state.main(["init", "--sample-id", f"arvo_{aid}", "--output", str(d / "sample_state.json")])
    except Exception:
        pass

    report = {"arvo_id": aid, "vul_image": vul, "fix_image_name": fix, "fix_image_pulled": fix_ok,
              "source": src.exists(), "poc": (d / "poc").exists(), "patch": (d / "patch.diff").exists(),
              "prepared": bool(src.exists())}
    (d / "prepare_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


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
