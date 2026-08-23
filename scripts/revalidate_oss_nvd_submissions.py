#!/usr/bin/env python3
"""Re-run saved OSS/NVD PoC submissions against the current runtime specs.

This repairs result directories produced before non-ARVO runtime specs were
available.  It does not invoke any model and does not touch SEC-bench samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GT_RESULTS = ROOT / "gt_results"
POC_RESULTS = ROOT / "poc_generation" / "poc_results"

sys.path.insert(0, str(ROOT / "poc_generation" / "poc_generator"))

from openhands_backend.run_local_sample import (  # noqa: E402
    load_runtime_spec,
    runtime_triggered,
)
from poc_dedup import deduplicate_submission_attempts  # noqa: E402
from evaluator.reachability.runtime_spec import (  # noqa: E402
    compile_runtime_spec,
    container_path_on_host,
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def is_oss_or_nvd(sample_id: str) -> bool:
    return sample_id.startswith("osv_") or sample_id.startswith("nvd_")


def valid_oss_nvd_samples() -> list[str]:
    valid = load_json(GT_RESULTS / "valid_gt.json")["samples"]
    return [sample for sample in valid if is_oss_or_nvd(sample)]


def revalidate_submission(
    *,
    sample_id: str,
    sample_result_dir: Path,
    submission_dir: Path,
    inner_command: str,
    detector: str,
    timeout: int,
) -> dict:
    attempt_id = submission_dir.name
    poc_path = submission_dir / "poc.bin"
    result_path = submission_dir / "result.json"
    runtime_output_path = submission_dir / "runtime_output.txt"
    existing = load_json(result_path) if result_path.is_file() else {}
    if not poc_path.is_file():
        result = dict(existing)
        result.update(
            {
                "attempt_id": attempt_id,
                "exit_code": None,
                "runtime_output_path": "runtime_output.txt",
                "analysis_path": (
                    f"submissions/{attempt_id}/analysis.json"
                    if (submission_dir / "analysis.json").is_file()
                    else None
                ),
                "poc_path": f"submissions/{attempt_id}/poc.bin",
                "validation": "invalid_submission_dir",
                "triggered": False,
                "vul_exit_code": None,
                "analysis_valid": (submission_dir / "analysis.json").is_file(),
                "error": "submitted PoC is missing",
            }
        )
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return result

    gt_sample_dir = GT_RESULTS / sample_id
    tmp_root = gt_sample_dir / ".poc_eval_tmp"
    staged_dir = tmp_root / f"revalidate-{sample_result_dir.parent.name}-{attempt_id}"
    if staged_dir.exists():
        shutil.rmtree(staged_dir)
    staged_dir.mkdir(parents=True)
    shutil.copy2(poc_path, staged_dir / "poc.bin")
    runtime_poc = f"/gt/.poc_eval_tmp/{staged_dir.name}/poc.bin"
    command = inner_command.replace("/gt/poc", runtime_poc)
    try:
        completed = subprocess.run(
            [str((gt_sample_dir / "build.sh").resolve()), command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = completed.stdout or ""
        exit_code = int(completed.returncode)
        validation = "host_validated"
        error = None
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        output = stdout + stderr + "\nvalidation timed out\n"
        exit_code = 124
        validation = "host_validated"
        error = "validation_timeout"
    finally:
        shutil.rmtree(staged_dir, ignore_errors=True)
        try:
            tmp_root.rmdir()
        except OSError:
            pass

    runtime_output_path.write_text(output, encoding="utf-8", errors="replace")
    poc_hash = hashlib.sha256(poc_path.read_bytes()).hexdigest()
    result = dict(existing)
    result.update(
        {
            "attempt_id": attempt_id,
            "exit_code": exit_code,
            "poc_sha256": poc_hash,
            "poc_length": poc_path.stat().st_size,
            "runtime_output_path": f"submissions/{attempt_id}/runtime_output.txt",
            "analysis_path": (
                f"submissions/{attempt_id}/analysis.json"
                if (submission_dir / "analysis.json").is_file()
                else None
            ),
            "poc_path": f"submissions/{attempt_id}/poc.bin",
            "validation": validation,
            "triggered": runtime_triggered(output, exit_code, detector),
            "poc_hash": poc_hash,
            "vul_exit_code": exit_code,
            "analysis_valid": (submission_dir / "analysis.json").is_file(),
        }
    )
    result.pop("runtime_unavailable", None)
    if error:
        result["error"] = error
    else:
        result.pop("error", None)
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


def update_manifest(sample_result_dir: Path, submissions: list[dict]) -> None:
    manifest_path = sample_result_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    manifest = load_json(manifest_path)
    for index, item in enumerate(submissions, 1):
        item["sequence_in_run"] = index
        item["result_path"] = f"submissions/{item['attempt_id']}/"
    stats, representatives = deduplicate_submission_attempts(submissions)
    manifest["num_submission_attempts"] = len(submissions)
    manifest["submission_attempts"] = submissions
    manifest["poc_generation"] = {
        "ok": bool(submissions),
        "submissions": submissions,
        "num_submission_attempts": len(submissions),
        "triggered_attempts": sum(1 for item in submissions if item.get("triggered") is True),
        "runtime_unavailable": False,
        "revalidated_from_saved_submissions": True,
    }
    manifest["poc_deduplication"] = stats
    manifest["deduplicated_pocs"] = representatives
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def revalidate_sample(model: str, sample_id: str, timeout: int) -> dict:
    return revalidate_sample_impl(
        model=model,
        sample_id=sample_id,
        timeout=timeout,
        all_submissions=False,
    )


def revalidate_sample_impl(
    *,
    model: str,
    sample_id: str,
    timeout: int,
    all_submissions: bool,
) -> dict:
    sample_result_dir = POC_RESULTS / model / sample_id
    submissions_root = sample_result_dir / "submissions"
    if not sample_result_dir.is_dir():
        return {"sample_id": sample_id, "status": "missing_result_dir"}
    if not submissions_root.is_dir():
        return {"sample_id": sample_id, "status": "no_submissions"}
    submission_dirs = sorted(path for path in submissions_root.iterdir() if path.is_dir())
    if not submission_dirs:
        return {"sample_id": sample_id, "status": "no_submissions"}

    inner_command, meta = load_runtime_spec(GT_RESULTS / sample_id)
    detector = str(meta.get("detector") or "")

    by_hash: dict[str, list[Path]] = {}
    for index, submission_dir in enumerate(submission_dirs, 1):
        poc_path = submission_dir / "poc.bin"
        if poc_path.is_file():
            key = hashlib.sha256(poc_path.read_bytes()).hexdigest()
        else:
            key = f"missing:{submission_dir.name}:{index}"
        by_hash.setdefault(key, []).append(submission_dir)
    representative_dirs = set(submission_dirs if all_submissions else [items[-1] for items in by_hash.values()])
    representative_results: dict[str, dict] = {}
    def poc_key(submission_dir: Path) -> str:
        poc_path = submission_dir / "poc.bin"
        return (
            hashlib.sha256(poc_path.read_bytes()).hexdigest()
            if poc_path.is_file()
            else f"missing:{submission_dir.name}"
        )

    for submission_dir in sorted(representative_dirs):
        key = poc_key(submission_dir)
        representative_results[key] = revalidate_submission(
            sample_id=sample_id,
            sample_result_dir=sample_result_dir,
            submission_dir=submission_dir,
            inner_command=inner_command,
            detector=detector,
            timeout=timeout,
        )

    submissions: list[dict] = []
    reused = 0
    for submission_dir in submission_dirs:
        poc_path = submission_dir / "poc.bin"
        key = poc_key(submission_dir)
        if submission_dir in representative_dirs:
            result = representative_results[key]
            submissions.append(result)
            continue

        representative = representative_results.get(key)
        if representative is None:
            # This can only happen when the latest representative sorts before a
            # duplicate with a non-monotonic directory name; validate directly.
            result = revalidate_submission(
                sample_id=sample_id,
                sample_result_dir=sample_result_dir,
                submission_dir=submission_dir,
                inner_command=inner_command,
                detector=detector,
                timeout=timeout,
            )
            submissions.append(result)
            continue
        reused += 1
        result_path = submission_dir / "result.json"
        runtime_output_path = submission_dir / "runtime_output.txt"
        existing = load_json(result_path) if result_path.is_file() else {}
        result = dict(existing)
        result.update(
            {
                "attempt_id": submission_dir.name,
                "exit_code": representative.get("exit_code"),
                "poc_sha256": key if not key.startswith("missing:") else existing.get("poc_sha256"),
                "poc_length": poc_path.stat().st_size if poc_path.is_file() else existing.get("poc_length"),
                "runtime_output_path": f"submissions/{submission_dir.name}/runtime_output.txt",
                "analysis_path": (
                    f"submissions/{submission_dir.name}/analysis.json"
                    if (submission_dir / "analysis.json").is_file()
                    else None
                ),
                "poc_path": f"submissions/{submission_dir.name}/poc.bin",
                "validation": representative.get("validation"),
                "triggered": representative.get("triggered"),
                "poc_hash": key if not key.startswith("missing:") else existing.get("poc_sha256"),
                "vul_exit_code": representative.get("vul_exit_code"),
                "analysis_valid": (submission_dir / "analysis.json").is_file(),
                "dedup_revalidation_reused_from": representative.get("attempt_id"),
            }
        )
        result.pop("runtime_unavailable", None)
        if representative.get("error"):
            result["error"] = representative.get("error")
        else:
            result.pop("error", None)
        source_runtime = sample_result_dir / str(representative.get("runtime_output_path") or "")
        if source_runtime.is_file():
            runtime_output_path.write_text(
                (
                    f"deduplicated revalidation reused runtime result from "
                    f"{representative.get('attempt_id')}\n\n"
                    + source_runtime.read_text(encoding="utf-8", errors="replace")
                ),
                encoding="utf-8",
                errors="replace",
            )
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        submissions.append(result)

    update_manifest(sample_result_dir, submissions)
    return {
        "sample_id": sample_id,
        "status": "revalidated",
        "submissions": len(submissions),
        "unique_pocs_validated": len(representative_dirs),
        "duplicate_submissions_reused": reused,
        "triggered": sum(1 for item in submissions if item.get("triggered") is True),
        "runtime_unavailable": sum(1 for item in submissions if item.get("validation") == "runtime_unavailable"),
        "nonzero": sum(1 for item in submissions if item.get("exit_code") not in (0, None)),
    }


def force_rebuild_runtime(sample_id: str) -> dict:
    gt_dir = GT_RESULTS / sample_id
    spec = compile_runtime_spec(gt_dir, require_artifacts=False)
    removed: list[str] = []
    if spec.backend != "local_workspace" or not spec.build_commands:
        return {"removed": removed, "reason": "no_local_build_commands"}
    executable = container_path_on_host(gt_dir, spec.executable, spec.workdir)
    try:
        if executable.exists() or executable.is_symlink():
            executable.unlink()
            removed.append(str(executable))
    except IsADirectoryError:
        return {"removed": removed, "error": f"runtime executable is a directory: {executable}"}
    return {"removed": removed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--sample-id", action="append")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="remove each sample's runtime executable before validating submissions",
    )
    parser.add_argument(
        "--all-submissions",
        action="store_true",
        help="validate every submission instead of one representative per PoC hash",
    )
    args = parser.parse_args(argv)

    samples = args.sample_id or valid_oss_nvd_samples()
    rows = []
    for model in args.model:
        for sample_id in samples:
            if not is_oss_or_nvd(sample_id):
                continue
            sample_result_dir = POC_RESULTS / model / sample_id
            has_submissions = (sample_result_dir / "submissions").is_dir()
            rebuild = (
                force_rebuild_runtime(sample_id)
                if args.force_rebuild and has_submissions
                else None
            )
            row = {
                "model": model,
                **revalidate_sample_impl(
                    model=model,
                    sample_id=sample_id,
                    timeout=args.timeout,
                    all_submissions=args.all_submissions,
                ),
            }
            if rebuild is not None:
                row["force_rebuild"] = rebuild
            rows.append(row)
            out = POC_RESULTS / "oss_nvd_revalidation_report.json"
            out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
            print(json.dumps(row, ensure_ascii=False), flush=True)
    out = POC_RESULTS / "oss_nvd_revalidation_report.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    failed = any(row.get("status") not in {"revalidated", "no_submissions"} for row in rows)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
