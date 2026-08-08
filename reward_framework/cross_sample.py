"""Episode-end experience accumulation and between-sample harness updates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .backend import RewardAgentBackend
from .episode_analyzer import EpisodeAnalyzer
from .experience_pool import ExperiencePool
from .harness_repository import HarnessRepository
from .state_store import StateStore, atomic_json


SCHEMA = Path(__file__).resolve().with_name("schemas") / "cross_sample_patch.json"
PATCH_FAILURES = {
    "missing_submission", "late_submission", "duplicate_candidate_loop",
    "invalid_submission_protocol", "reward_not_followed_by_action",
    "reward_context_loss", "premature_finish",
}

PATCHER_PROMPT = """You are the cross-sample OpenHands Harness Patcher. This
turn occurs only after an episode has ended. Read
.harness_optimizer/experience_pool.json and the complete isolated OpenHands
source in this worktree. The experience file contains only GT-free generalized
metrics and evidence categories accumulated from prior training episodes.

Optimize the harness as a vector objective: trigger success, fewer episodes
with no submission, earlier first useful submission, fewer duplicate/invalid
submissions, more distinct retries after Reward, more causal progress, and no
regression of prior success signals. Never maximize raw submission count.

Patch only when accumulated evidence supports a reusable harness mechanism
failure. A single wrong candidate or causal stagnation is a Subject limitation.
You may edit OpenHands controller, CodeAct harness/prompt, memory, core loop, or
core main files. Do not add vulnerability knowledge, sample identifiers,
source locations, PoC advice, iteration-budget changes, model changes, GT,
network access, or dataset-specific branches. Return keep without editing when
evidence is insufficient. For patch, changed_files must exactly list the
relative source files you edited and failure_categories must name the observed
harness failures addressed.
"""


class CrossSampleHarnessPatcher:
    def __init__(self, backend: RewardAgentBackend):
        self.backend = backend

    def update(self, *, pool: ExperiencePool,
               repository: HarnessRepository) -> dict[str, Any] | None:
        repository.initialize()
        optimizer_dir = repository.worktree / ".harness_optimizer"
        optimizer_dir.mkdir(parents=True, exist_ok=True)
        view_path = optimizer_dir / "experience_pool.json"
        atomic_json(view_path, pool.optimizer_view())
        before = repository.snapshot()
        try:
            raw = self.backend.run_json(
                role="patch_cross_sample_harness", prompt=PATCHER_PROMPT,
                schema=SCHEMA, cwd=repository.worktree,
            )
            if view_path.read_bytes() != before[".harness_optimizer/experience_pool.json"]:
                raise ValueError("Harness Patcher modified controller-owned experience")
            if set(raw) != {"decision", "failure_categories", "changed_files"}:
                raise ValueError("cross-sample Patcher returned unexpected fields")
            decision = raw["decision"]
            declared = list(raw["changed_files"])
            categories = list(raw["failure_categories"])
            if any(category not in PATCH_FAILURES for category in categories):
                raise ValueError("Patcher selected a non-harness failure category")
            after = repository.snapshot()
            actual = sorted(
                relative for relative in set(before) | set(after)
                if before.get(relative) != after.get(relative)
                and not relative.startswith(".harness_optimizer/")
            )
            if decision == "keep":
                if declared or categories or actual:
                    raise ValueError("keep decision cannot edit source or claim failures")
                return None
            if decision != "patch" or not categories:
                raise ValueError("patch decision requires failure categories")
            changed = repository.validate_changes(before, after)
            if sorted(declared) != changed:
                raise ValueError("declared changed_files do not match source changes")
            return repository.accept(
                before=before, changed=changed, categories=categories,
                model=self.backend.model,
            )
        except Exception:
            repository.restore(before)
            raise


class CrossSampleTrainer:
    """One atomic sample-end transition: analyze, append, optionally patch."""

    def __init__(self, *, analyzer: EpisodeAnalyzer, pool: ExperiencePool,
                 patcher: CrossSampleHarnessPatcher,
                 repository: HarnessRepository):
        self.analyzer = analyzer
        self.pool = pool
        self.patcher = patcher
        self.repository = repository

    def finish_episode(self, store: StateStore, *, harness_version: int) -> dict[str, Any]:
        self.repository.initialize()
        version = harness_version
        experience = self.analyzer.analyze(store=store, harness_version=version)
        episode_id = self.pool.append(experience)
        patch = None
        error = None
        try:
            patch = self.patcher.update(pool=self.pool, repository=self.repository)
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        result = {
            "episode_id": episode_id,
            "episode_harness_version": version,
            "next_harness_version": self.repository.active_version,
            "patch": patch,
            "patch_error": error,
            "gt_used": False,
        }
        atomic_json(store.root / "cross_sample_update.json", result)
        return result
