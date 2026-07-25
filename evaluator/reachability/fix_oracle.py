#!/usr/bin/env python3
"""Behavioral fix oracle: does the crash disappear in the real fixed build?

For an ARVO sample we do NOT trust patch.diff -- the official fix commit is frequently an
unrelated build/docs/version-bump commit that does not touch the vulnerable code. The
prebuilt n132/arvo:<id>-fix image, by ARVO's construction, is the build where the crash
no longer reproduces, so it is the authoritative "the fix removes the crash" oracle. This
runs the sample's poc against both images:

    n132/arvo:<id>-vul  -> expect CRASH  (sanity: the bug reproduces)
    n132/arvo:<id>-fix  -> expect CLEAN  (the crash is genuinely fixed)

A sample where -vul does not crash, or -fix still crashes, is flagged: its poc/GT no
longer matches the images and needs review. No debugger, no patch apply; works under qemu
emulation (amd64 image on Apple Silicon).

    PYTHONPATH=evaluator python3 -m reachability.fix_oracle --sample-id arvo_10123
    PYTHONPATH=evaluator python3 -m reachability.fix_oracle           # every ARVO sample
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"

_SANITIZER_RE = re.compile(
    r"(AddressSanitizer|MemorySanitizer|ERROR: libFuzzer|runtime error:|"
    r"use-of-uninitialized-value|heap-buffer-overflow|heap-use-after-free|"
    r"stack-buffer-overflow|global-buffer-overflow|SEGV|Segmentation fault)",
    re.IGNORECASE,
)


def _arvo_id(sample_id: str) -> str:
    return sample_id.removeprefix("arvo_")


def run_poc_in_image(image: str, poc: Path, timeout: int) -> dict[str, Any]:
    """Run the ARVO reproducer once with the poc; classify crash vs clean."""
    proc = subprocess.run(
        [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "-e", "LANG=C.UTF-8",
            "-v", f"{poc.resolve()}:/tmp/poc:ro",
            "--entrypoint", "/bin/bash", image, "-lc", "/bin/arvo run",
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    sanitizer = bool(_SANITIZER_RE.search(out))
    return {
        "image": image,
        "returncode": proc.returncode,
        "sanitizer": sanitizer,
        "crashed": proc.returncode != 0 or sanitizer,
    }


def verify_sample(sample_id: str, timeout: int) -> dict[str, Any]:
    aid = _arvo_id(sample_id)
    if sample_id == aid:  # no "arvo_" prefix -> repo/secbench track, no -fix image
        return {"sample_id": sample_id, "skipped": "non-ARVO track (no -fix image)"}
    poc = GT_RESULTS / sample_id / "poc"
    if not poc.is_file():
        return {"sample_id": sample_id, "skipped": "no poc"}
    vul, fix = f"n132/arvo:{aid}-vul", f"n132/arvo:{aid}-fix"
    try:
        vul_r = run_poc_in_image(vul, poc, timeout)
        fix_r = run_poc_in_image(fix, poc, timeout)
    except subprocess.TimeoutExpired:
        return {"sample_id": sample_id, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001 - report any docker/runtime failure
        return {"sample_id": sample_id, "error": f"{type(exc).__name__}: {exc}"}

    problems = []
    if not vul_r["crashed"]:
        problems.append("vul image did NOT crash (poc no longer reproduces the bug)")
    if fix_r["crashed"]:
        problems.append("fix image STILL crashes (the official fix does not remove it)")
    report = {
        "sample_id": sample_id,
        "vul": vul_r,
        "fix": fix_r,
        "oracle_ok": vul_r["crashed"] and not fix_r["crashed"],
        "problems": problems,
    }
    (GT_RESULTS / sample_id / "fix_verification.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample-id", action="append",
                    help="Verify only this sample (repeatable); default every ARVO poc.")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args(argv)

    samples = args.sample_id or sorted(
        p.name for p in GT_RESULTS.iterdir()
        if p.is_dir() and p.name.startswith("arvo_") and (p / "poc").is_file()
    )
    print(f"{'sample':16s} vul_crash fix_clean verdict")
    print("-" * 72)
    rows = []
    for s in samples:
        r = verify_sample(s, args.timeout)
        rows.append(r)
        if "skipped" in r:
            print(f"{s:16s} (skipped: {r['skipped']})")
        elif "error" in r:
            print(f"{s:16s} ERROR: {r['error']}")
        else:
            vc = "T" if r["vul"]["crashed"] else "F"
            fc = "T" if not r["fix"]["crashed"] else "F"
            verdict = "OK" if r["oracle_ok"] else "PROBLEM: " + "; ".join(r["problems"])
            print(f"{s:16s}    {vc}        {fc}      {verdict}")
    (GT_RESULTS / "fix_verification_report.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
