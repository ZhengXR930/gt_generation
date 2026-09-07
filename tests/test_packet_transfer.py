from __future__ import annotations

"""Every adapter must deliver the same packet to its agent.

A distilled packet is only comparable across harnesses if the lessons the agent
reads are identical everywhere and the submit interface behaves the same. These
tests install the real initial packet with each real adapter and check both.
"""
from pathlib import Path

import pytest

from reward_framework.adapters.base import SKILL_PACKET_ENV, SKILL_PATH_PLACEHOLDERS
from reward_framework.adapters.claude.install import install_workspace_skill_packet as claude_install
from reward_framework.adapters.codex.install import install_workspace_skill_packet as codex_install
from reward_framework.adapters.conformance import check_installed_packet
from reward_framework.adapters.deepseek_harness.install import install_workspace_skill_packet as dsh_install
from reward_framework.adapters.openhands.install import install_workspace_skill_packet as openhands_install
from reward_framework.distillation.defaults import INITIAL_PACKET
from harness_runtime.workspace import render_prompt

ADAPTERS = {
    "openhands": openhands_install,
    "codex": codex_install,
    "claude": claude_install,
    "deepseek_harness": dsh_install,
}
PROMPT_FILE = Path("reward_framework/prompt.txt")


def _install(name, installer, tmp_path):
    workspace = tmp_path / name / "workspace"
    scratch = tmp_path / name / "scratch"
    workspace.mkdir(parents=True)
    scratch.mkdir(parents=True)
    # A staged benchmark workspace always carries the target's own submit.sh.
    (workspace / "submit.sh").write_text("#!/bin/bash\necho benchmark submit\n", encoding="utf-8")
    env = {SKILL_PACKET_ENV: str(INITIAL_PACKET)}
    metadata = installer(harness=name, workspace=workspace, sample_id="arvo_1", scratch=scratch, env=env)
    return workspace, metadata


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_adapter_delivers_the_packet_intact(name, tmp_path):
    workspace, metadata = _install(name, ADAPTERS[name], tmp_path)
    assert metadata and metadata.get("agent_paths"), f"{name} does not report agent_paths"
    errors = check_installed_packet(INITIAL_PACKET, metadata["agent_paths"], workspace=workspace)
    assert errors == [], f"{name}: {errors}"


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_adapter_leaves_submit_sh_alone(name, tmp_path):
    # Wrapping submit.sh changes the exit codes the agent sees, which breaks
    # comparability with the other harnesses and with the poc_generation baseline.
    workspace, _ = _install(name, ADAPTERS[name], tmp_path)
    assert (workspace / "submit.sh").read_text(encoding="utf-8") == "#!/bin/bash\necho benchmark submit\n"
    assert not (workspace / ".cybergym_submit.sh").exists()


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_adapter_reports_locations_that_exist(name, tmp_path):
    # The packet is delivered by each harness's own mechanism, so the task prompt
    # names nothing; what has to be true is that the locations the adapter reports
    # are real, because the skill text was rewritten to point at them.
    _, metadata = _install(name, ADAPTERS[name], tmp_path)
    for key in ("skill_packet", "helpers", "workspace"):
        assert Path(metadata["agent_paths"][key]).exists(), f"{name}: agent_paths.{key} does not exist"


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_task_prompt_is_untouched_by_installation(name, tmp_path):
    workspace, metadata = _install(name, ADAPTERS[name], tmp_path)
    rendered = render_prompt(PROMPT_FILE, sample_id="arvo_1", workspace=workspace)
    leftover = [token for token in SKILL_PATH_PLACEHOLDERS if token in rendered]
    assert leftover == [], f"{name}: the task prompt should carry no placeholders, found {leftover}"


def test_all_adapters_agree_on_the_lesson_text(tmp_path):
    # The point of the whole exercise: one distilled artifact, four harnesses.
    from reward_framework.adapters.conformance import _find_installed_skill, bullet_lines

    seen = {}
    for name, installer in sorted(ADAPTERS.items()):
        _, metadata = _install(name, installer, tmp_path)
        root = Path(metadata["agent_paths"]["skill_packet"])
        seen[name] = {
            relative: bullet_lines(_find_installed_skill(root, relative).read_text(encoding="utf-8"))
            for relative in ("reproduction_skill/SKILL.md", "submission_skill/SKILL.md")
        }
    reference = seen["codex"]
    for name, lessons in seen.items():
        assert lessons == reference, f"{name} delivers different lesson text than codex"
    assert reference["reproduction_skill/SKILL.md"], "sanity: lessons were actually found"


def _documented_helper_commands(skill_text: str) -> list[str]:
    """The `python3 ...` lines the installed submission skill tells the agent to run."""
    return [line.strip() for line in skill_text.splitlines() if line.strip().startswith("python3 ")]


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_documented_helper_commands_actually_run(name, tmp_path):
    """The commands printed in the skill must work at the paths the adapter chose.

    This is the check that would have caught a submit wrapper calling the helpers
    with flags they never accepted: documentation and executable have to agree,
    on every harness.
    """
    import subprocess

    from reward_framework.adapters.conformance import _find_installed_skill

    workspace, metadata = _install(name, ADAPTERS[name], tmp_path)
    root = Path(metadata["agent_paths"]["skill_packet"])
    skill = _find_installed_skill(root, "submission_skill/SKILL.md")
    assert skill is not None, f"{name}: submission skill not installed"

    poc = workspace / "candidate.bin"
    poc.write_bytes(b"\x00candidate bytes\n")
    (workspace / "analysis.json").write_text('{"sample_id": "arvo_1"}\n', encoding="utf-8")
    result_json = workspace / "result.json"
    result_json.write_text('{"attempt_id": "1", "exit_code": 0}\n', encoding="utf-8")

    commands = _documented_helper_commands(skill.read_text(encoding="utf-8"))
    assert len(commands) >= 3, f"{name}: expected the three documented helper commands, got {commands}"

    for command in commands:
        filled = (command
                  .replace("<poc>", str(poc))
                  .replace("<result.json>", str(result_json))
                  .replace("<status>", "evaluated")
                  .replace("<short-note>", "smoke"))
        assert "${" not in filled, f"{name}: unresolved placeholder in {filled}"
        proc = subprocess.run(filled, shell=True, cwd=workspace, capture_output=True, text=True)
        assert proc.returncode == 0, f"{name}: `{filled}` failed rc={proc.returncode}\n{proc.stderr}"

    state = Path(metadata["agent_paths"]["state"])
    assert (state / "submit_history.jsonl").is_file(), f"{name}: history was not recorded under {state}"


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_helpers_record_state_without_an_absolute_container_path(name, tmp_path):
    """With no explicit flag the helper must not reach for a container-only path."""
    import subprocess

    workspace, metadata = _install(name, ADAPTERS[name], tmp_path)
    helpers = Path(metadata["agent_paths"]["helpers"])
    poc = workspace / "candidate.bin"
    poc.write_bytes(b"bytes\n")

    proc = subprocess.run(
        ["python3", str(helpers / "submit_history.py"), "record",
         "--candidate", str(poc), "--status", "evaluated", "--note", "default-path"],
        cwd=workspace, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "REWARD_FRAMEWORK_STATE_DIR": ".poc_skill_state"},
    )
    assert proc.returncode == 0, f"{name}: {proc.stderr}"
    assert (workspace / ".poc_skill_state" / "submit_history.jsonl").is_file(), \
        f"{name}: history did not land beside the workspace"
