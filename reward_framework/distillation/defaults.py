"""Default settings for reward-framework skill distillation."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_GT = REPO_ROOT / "gt_results" / "valid_gt.json"
INITIAL_PACKET = REPO_ROOT / "reward_framework" / "skill_packets" / "initial"
DEFAULT_RUN_ROOT = REPO_ROOT / "reward_framework" / "distillation_runs"
DEFAULT_CODING_HARNESS = "deepseek_harness"
DEFAULT_CODING_MODEL = "deepseek-v4-flash"
DEFAULT_DISTILLER_MODEL = "deepseek-v4-flash"
DEFAULT_CODING_BASE_URL = ""
DEFAULT_CODING_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_DISTILLER_BASE_URL = "https://aidp.bytedance.net/api/modelhub/online/v2/crawl/openai/deployments/gpt_openapi"
DEFAULT_DISTILLER_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_BASE_URL = DEFAULT_DISTILLER_BASE_URL
DEFAULT_API_KEY_ENV = DEFAULT_DISTILLER_API_KEY_ENV
TRAIN_SIZE = 300
TEST_SIZE = 200
BATCH_SIZE = 10
