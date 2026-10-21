"""Default settings for reward-framework skill distillation."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_GT = REPO_ROOT / "gt_results" / "valid_gt.json"
INITIAL_PACKET = REPO_ROOT / "reward_framework" / "skill_packets" / "dsh_deepseek_initial_100_100"
DEFAULT_RUN_ROOT = REPO_ROOT / "reward_framework" / "distillation_runs"
DEFAULT_DSH_BASELINE_DIR = REPO_ROOT / "reward_framework" / "baselines" / "dsh_deepseek_failure_balanced100"
# Fixed DSH + DeepSeek controls used by the failure-balanced 100/100
# distillation experiment.  The train and test runs deliberately use
# different output-token budgets; the no-skill control is the formal
# poc_generation result set and covers the full 500-sample valid corpus.
DEFAULT_INITIAL_TRAIN_RESULTS_DIR = (
    REPO_ROOT / "reward_framework" / "harness_runs"
    / "dsh_deepseek_failure_balanced_train_initial_4096_high_20261016" / "results"
)
DEFAULT_INITIAL_TEST_RESULTS_DIR = (
    REPO_ROOT / "reward_framework" / "harness_runs"
    / "dsh_deepseek_official_v4flash_initial_32768_max_failure_balanced100_test_full_20261018"
    / "results"
)
DEFAULT_NO_SKILL_RESULTS_DIR = (
    REPO_ROOT / "poc_generation" / "poc_results" / "deepseek-harness-v4-flash"
)
DEFAULT_DISTILLATION_SPLIT = (
    REPO_ROOT / "reward_framework" / "experiment_splits"
    / "skill_distillation_100_100_failure_balanced_20261016" / "split_manifest.json"
)
DEFAULT_CODING_HARNESS = "deepseek_harness"
DEFAULT_CODING_MODEL = "deepseek-v4-flash"
DEFAULT_DISTILLER_MODEL = "deepseek-v4-flash"
DEFAULT_CODING_BASE_URL = ""
DEFAULT_CODING_API_KEY_ENV = "DEEPSEEK_AUTH_TOKEN"
DEFAULT_ROLE_HARNESS = "codex"
DEFAULT_DISTILLER_BASE_URL = "https://api.deepseek.com"
DEFAULT_DISTILLER_API_KEY_ENV = "DEEPSEEK_AUTH_TOKEN"
DEFAULT_BASE_URL = DEFAULT_DISTILLER_BASE_URL
DEFAULT_API_KEY_ENV = DEFAULT_DISTILLER_API_KEY_ENV
TRAIN_SIZE = 100
TEST_SIZE = 100
BATCH_SIZE = 10
