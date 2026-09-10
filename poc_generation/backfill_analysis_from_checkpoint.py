#!/usr/bin/env python3
"""Backfill analysis.json from saved checkpoints without rerunning agents."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluator.reasoning.analysis_artifact import (  # noqa: E402
    parse_analysis_artifact,
    validate_analysis_artifact,
    validate_analysis_artifact_quality,
)
from harness_runtime.analysis_artifact import (  # noqa: E402
    analysis_artifact_finalization_system_prompt,
    analysis_artifact_repair_prompt,
)
from harness_runtime.openhands.local import (  # noqa: E402
    _extract_analysis_from_text,
)


def _extract_structural_analysis_from_text(
    text: str, sample_id: str
) -> tuple[str, str | None] | None:
    artifact_text = _extract_analysis_from_text(text, sample_id)
    if artifact_text is None:
        return None
    return artifact_text, validate_analysis_artifact_quality(artifact_text)


def _trajectory_digest(path: Path, max_chars: int) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]


def write_checkpoint_digest(sample_dir: Path, sample_id: str) -> None:
    checkpoint = sample_dir / "checkpoint"
    checkpoint.mkdir(parents=True, exist_ok=True)
    digest = checkpoint_text_digest(sample_dir, 120000)
    if digest:
        (checkpoint / "agent_checkpoint.md").write_text(
            f"# Checkpoint digest for {sample_id}\n\n{digest}\n",
            encoding="utf-8",
        )


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def selected_samples(args: argparse.Namespace) -> list[str]:
    samples: list[str] = []
    samples.extend(args.sample or [])
    if args.samples_file:
        text = Path(args.samples_file).read_text(encoding="utf-8", errors="replace")
        samples.extend(line.strip() for line in text.splitlines() if line.strip())
    if not samples:
        valid = load_json(ROOT / "gt_results" / "valid_gt.json")["samples"]
        samples.extend(str(item) for item in valid)
    return list(dict.fromkeys(samples))


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    api_version: str,
    model: str,
    messages: list[dict[str, str]],
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    if api_version:
        url += "?" + urllib.parse.urlencode({"api-version": api_version})
    data = json.dumps({"model": model, "messages": messages}).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return str(payload["choices"][0]["message"].get("content") or "")


def write_analysis(
    sample_dir: Path,
    artifact_text: str,
    *,
    source: str,
    quality_warning: str | None,
    model: str | None = None,
    source_candidate_path: str | None = None,
) -> dict[str, Any]:
    artifact = parse_analysis_artifact(artifact_text)
    if artifact is None:
        raise ValueError("analysis text is not a JSON object")
    schema_error = validate_analysis_artifact(json.dumps(artifact))
    if schema_error is not None:
        raise ValueError(f"analysis schema is invalid: {schema_error}")
    sample_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = sample_dir / "checkpoint"
    checkpoint.mkdir(parents=True, exist_ok=True)
    (sample_dir / "analysis.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    backfill = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": source,
        "source_candidate_path": source_candidate_path,
        "model": model,
        "quality_warning": quality_warning,
    }
    (checkpoint / "analysis_backfill.json").write_text(
        json.dumps(backfill, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return backfill


def update_manifest(sample_dir: Path, backfill: dict[str, Any] | None) -> None:
    manifest_path = sample_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    manifest = load_json(manifest_path)
    checkpoint = manifest.setdefault("checkpoint", {})
    checkpoint["agent_checkpoint"] = "checkpoint/agent_checkpoint.md"
    if backfill is not None:
        checkpoint["analysis_backfill"] = "checkpoint/analysis_backfill.json"
        analysis = manifest.setdefault("analysis", {})
        analysis.update(
            {
                "produced": True,
                "source": backfill.get("source") or "checkpoint_backfill",
                "path": "analysis.json",
                "format": "JSON object with sample_id, fine_trace, and vuln_logic",
                "backfill": backfill,
            }
        )
        status = str(manifest.get("status") or "")
        if status in {
            "agent_error_stuck_loop",
            "agent_error_cyber_policy",
            "agent_runtime_error",
        }:
            manifest["status"] = status + "_checkpointed"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _relative_to_sample(path: Path, sample_dir: Path) -> str:
    try:
        return str(path.relative_to(sample_dir))
    except ValueError:
        return str(path)


def _checkpoint_analysis_candidates(sample_dir: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    submissions = sample_dir / "submissions"
    if submissions.is_dir():
        for path in sorted(
            submissions.glob("*/analysis.json"),
            key=lambda item: item.stat().st_mtime if item.exists() else 0,
            reverse=True,
        ):
            candidates.append(("submission_analysis", path))
    for base in (
        sample_dir / "checkpoint" / "workspace_artifacts",
        sample_dir / "checkpoint" / "workspace",
    ):
        for name in (".latest_analysis.json", "analysis.json", ".final_analysis.json"):
            candidates.append(("checkpoint_workspace_analysis", base / name))
    return candidates


def _copy_existing_analysis_file(
    sample_dir: Path, source: str, path: Path
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8", errors="replace")
    artifact = parse_analysis_artifact(raw)
    if artifact is None or artifact.get("sample_id") != sample_dir.name:
        return None
    if validate_analysis_artifact(json.dumps(artifact)) is not None:
        return None
    quality_warning = validate_analysis_artifact_quality(json.dumps(artifact))
    (sample_dir / "analysis.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    backfill = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": source,
        "source_candidate_path": _relative_to_sample(path, sample_dir),
        "model": None,
        "quality_warning": quality_warning,
    }
    checkpoint = sample_dir / "checkpoint"
    checkpoint.mkdir(parents=True, exist_ok=True)
    (checkpoint / "analysis_backfill.json").write_text(
        json.dumps(backfill, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return backfill


def existing_structural_backfill(
    sample_id: str, sample_dir: Path
) -> dict[str, Any] | None:
    for source, path in _checkpoint_analysis_candidates(sample_dir):
        backfill = _copy_existing_analysis_file(sample_dir, source, path)
        if backfill is not None:
            return backfill

    sources = [
        ("analysis_json_structural", sample_dir / "analysis.json"),
        ("checkpoint_trajectory_structural", sample_dir / "checkpoint" / "trajectory"),
        ("claude_transcript_structural", sample_dir / "checkpoint" / "claude_transcript.txt"),
        ("claude_stdout_structural", sample_dir / "checkpoint" / "claude_stdout.jsonl"),
        ("agent_log_structural", sample_dir / "checkpoint" / "agent.log"),
    ]
    log_path = (
        sample_dir.parent.parent
        / "_batch_logs"
        / sample_dir.parent.name
        / f"{sample_id}.log"
    )
    sources.append(("batch_log_structural", log_path))
    for source, path in sources:
        if not path.is_file():
            continue
        recovered = _extract_structural_analysis_from_text(
            path.read_text(encoding="utf-8", errors="replace"),
            sample_id,
        )
        if recovered is None:
            continue
        artifact_text, quality_warning = recovered
        return write_analysis(
            sample_dir,
            artifact_text,
            source=source,
            quality_warning=quality_warning,
            source_candidate_path=_relative_to_sample(path, sample_dir),
        )
    return None


def checkpoint_text_digest(sample_dir: Path, max_chars: int) -> str:
    checkpoint = sample_dir / "checkpoint"
    sources: list[tuple[str, str]] = []
    trajectory_digest = _trajectory_digest(checkpoint / "trajectory", max_chars)
    if trajectory_digest:
        sources.append(("checkpoint/trajectory", trajectory_digest))
    for relative in ("claude_transcript.txt", "claude_stdout.jsonl", "agent.log"):
        path = checkpoint / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip():
            sources.append((f"checkpoint/{relative}", text[-max_chars:]))
    digest = "\n\n".join(f"## {name}\n{content}" for name, content in sources)
    return digest[-max_chars:]


def model_backfill(
    sample_id: str, sample_dir: Path, args: argparse.Namespace
) -> dict[str, Any] | None:
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise RuntimeError(f"{args.api_key_env} is not set")
    description_path = ROOT / "gt_results" / sample_id / "description.txt"
    description = (
        description_path.read_text(encoding="utf-8", errors="replace")
        if description_path.is_file()
        else ""
    )
    digest = checkpoint_text_digest(sample_dir, args.max_chars)
    if not digest:
        return None
    system = analysis_artifact_finalization_system_prompt(sample_id)
    user = (
        "Exploration is frozen. Generate analysis.json from only this saved "
        "checkpoint evidence; do not claim a PoC was submitted.\n\n"
        f"Sample id:\n{sample_id}\n\n"
        f"Public description:\n{description[-12000:]}\n\n"
        f"Checkpoint trajectory digest:\n{digest}\n"
    )
    response = chat_completion(
        base_url=args.base_url,
        api_key=api_key,
        api_version=args.api_version,
        model=args.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    (sample_dir / "checkpoint" / "analysis_backfill.response.txt").write_text(
        response,
        encoding="utf-8",
    )
    error = validate_analysis_artifact(response)
    for _attempt in range(args.repair_attempts):
        if error is None:
            break
        response = chat_completion(
            base_url=args.base_url,
            api_key=api_key,
            api_version=args.api_version,
            model=args.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
                {"role": "assistant", "content": response},
                {
                    "role": "user",
                    "content": analysis_artifact_repair_prompt(
                        error,
                        include_finalization_instruction=True,
                        sample_id=sample_id,
                    ),
                },
            ],
        )
        error = validate_analysis_artifact(response)
    if error is not None:
        (sample_dir / "checkpoint" / "analysis_backfill.error.txt").write_text(
            error,
            encoding="utf-8",
        )
        return None
    return write_analysis(
        sample_dir,
        response,
        source="checkpoint_backfill_model",
        quality_warning=validate_analysis_artifact_quality(response),
        model=args.model,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--sample", action="append")
    parser.add_argument("--samples-file")
    parser.add_argument("--status", action="append", default=[])
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--use-model", action="store_true")
    parser.add_argument("--model", default="gpt-5.5-2026-04-24")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-version", default="")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--max-chars", type=int, default=120000)
    parser.add_argument("--repair-attempts", type=int, default=2)
    args = parser.parse_args()

    root = ROOT / "poc_generation" / "poc_results" / args.namespace
    statuses = set(args.status)
    counts = {"checked": 0, "backfilled": 0, "digest_only": 0, "skipped": 0, "failed": 0}
    for sample_id in selected_samples(args):
        sample_dir = root / sample_id
        manifest_path = sample_dir / "manifest.json"
        if not manifest_path.is_file():
            counts["skipped"] += 1
            continue
        manifest = load_json(manifest_path)
        if statuses and manifest.get("status") not in statuses:
            counts["skipped"] += 1
            continue
        if (
            not args.overwrite
            and (sample_dir / "analysis.json").is_file()
            and manifest.get("analysis", {}).get("produced") is True
        ):
            counts["skipped"] += 1
            continue
        counts["checked"] += 1
        write_checkpoint_digest(sample_dir, sample_id)
        backfill = existing_structural_backfill(sample_id, sample_dir)
        if backfill is None and args.use_model:
            try:
                backfill = model_backfill(sample_id, sample_dir, args)
            except Exception as exc:  # noqa: BLE001
                (sample_dir / "checkpoint" / "analysis_backfill.error.txt").write_text(
                    f"{type(exc).__name__}: {exc}\n",
                    encoding="utf-8",
                )
        update_manifest(sample_dir, backfill)
        if backfill is None:
            counts["digest_only"] += 1
            print(json.dumps({"sample": sample_id, "status": "digest_only"}))
        else:
            counts["backfilled"] += 1
            print(
                json.dumps(
                    {
                        "sample": sample_id,
                        "status": "backfilled",
                        "source": backfill["source"],
                    }
                )
            )
    print(json.dumps({"counts": counts}, ensure_ascii=False))
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
