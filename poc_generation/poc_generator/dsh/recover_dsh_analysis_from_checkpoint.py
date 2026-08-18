#!/usr/bin/env python3
"""Recover DSH analysis.json artifacts from saved checkpoint sessions.

This script is deliberately extraction-only: it does not ask an LLM anything.
It scans saved DeepSeek Harness JSONL events for an analysis artifact the
subject agent already wrote or said, validates it with the same evaluator-side
schema/quality checks, and patches the sample manifest to record the recovery.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
GENERATOR_ROOT = HERE.parent
GT_ROOT = GENERATOR_ROOT.parents[1]
RESULTS_ROOT = GENERATOR_ROOT.parent / "poc_results"

sys.path.insert(0, str(GENERATOR_ROOT))
sys.path.insert(0, str(GT_ROOT))

from evaluator.reasoning.analysis_artifact import (  # noqa: E402
    validate_analysis_artifact_quality,
)


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def event_texts(event: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    data = event.get("data")
    if not isinstance(data, dict):
        return texts

    message = data.get("message")
    if isinstance(message, dict):
        for block in message.get("content") or []:
            if isinstance(block, dict):
                if block.get("type") in {"text", "reasoning"}:
                    texts.append(str(block.get("text") or ""))
                if block.get("type") == "tool-call":
                    arguments = block.get("arguments")
                    if isinstance(arguments, str):
                        texts.append(arguments)

    chunk = data.get("chunk")
    if isinstance(chunk, dict):
        text = chunk.get("text")
        if isinstance(text, str):
            texts.append(text)
        arguments = chunk.get("arguments")
        if isinstance(arguments, str):
            texts.append(arguments)

    if event.get("type") == "tool/call":
        arguments = data.get("arguments")
        if isinstance(arguments, str):
            texts.append(arguments)

    return texts


def valid_artifact_from_text(text: str, sample_id: str) -> tuple[dict[str, Any] | None, str | None]:
    last_error: str | None = None
    for obj in reversed(extract_json_objects(text)):
        if obj.get("sample_id") != sample_id:
            continue
        if set(obj) != {"sample_id", "fine_trace", "vuln_logic"}:
            continue
        raw = json.dumps(obj, ensure_ascii=False)
        error = validate_analysis_artifact_quality(raw)
        if error is not None:
            last_error = error
            continue
        return obj, None
    return None, last_error


def recover_sample(sample_dir: Path) -> dict[str, Any]:
    sample_id = sample_dir.name
    checkpoint = sample_dir / "checkpoint" / "dsh_home" / "sessions-jsonl"
    session_files = sorted(checkpoint.glob("**/session.jsonl"))
    candidates_seen = 0
    last_error: str | None = None
    for session_file in reversed(session_files):
        try:
            lines = session_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            last_error = f"read failed: {exc}"
            continue
        # Later events are better recovery candidates.
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            joined = "\n".join(event_texts(event))
            if not joined or ("sample_id" not in joined and "fine_trace" not in joined):
                continue
            candidates_seen += 1
            artifact, error = valid_artifact_from_text(joined, sample_id)
            if error:
                last_error = error
            if artifact is None:
                continue
            analysis_path = sample_dir / "analysis.json"
            analysis_path.write_text(
                json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            manifest_path = sample_dir / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["analysis"] = {
                    "produced": True,
                    "source": "checkpoint_session_recovered",
                    "path": "analysis.json",
                    "format": "JSON object with sample_id, fine_trace, and vuln_logic",
                }
                manifest["analysis_recovery"] = {
                    "status": "recovered",
                    "method": "extract_valid_artifact_from_dsh_checkpoint_jsonl",
                    "session_file": str(session_file.relative_to(sample_dir)),
                    "candidates_seen": candidates_seen,
                }
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            return {
                "sample": sample_id,
                "status": "recovered",
                "session_file": str(session_file),
                "candidates_seen": candidates_seen,
            }
    return {
        "sample": sample_id,
        "status": "not_found",
        "session_files": len(session_files),
        "candidates_seen": candidates_seen,
        "last_validation_error": last_error,
    }


def failed_samples_from_summary(summary_path: Path) -> list[str]:
    samples: list[str] = []
    for line in summary_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "failed":
            sample = str(record.get("sample") or "")
            if sample:
                samples.append(sample)
    return samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("samples", nargs="*")
    args = parser.parse_args()

    samples = list(args.samples)
    if args.summary is not None:
        samples.extend(failed_samples_from_summary(args.summary))
    samples = list(dict.fromkeys(samples))
    root = RESULTS_ROOT / args.namespace
    for sample in samples:
        result = recover_sample(root / sample)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
