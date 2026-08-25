#!/usr/bin/env python3
"""Sangfor AI protocol wrapper for PoC generation.

The public Sangfor CyberGym repositories currently publish methodology and
results only, not a runnable agent implementation. This wrapper preserves a
separate harness identity while reusing the local OpenHands execution substrate
so the same ARVO/non-ARVO task materials, submission hooks, and reachability
artifacts are exercised under the Sangfor-style evaluation protocol.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SANGFOR_SOURCES = [
    "https://github.com/Sangfor-AI/cybergym-submission-sangfor-ai",
    "https://github.com/Sangfor-AI/cybergym-submission-sangfor-ai-v2",
]


def _build_openhands_command(args: argparse.Namespace) -> tuple[list[str], str]:
    common = [
        "--model", args.model,
        "--base-url", args.base_url,
        "--max-iter", str(args.max_iter),
        "--timeout", str(args.timeout),
        "--results-dir", str(args.results_dir),
        "--prompt-file", str(args.prompt_file),
    ]
    if args.api_key_env:
        common += ["--api-key-env", args.api_key_env]
    if args.api_version:
        common += ["--api-version", args.api_version]
    if args.provider_kind:
        common += ["--provider-kind", args.provider_kind]
    if args.workspace_installer:
        common += ["--workspace-installer", args.workspace_installer]

    if args.arvo_id:
        sample_id = f"arvo_{args.arvo_id}"
        command = [
            sys.executable,
            str(REPO_ROOT / "harness_runtime" / "openhands" / "arvo.py"),
            "--arvo-id", args.arvo_id,
            "--max-attempts", str(args.max_attempts),
            "--server", args.server,
            "--difficulty", args.difficulty,
            "--server-root", str(args.server_root),
            "--harness-profile", args.harness_profile,
            "--openhands-repo", str(args.openhands_repo),
            *common,
        ]
        return command, sample_id

    if not args.sample_id:
        raise SystemExit("either --arvo-id or --sample-id is required")
    command = [
        sys.executable,
        str(REPO_ROOT / "harness_runtime" / "openhands" / "local.py"),
        "--sample-id", args.sample_id,
        "--openhands-repo", str(args.openhands_repo),
        *common,
    ]
    return command, args.sample_id


def _annotate_manifest(results_dir: Path, sample_id: str, command: list[str]) -> None:
    manifest_path = results_dir / sample_id / "manifest.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    underlying = manifest.get("harness")
    manifest["harness"] = "sangfor_ai"
    manifest["underlying_harness"] = underlying or "openhands"
    manifest["sangfor_ai"] = {
        "implementation": "protocol_wrapper",
        "source_repositories": SANGFOR_SOURCES,
        "public_code_available": False,
        "public_release_contents": "README methodology/results only",
        "execution_substrate": "openhands",
        "methodology": "evidence-governed CyberGym reproduction protocol with isolated task materials and a single final PoC submission",
    }
    manifest["command_backend"] = {
        "wrapper": "harness_runtime/sangfor_ai/wrapper.py",
        "delegated_command": command,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arvo-id", default="")
    parser.add_argument("--sample-id", default="")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--server", default="http://host.docker.internal:8666")
    parser.add_argument("--difficulty", default="level1")
    parser.add_argument(
        "--server-root",
        type=Path,
        default=REPO_ROOT / "harness_runtime" / "server",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key-env", default="")
    parser.add_argument("--api-version", default="")
    parser.add_argument("--provider-kind", default="")
    parser.add_argument("--max-iter", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=10800)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--harness-profile", choices=("standard",), default="standard")
    parser.add_argument(
        "--openhands-repo",
        type=Path,
        default=REPO_ROOT / "external" / "OpenHands",
    )
    parser.add_argument("--workspace-installer", default="")
    args = parser.parse_args()

    command, sample_id = _build_openhands_command(args)
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    try:
        _annotate_manifest(args.results_dir.expanduser().resolve(), sample_id, command)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[sangfor-ai-wrapper] manifest annotation failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
