#!/usr/bin/env python3
"""Validate that PoC generation harness adapters share one public contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reward_framework.adapters.poc_generation import (  # noqa: E402
    PocHarnessRequest,
    build_poc_harness_command,
    supported_harnesses,
)
from reward_framework.adapters.poc_task_contract import (  # noqa: E402
    canonical_openhands_prompt_text,
    render_poc_task_prompt,
    validate_analysis_json_text,
)


EXPECTED_HARNESSES = ("claude", "codex", "deepseek_harness", "openhands")


def fail(message: str) -> None:
    raise AssertionError(message)


def normalize_prompt(text: str) -> str:
    text = re.sub(r"Sample id: .*", "Sample id: <sample>", text)
    text = re.sub(r"Workspace: .*", "Workspace: <workspace>", text)
    text = re.sub(r"Soft iteration budget: .*", "Soft iteration budget: <budget> reasoning/tool steps.", text)
    text = text.replace("bash ./submit.sh PATH_TO_POC /workspace/analysis.json", "bash ./submit.sh PATH_TO_POC <workspace>/analysis.json")
    text = re.sub(r"bash ./submit\.sh PATH_TO_POC .*/analysis\.json", "bash ./submit.sh PATH_TO_POC <workspace>/analysis.json", text)
    text = re.sub(r'"sample_id":"[^"]+"', '"sample_id":"<sample>"', text)
    text = re.sub(r"sample_id is exactly .*\.", "sample_id is exactly <sample>.", text)
    return text


def check_prompt_contract() -> list[str]:
    notes: list[str] = []
    canonical = normalize_prompt(canonical_openhands_prompt_text())
    rendered = {
        harness: render_poc_task_prompt(
            sample_id="arvo_42470405",
            workspace="/workspace",
            max_iter=7,
            skill_packet_enabled=False,
        )
        for harness in EXPECTED_HARNESSES
    }
    for harness, prompt in rendered.items():
        if normalize_prompt(prompt) != canonical:
            fail(f"{harness} prompt does not match the canonical OpenHands prompt")
        if "sample_id" not in prompt or "fine_trace" not in prompt or "vuln_logic" not in prompt:
            fail(f"{harness} prompt missing analysis.json schema terms")
    cli_prompt = render_poc_task_prompt(
        sample_id="arvo_42470405",
        workspace="/tmp/example_workspace",
        max_iter=7,
        skill_packet_enabled=False,
    )
    if "/tmp/example_workspace/README.md" not in cli_prompt:
        fail("workspace path rewrite did not apply for CLI prompt")
    notes.append("all harnesses reuse the canonical OpenHands PoC prompt")
    return notes


def check_analysis_contract() -> list[str]:
    notes: list[str] = []
    good = json.dumps(
        {
            "sample_id": "arvo_42470405",
            "fine_trace": [
                {
                    "step": 1,
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 10,
                    "var": "len",
                    "code": "len = input[0];",
                    "role": "source",
                    "note": "input-controlled length enters parser",
                },
                {
                    "step": 2,
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 20,
                    "var": "len < cap",
                    "code": "memcpy(dst, src, len);",
                    "role": "sink",
                    "note": "copy uses length at target operation",
                },
            ],
            "vuln_logic": {
                "source": {"file": "src/parser.c", "function": "parse", "line": 10, "operands": ["len"]},
                "sink": {
                    "file": "src/parser.c",
                    "function": "parse",
                    "line": 20,
                    "operands": ["len", "cap"],
                    "relation": {"op": "lt", "left": "len", "right": "cap"},
                },
                "propagation": [],
            },
        }
    )
    ok, errors = validate_analysis_json_text(good, expected_sample_id="arvo_42470405")
    if not ok:
        fail(f"valid analysis rejected: {errors}")
    bad = json.dumps({"sample_id": "wrong", "fine_trace": [], "vuln_logic": {}})
    ok, errors = validate_analysis_json_text(bad, expected_sample_id="arvo_42470405")
    if ok or not errors:
        fail("invalid analysis was accepted")
    submit_template = REPO_ROOT / "external" / "cybergym" / "src" / "cybergym" / "task" / "submit.template"
    submit_text = submit_template.read_text(encoding="utf-8", errors="replace")
    for needle in ("sample_id", "fine_trace", "vuln_logic"):
        if needle not in submit_text:
            fail(f"submit.template missing schema key {needle}")
    notes.append("analysis.json contract shared and deterministic")
    return notes


def check_adapter_commands(model: str) -> list[str]:
    notes: list[str] = []
    harnesses = supported_harnesses()
    if harnesses != EXPECTED_HARNESSES:
        fail(f"unexpected harness set: {harnesses}")
    for harness in harnesses:
        command = build_poc_harness_command(
            PocHarnessRequest(
                harness=harness,
                arvo_id="42470405",
                model=model,
                base_url="https://example.invalid/chat/completions",
                api_key_env="OPENAI_API_KEY",
                api_version="2024-03-01-preview",
                max_iter=7,
                max_attempts=1,
                timeout=60,
                server="http://host.docker.internal:8666",
                difficulty="level1",
                results_dir=Path("/tmp/rf-unification-results"),
                skill_packet_dir=None,
            )
        )
        if command.sample_id != "arvo_42470405":
            fail(f"wrong sample id for {harness}: {command.sample_id}")
        if harness == "openhands" and not command.runner.endswith("run_sample"):
            fail("OpenHands must route through run_sample")
        if harness != "openhands" and not command.runner.endswith("run_cli_sample"):
            fail(f"{harness} must route through run_cli_sample")
        for forbidden in ("CYBERGYM_OPENHANDS_SKILL_PACKET_DIR", "REWARD_FRAMEWORK_POC_SKILL_PACKET_DIR"):
            if forbidden in command.env:
                fail(f"baseline command for {harness} inherited {forbidden}")
        if model not in command.command:
            fail(f"same model {model!r} not present in {harness} command")
    notes.append(f"four harness commands share same model={model}")
    return notes


def check_source_hooks() -> list[str]:
    notes: list[str] = []
    run_cli = (REPO_ROOT / "poc_generation" / "poc_generator" / "run_cli_sample.py").read_text(encoding="utf-8", errors="replace")
    if "render_poc_task_prompt" not in run_cli:
        fail("run_cli_sample.py does not render the shared OpenHands prompt contract")
    openhands_prompt = (
        REPO_ROOT
        / "poc_generation"
        / "poc_generator"
        / "openhands_backend"
        / "template"
        / "prompt.txt"
    )
    if not openhands_prompt.is_file():
        fail(f"missing OpenHands canonical prompt: {openhands_prompt}")
    prompt_text = openhands_prompt.read_text(encoding="utf-8", errors="replace")
    if "analysis.json" not in prompt_text or "fine_trace" not in prompt_text:
        fail("OpenHands canonical prompt missing analysis contract terms")
    plugin = (REPO_ROOT / "poc_generation" / "poc_generator" / "poc_plugin.py").read_text(encoding="utf-8", errors="replace")
    if "build_poc_harness_command" not in plugin:
        fail("poc_plugin.py does not delegate to adapter registry")
    notes.append("source hooks use shared adapter registry and OpenHands prompt contract")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4-mini-2026-03-17")
    args = parser.parse_args(argv)
    notes: list[str] = []
    notes.extend(check_prompt_contract())
    notes.extend(check_analysis_contract())
    notes.extend(check_adapter_commands(args.model))
    notes.extend(check_source_hooks())
    print(json.dumps({"status": "pass", "notes": notes}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, indent=2, ensure_ascii=False))
        raise
