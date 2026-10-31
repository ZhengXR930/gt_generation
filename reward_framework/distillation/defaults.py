"""Default settings for reward-framework skill distillation."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_GT = REPO_ROOT / "gt_results" / "valid_gt.json"
# The active packet is the two-runtime-skill scaffold.  The historical
# five-stage packet remains available by explicit path, but is never selected
# by an omitted packet argument.
INITIAL_PACKET = REPO_ROOT / "reward_framework" / "skill_packets" / "dsh_deepseek_initial_two_layer_staged_100_100"
DEFAULT_RUN_ROOT = REPO_ROOT / "reward_framework" / "distillation_runs"
# Canonical current split. It is updated in place from the latest unique
# baseline results; timestamped copies are not used to identify namespaces or
# controls.
DEFAULT_DSH_BASELINE_DIR = REPO_ROOT / "reward_framework" / "baselines" / "dsh_deepseek_failure_balanced100"
# Initial controls are resolved from each distillation batch's
# ``evaluation_initial_two_layer.json`` when present.  There is deliberately no
# legacy 100/100 results-directory fallback: silently comparing the current
# packet with the old complete-flow packet invalidates the paired experiment.
DEFAULT_INITIAL_TRAIN_RESULTS_DIR = None
DEFAULT_INITIAL_TEST_RESULTS_DIR = None
DEFAULT_NO_SKILL_RESULTS_DIR = (
    REPO_ROOT / "poc_generation" / "poc_results" / "deepseek-harness-v4-flash"
)
DEFAULT_DISTILLATION_SPLIT = (
    DEFAULT_DSH_BASELINE_DIR / "split_manifest.json"
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
