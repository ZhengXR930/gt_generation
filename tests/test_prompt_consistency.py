from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_poc_and_reward_prompts_are_identical() -> None:
    poc_prompt = (REPO_ROOT / "poc_generation" / "prompt.txt").read_text(
        encoding="utf-8"
    )
    reward_prompt = (REPO_ROOT / "reward_framework" / "prompt.txt").read_text(
        encoding="utf-8"
    )

    assert poc_prompt == reward_prompt


def test_universal_prompt_does_not_allow_analysis_only_exit() -> None:
    prompt = (REPO_ROOT / "poc_generation" / "prompt.txt").read_text(
        encoding="utf-8"
    )

    forbidden = (
        "If you never submit any",
        "final answer must be only the required analysis artifact JSON object",
    )
    for marker in forbidden:
        assert marker not in prompt


def test_universal_prompt_uses_dsh_readme_entry_shape() -> None:
    prompt = (REPO_ROOT / "poc_generation" / "prompt.txt").read_text(
        encoding="utf-8"
    )

    required = (
        "Read README.md first.",
        "Use the vulnerable source under repo-vul/src-vul.",
        "Submit every runnable candidate immediately with:",
        "bash ./submit.sh /path/to/poc ./analysis.json",
    )
    for marker in required:
        assert marker in prompt
