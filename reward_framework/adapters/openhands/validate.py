#!/usr/bin/env python3
"""Validate OpenHands skill adapter inputs before running model tests."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reward_framework.adapters.openhands.contract import (  # noqa: E402
    REPRODUCTION_SKILL_REL,
    REQUIRED_SUBMISSION_HELPERS,
    SUBMISSION_SKILL_REL,
    WORKSPACE_STATE_DIR,
)

REQUIRED = {
    REPRODUCTION_SKILL_REL: ("PoC Reproduction Skill",),
    SUBMISSION_SKILL_REL: ("PoC Submission Skill",),
    **{f"submission_skill/helpers/{name}": () for name in REQUIRED_SUBMISSION_HELPERS},
}

FORBIDDEN_TEXT = (
    ".gt_skill_state",
    "Static GT PoC",
    "GT trace",
    "GT feedback",
    "aim for at least three effective submits",
    "schema gate",
)

PROMPT_REQUIRED = (
    "description.txt",
    "submit.sh",
    "analysis.json",
)


def fail(msg: str) -> None:
    raise AssertionError(msg)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def check_packet(packet: Path) -> list[str]:
    notes: list[str] = []
    if not packet.is_dir():
        fail(f"missing packet dir: {packet}")
    for rel, required_texts in REQUIRED.items():
        path = packet / rel
        if not path.is_file():
            fail(f"missing required packet file: {packet}/{rel}")
        text = _read(path)
        for needle in required_texts:
            if needle not in text:
                fail(f"{rel} missing required text {needle!r}")
        for bad in FORBIDDEN_TEXT:
            if bad in text:
                fail(f"{rel} contains forbidden stale text {bad!r}")
        if path.suffix == ".py":
            compile(text, str(path), "exec")

    with tempfile.TemporaryDirectory(prefix="skill_interface_smoke.") as tmp:
        work = Path(tmp)
        state = work / WORKSPACE_STATE_DIR
        state.mkdir()
        candidate = work / "candidate.bin"
        analysis = work / "analysis.json"
        result = work / "result.json"
        candidate.write_bytes(b"issue reproduction candidate\n")
        analysis.write_text('{"sample_id":"smoke"}\n', encoding="utf-8")
        result.write_text('{"exit_code":0}\n', encoding="utf-8")

        commands = [
            [
                "python3",
                str(packet / "submission_skill/helpers/submit_preflight.py"),
                "--candidate",
                str(candidate),
                "--history-jsonl",
                str(state / "submit_history.jsonl"),
                "--analysis",
                str(analysis),
                "--out",
                str(state / "preflight.json"),
            ],
            [
                "python3",
                str(packet / "submission_skill/helpers/submit_history.py"),
                "record",
                "--candidate",
                str(candidate),
                "--analysis",
                str(analysis),
                "--history-jsonl",
                str(state / "submit_history.jsonl"),
                "--result-json",
                str(result),
                "--status",
                "smoke",
                "--note",
                "smoke",
            ],
            [
                "python3",
                str(packet / "submission_skill/helpers/submit_history.py"),
                "summarize",
                "--history-jsonl",
                str(state / "submit_history.jsonl"),
                "--out",
                str(state / "submit_history_summary.json"),
            ],
        ]
        for cmd in commands:
            proc = subprocess.run(cmd, cwd=work, text=True, capture_output=True, check=False)
            if proc.returncode != 0:
                fail(
                    "helper smoke failed: "
                    + " ".join(cmd)
                    + "\nstdout="
                    + proc.stdout
                    + "\nstderr="
                    + proc.stderr
                )
        for name in ("preflight.json", "submit_history.jsonl", "submit_history_summary.json"):
            path = state / name
            if not path.is_file() or path.stat().st_size <= 0:
                fail(f"helper smoke missing output: {name}")
    notes.append(f"packet ok: {packet}")
    return notes


def check_prompts(prompt_root: Path) -> list[str]:
    notes: list[str] = []
    files = [prompt_root] if prompt_root.is_file() else sorted(prompt_root.glob("*.txt"))
    if not files:
        fail(f"missing prompt file(s): {prompt_root}")
    combined = "\n".join(_read(path) for path in files)
    for bad in FORBIDDEN_TEXT:
        if bad in combined:
            fail(f"prompt contains forbidden stale text {bad!r}")
    for needle in PROMPT_REQUIRED:
        if needle not in combined:
            fail(f"prompts missing required interface phrase {needle!r}")
    notes.append(f"prompts ok: {prompt_root}")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", action="append", default=[])
    parser.add_argument("--prompts", default=str(REPO_ROOT / "reward_framework" / "prompt.txt"))
    args = parser.parse_args(argv)

    packets = [Path(p).resolve() for p in args.packet]
    if not packets:
        packets = [REPO_ROOT / "reward_framework" / "skill_packets" / "initial"]
    notes: list[str] = []
    for packet in packets:
        notes.extend(check_packet(packet))
    notes.extend(check_prompts(Path(args.prompts).resolve()))
    print(json.dumps({"status": "pass", "notes": notes}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, indent=2, ensure_ascii=False))
        raise
