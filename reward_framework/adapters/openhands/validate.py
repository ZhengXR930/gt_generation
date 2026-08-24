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
    REQUIRED_REPRODUCTION_HELPERS,
    REQUIRED_SUBMISSION_HELPERS,
    SUBMISSION_SKILL_REL,
    WORKSPACE_STATE_DIR,
)


REQUIRED = {
    REPRODUCTION_SKILL_REL: ("Reproduction Skill", WORKSPACE_STATE_DIR),
    SUBMISSION_SKILL_REL: ("Submission Skill", WORKSPACE_STATE_DIR),
    **{
        f"submission_skill/helpers/{name}": ()
        for name in REQUIRED_SUBMISSION_HELPERS
    },
    **{
        f"reproduction_skill/helpers/{name}": ()
        for name in REQUIRED_REPRODUCTION_HELPERS
    },
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
    "Reproduction Skill",
    "Submission Skill",
    "TRAIN evidence",
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
        (work / "candidate.bin").write_bytes(b"issue reproduction candidate\n")
        (work / "note.md").write_text(
            "candidate goal: test parser/source/root-cause/sink/trigger alignment\n",
            encoding="utf-8",
        )
        (work / "analysis.md").write_text(
            "current hypothesis: the target consumes candidate.bin as input\n",
            encoding="utf-8",
        )
        commands = [
            [
                "python3",
                str(packet / "submission_skill/helpers/candidate_diff.py"),
                "--current",
                "candidate.bin",
                "--history-jsonl",
                f"{WORKSPACE_STATE_DIR}/submit_history.jsonl",
                "--out",
                f"{WORKSPACE_STATE_DIR}/candidate_diff.json",
            ],
            [
                "python3",
                str(packet / "submission_skill/helpers/submit_preflight.py"),
                "--candidate",
                "candidate.bin",
                "--artifact-kind",
                "raw",
                "--analysis",
                "analysis.md",
                "--note-file",
                "note.md",
                "--evidence-file",
                "analysis.md",
                "--out",
                f"{WORKSPACE_STATE_DIR}/preflight.json",
            ],
            [
                "python3",
                str(packet / "submission_skill/helpers/submit_command_lint.py"),
                "--command",
                "bash submit.sh candidate.bin analysis.json",
                "--out",
                f"{WORKSPACE_STATE_DIR}/submit_command_lint.json",
            ],
            [
                "python3",
                str(packet / "submission_skill/helpers/submit_history.py"),
                "record",
                "--candidate",
                "candidate.bin",
                "--candidate-kind",
                "raw",
                "--analysis",
                "analysis.md",
                "--preflight-report",
                f"{WORKSPACE_STATE_DIR}/preflight.json",
                "--submission-status",
                "smoke",
                "--repair-class",
                "unknown",
                "--note",
                "smoke",
            ],
            [
                "python3",
                str(packet / "submission_skill/helpers/submit_history.py"),
                "summarize",
                "--out",
                f"{WORKSPACE_STATE_DIR}/summary.md",
            ],
            [
                "python3",
                str(packet / "reproduction_skill/helpers/candidate_plan.py"),
                "--issue-file",
                "note.md",
                "--code-notes",
                "analysis.md",
                "--previous-plan",
                f"{WORKSPACE_STATE_DIR}/summary.md",
                "--out",
                f"{WORKSPACE_STATE_DIR}/candidate_plan.md",
            ],
            [
                "python3",
                str(packet / "reproduction_skill/helpers/issue_code_alignment.py"),
                "--issue-file",
                "note.md",
                "--code-notes",
                "analysis.md",
                "--plan",
                f"{WORKSPACE_STATE_DIR}/candidate_plan.md",
                "--out",
                f"{WORKSPACE_STATE_DIR}/issue_code_alignment.json",
            ],
        ]
        for cmd in commands:
            proc = subprocess.run(cmd, cwd=work, text=True, capture_output=True, check=False)
            if proc.returncode != 0:
                fail(
                    "helper smoke failed: "
                    + " ".join(cmd)
                    + f"\nstdout={proc.stdout}\nstderr={proc.stderr}"
                )
        expected_outputs = [
            "candidate_diff.json",
            "preflight.json",
            "submit_command_lint.json",
            "submit_history.jsonl",
            "summary.md",
            "candidate_plan.md",
            "issue_code_alignment.json",
        ]
        for name in expected_outputs:
            path = state / name
            if not path.is_file() or path.stat().st_size <= 0:
                fail(f"helper smoke missing output: {name}")
    notes.append(f"packet ok: {packet}")
    return notes


def check_prompts(prompt_dir: Path) -> list[str]:
    notes: list[str] = []
    if not prompt_dir.is_dir():
        fail(f"missing prompt dir: {prompt_dir}")
    for path in sorted(prompt_dir.glob("*.txt")):
        text = _read(path)
        for bad in FORBIDDEN_TEXT:
            if bad in text:
                fail(f"{path.name} contains forbidden stale text {bad!r}")
    combined = "\n".join(_read(p) for p in sorted(prompt_dir.glob("*.txt")))
    for needle in PROMPT_REQUIRED:
        if needle not in combined:
            fail(f"prompts missing required interface phrase {needle!r}")
    notes.append(f"prompts ok: {prompt_dir}")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--packet",
        action="append",
        default=[],
        help="Skill packet directory to validate. May be repeated.",
    )
    parser.add_argument(
        "--prompts",
        default=str(REPO_ROOT / "reward_framework/offline_static_distillation/prompts"),
    )
    args = parser.parse_args(argv)

    packets = [Path(p).resolve() for p in args.packet]
    if not packets:
        packets = [REPO_ROOT / "reward_framework/offline_static_distillation/templates/skill_packet"]
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
