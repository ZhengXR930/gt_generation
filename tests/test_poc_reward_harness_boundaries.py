import ast
import hashlib
from pathlib import Path

from poc_generation.adapters import HARNESSES as POC_HARNESSES
from reward_framework import run_harness as reward_run_harness
from reward_framework.adapters import HARNESSES as REWARD_HARNESSES


ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_baseline_and_reward_expose_the_same_four_harnesses():
    assert POC_HARNESSES == REWARD_HARNESSES == (
        "openhands",
        "codex",
        "claude",
        "deepseek_harness",
    )


def test_frontend_prompts_have_identical_content():
    baseline = ROOT / "poc_generation" / "prompt.txt"
    reward = ROOT / "reward_framework" / "prompt.txt"
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == hashlib.sha256(
        reward.read_bytes()
    ).hexdigest()


def test_poc_generation_frontend_does_not_import_reward_framework():
    source_roots = [
        ROOT / "poc_generation" / "run_harness.py",
        *sorted((ROOT / "poc_generation" / "adapters").rglob("*.py")),
    ]
    offenders = {
        path.relative_to(ROOT).as_posix(): sorted(
            name for name in _imports(path) if name.startswith("reward_framework")
        )
        for path in source_roots
    }
    offenders = {path: imports for path, imports in offenders.items() if imports}
    assert offenders == {}


def test_reward_runner_supports_the_same_valid_gt_selectors(tmp_path, monkeypatch):
    valid_gt_dir = tmp_path / "gt_results"
    valid_gt_dir.mkdir()
    (valid_gt_dir / "valid_gt.json").write_text(
        '{"samples":["arvo_1","secbench_case","arvo_2"]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(reward_run_harness, "REPO_ROOT", tmp_path)

    class Args:
        sample = []
        samples_file = None
        sample_selector = "valid_gt_non_arvo"
        start_index = 0
        limit = 0

    assert reward_run_harness.load_samples(Args(), {}) == ["secbench_case"]


def test_reward_harness_adapters_use_neutral_runtime_only():
    adapter_paths = [
        ROOT / "reward_framework" / "run_harness.py",
        ROOT / "reward_framework" / "adapters" / "base.py",
        *sorted((ROOT / "reward_framework" / "adapters").glob("*/adapter.py")),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in adapter_paths)
    assert "poc_generation.poc_generator" not in text
    assert "reward_framework.adapters.poc_generation" not in text
    assert "harness_runtime" in text


def test_reward_skill_packet_uses_current_names():
    packet = ROOT / "reward_framework" / "offline_static_distillation" / "templates" / "skill_packet"
    assert (packet / "reproduction_skill" / "SKILL.md").is_file()
    assert (packet / "submission_skill" / "SKILL.md").is_file()
    assert not (packet / "level1_submission_verification").exists()
    assert not (packet / "level2_vulnerability_reproduction").exists()

    source_paths = [
        ROOT / "reward_framework" / "offline_static_distillation" / "cli.py",
        ROOT / "reward_framework" / "offline_static_distillation" / "README.md",
        ROOT / "reward_framework" / "adapters" / "agent_skill_export.py",
        ROOT / "reward_framework" / "adapters" / "openhands" / "contract.py",
        ROOT / "reward_framework" / "adapters" / "openhands" / "validate.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    for stale in (
        "level1_submission_verification",
        "level2_vulnerability_reproduction",
        "poc-submission-verification",
        "poc-vulnerability-reproduction",
        "audit_prompt",
        "build-audit",
    ):
        assert stale not in text
