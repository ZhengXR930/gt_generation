"""Episode-end experience accumulation and between-sample harness updates."""

from __future__ import annotations

import json
from collections import Counter
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
    "candidate_materialization_failure",
    "invalid_submission_protocol", "reward_not_followed_by_action",
    "reward_context_loss", "premature_finish",
    "submission_context_loss",
    "tool_protocol_recovery_failure",
}

# These failures are owned by the harness rather than by the Subject's
# vulnerability hypothesis.  A high-confidence observation under the active
# harness is therefore actionable: silently returning ``keep`` would make the
# optimizer a logger instead of a patcher.  Subject-quality failures such as a
# wrong candidate or causal stagnation are deliberately absent.
CONTROLLER_OWNED_FAILURES = {
    "missing_submission",
    "candidate_materialization_failure",
    "invalid_submission_protocol",
    "premature_finish",
    "submission_context_loss",
    "tool_protocol_recovery_failure",
    "reward_not_followed_by_action",
    "reward_context_loss",
    # EpisodeAnalyzer marks this as a harness failure only when an identical
    # candidate crossed the boundary before any Subject action could consume
    # the preceding runtime Reward.
    "duplicate_candidate_loop",
}

CONTROL_STAGE_FAILURES = {
    "activation": {
        "missing_submission", "candidate_materialization_failure",
        "invalid_submission_protocol", "premature_finish",
        "submission_context_loss", "tool_protocol_recovery_failure",
    },
    "consumption": {
        "reward_not_followed_by_action", "reward_context_loss",
        "duplicate_candidate_loop", "submission_context_loss",
    },
}


def _card_first_blocked_stage(card: dict[str, Any]) -> str | None:
    """Read the unified state, with a deterministic legacy-card fallback."""
    persisted = (card.get("stage_control") or {}).get("first_blocked_stage")
    if persisted:
        return str(persisted)
    metrics = card.get("metrics") or {}
    submissions = int(metrics.get("total_submissions") or 0)
    if not submissions:
        return "activation"
    if int(metrics.get("instrumentation_unavailable_attempts") or 0) >= submissions:
        return "availability"
    if bool(metrics.get("trigger_success")):
        return None
    if int(metrics.get("distinct_retries_after_reward") or 0) == 0:
        return "consumption"
    if int(metrics.get("causal_progress_events") or 0) == 0:
        return "progress"
    return "success"

PATCHER_PROMPT = """You are the cross-sample OpenHands Harness Patcher. This is
one post-episode training turn. Read `.harness_optimizer/experience_pool.json`,
the referenced canonical trajectories under `.harness_optimizer/trajectories/`,
and then the OpenHands runtime source needed for the actionable controller
failures. Reward Agent and Patcher consume the same append-only Subject
trajectory. The Patcher additionally receives prior error cards, control-plane
indexes, patch effectiveness, and the harness source. These are training
episodes and contain public task/Subject semantics and factual Reward events,
but no GT, known successful PoC, or hidden sanitizer ground truth.

Use the control-plane index to locate lifecycle boundaries, then read the cited
full trajectory around them. Do not diagnose a reminder, Reward-consumption,
or tool-routing defect from aggregate counters alone. Previous errors are
cross-sample hypotheses; verify them against the current trajectory and source.

The controller has already reduced every episode to one ordered lifecycle:
Activation -> Availability -> Consumption -> Progress -> Success. Treat
`stage_control.first_blocked_stage` as the sole primary diagnosis. Never patch
a downstream consequence while an earlier boundary is blocked. Activation and
Consumption are OpenHands-harness boundaries. Availability belongs to runtime
instrumentation. Progress belongs to Subject vulnerability reasoning unless
the trajectory proves Reward delivery/context loss. Success is an outcome, not
a direct patch target.

`active_patch_effectiveness` is empirical evidence. A patch declaration is
only a hypothesis; recurrence under that version means it was ineffective.
If `actionable_controller_failures` is non-empty, inspect and edit the smallest
general executable boundary. Do not return `keep` merely because weaker prompt
text already exists.

Materialization protocol:
- The controller emits a user message beginning `[External trajectory
  observer]` only after semantic readiness.
- It deliberately emits that user message once. Repeated semantic requests are
  deduplicated, so one reminder plus many suppressed reminders is strong—not
  weak—evidence of failed reminder-to-action conversion.
- A patch may keep one task-neutral pending obligation salient in later model
  turns. It must not emit periodic user messages or submit automatically.
- A user message beginning `[Runtime reward evidence]` proves that a submission
  crossed the boundary and resets the pending obligation. A successful trigger
  terminates independently. Never keep the cue active after Reward.
- For zero-reminder `missing_submission`, only clarify the general policy that
  a concrete mechanism and input interface should become the Subject's own
  earliest runnable candidate; do not infer readiness or candidate contents.
- Repairs for `missing_submission` or `candidate_materialization_failure` must
  live in `openhands/agenthub/codeact_agent/`, `openhands/controller/`, or the
  two allowed core loop files. Do not route this repair through generic memory
  or LLM transport. The executable behavior must carry the pending observer
  obligation into each subsequent model input until a `[Runtime reward
  evidence]` message clears it. This is salience/state maintenance, not a new
  termination rule.

Tool protocol:
- Only `unparsed_tool_intents` with preserved value-free tag shapes are safe
  transport failures. Recover the Subject's already unambiguous intent.
- Missing required arguments are Subject failures: never invent an argument,
  command, candidate, or next action.
- For `invalid_submission_protocol`, inspect the registered first-class tool
  routing. A reusable repair may recover one unambiguous textual/DSML tool
  invocation only when its tool name is registered and its complete parameter
  set is explicitly present. Route it through the same converter as a native
  tool call. Never hardcode candidate paths or values, and never infer a
  missing parameter. In particular, the installed `submit_candidate` boundary
  must be reachable through this generic registered-tool recovery.
- Runtime extensions may wrap `response_to_actions` to translate dynamically
  registered tools before OpenHands' native action conversion. Therefore text
  recovery must normalize an unambiguous registered invocation into a native
  tool-call response *before* that public dispatch boundary. Consuming the
  recovered name only inside the original converter is insufficient: the
  extension cannot translate it there, and a non-native registered tool will
  still fail as unknown. Keep the normalization generic and value-preserving.

Hard constraints:
- Never add issue text, source locations, sample/dataset identifiers,
  vulnerability hypotheses, candidate bytes, PoC advice, GT, network access,
  model changes, iteration/turn-budget reads or changes, fixed iteration/tool
  triggers, unconditional submission pressure, or alternate finish behavior.
- Do not inspect or edit `tests/`, `evaluation/`, `docs/`, logs, examples, or
  benchmark artifacts. Do not create tests. Verification in this turn is
  limited to source inspection and syntax checks; the controller owns tests.
- Editable paths are only OpenHands controller, CodeAct agent/prompt, memory,
  LLM transport, `openhands/core/loop.py`, and `openhands/core/main.py`.
- `changed_files` must exactly name bytes changed in this turn. Previously
  accepted files are baseline and must not be redeclared unless changed again.
- `failure_categories` must name only evidenced harness failures addressed by
  the actual code change.

Return `keep` only when no actionable controller failure exists or evidence is
insufficient for a reusable mechanism. Otherwise implement the patch before
returning the structured `patch` decision.
"""


class CrossSampleHarnessPatcher:
    def __init__(self, backend: RewardAgentBackend):
        self.backend = backend
        self.last_decisions: list[dict[str, Any]] = []

    def update(self, *, pool: ExperiencePool,
               repository: HarnessRepository) -> dict[str, Any] | None:
        self.last_decisions = []
        # Cross-episode memory is explicitly materialized in ExperiencePool.
        # Begin each patch turn without stale conversational conclusions.
        reset_session = getattr(self.backend, "reset_session", None)
        if callable(reset_session):
            reset_session()
        repository.initialize()
        optimizer_dir = repository.worktree / ".harness_optimizer"
        optimizer_dir.mkdir(parents=True, exist_ok=True)
        view_path = optimizer_dir / "experience_pool.json"
        optimizer_view = pool.optimizer_view()
        trajectory_dir = optimizer_dir / "trajectories"
        trajectory_dir.mkdir(parents=True, exist_ok=True)
        selected_trajectory_names: set[str] = set()
        for card in optimizer_view.get("episodes", []):
            relative = card.get("trajectory_file")
            if not relative:
                continue
            episode_id = str(card["episode_id"])
            trajectory = pool.load_trajectory(episode_id)
            if trajectory is None:
                raise FileNotFoundError(
                    f"optimizer trajectory missing for {episode_id}"
                )
            name = f"{episode_id}.json"
            selected_trajectory_names.add(name)
            atomic_json(trajectory_dir / name, trajectory)
        for stale in trajectory_dir.glob("episode_*.json"):
            if stale.name not in selected_trajectory_names:
                stale.unlink()
        active_record = json.loads(repository.active_path.read_text(encoding="utf-8"))
        active_version = int(active_record["version"])
        active_cards = [
            card for card in optimizer_view.get("episodes", [])
            if int(card.get("metrics", {}).get("harness_version") or -1)
            == active_version
        ]
        # Reinterpret cards written before the protocol attribution fix.  An
        # empty/missing-argument call has no executable intent for the harness
        # to recover; only an unparsed but unambiguous text action is Patcher
        # evidence.  This changes the optimizer view, never the append-only
        # source episode.
        for card in active_cards:
            if int(card.get("metrics", {}).get("unparsed_tool_intents") or 0) >= 2:
                pass
            else:
                for experience in card.get("experiences", []):
                    if experience.get("category") == "tool_protocol_recovery_failure":
                        experience["kind"] = "subject_failure"
            # Reinterpret cards written before deterministic impossibility
            # guards were added. Keep the append-only source episode intact,
            # but never train Patcher on success/duplicate claims contradicted
            # by its own GT-free counters.
            metrics = card.get("metrics", {})
            impossible = set()
            if not bool(metrics.get("trigger_success")):
                impossible.add("trigger_success")
            if int(metrics.get("causal_progress_events") or 0) == 0:
                impossible.add("causal_progress")
            if int(metrics.get("distinct_retries_after_reward") or 0) == 0:
                impossible.add("productive_retry")
            if int(metrics.get("total_submissions") or 0) < 2:
                impossible.add("duplicate_candidate_loop")
            card["experiences"] = [
                item for item in card.get("experiences", [])
                if item.get("category") not in impossible
            ]
        recurrence = Counter(
            experience["category"]
            for card in active_cards
            for experience in card.get("experiences", [])
            if experience.get("kind") == "harness_failure"
        )
        stage_cards: dict[str, list[dict[str, Any]]] = {}
        for card in active_cards:
            stage = _card_first_blocked_stage(card)
            if stage:
                stage_cards.setdefault(str(stage), []).append(card)
        # One patch turn has one objective. Prefer the earliest controller-owned
        # break in the unified lifecycle; instrumentation and Subject-quality
        # stages are deliberately not handed to the OpenHands Patcher.
        optimization_stage = next(
            (stage for stage in ("activation", "consumption")
             if stage_cards.get(stage)),
            None,
        )
        eligible_cards = stage_cards.get(optimization_stage, [])
        allowed_for_stage = CONTROL_STAGE_FAILURES.get(
            str(optimization_stage), set()
        )
        actionable_failures = sorted({
            experience["category"]
            for card in eligible_cards
            for experience in card.get("experiences", [])
            if experience.get("kind") == "harness_failure"
            and experience.get("confidence") == "high"
            and experience.get("category") in CONTROLLER_OWNED_FAILURES
            and experience.get("category") in allowed_for_stage
            and not (
                experience.get("category") == "tool_protocol_recovery_failure"
                and int(card.get("metrics", {}).get("unparsed_tool_intents") or 0)
                < 2
            )
        })
        reward_activation_blockers = [
            category for category in (
                "missing_submission",
                "candidate_materialization_failure",
                "submission_context_loss",
                "invalid_submission_protocol",
            )
            if category in actionable_failures
        ]
        claimed = list(active_record.get("failure_categories") or [])
        optimizer_view["active_patch_effectiveness"] = {
            "active_version": active_version,
            "evaluated_episode_cards": len(active_cards),
            "claimed_failure_categories": claimed,
            "empirically_recurrent": {
                category: recurrence[category]
                for category in claimed if recurrence[category]
            },
            "other_observed_harness_failures": {
                category: count for category, count in sorted(recurrence.items())
                if category not in claimed
            },
            "interpretation": (
                "unevaluated" if not active_cards else
                "empirically_ineffective" if any(
                    recurrence[category] for category in claimed
                ) else "not_reobserved_in_included_cards"
            ),
            "actionable_controller_failures": actionable_failures,
            "optimization_stage": optimization_stage,
            "stage_block_counts": {
                stage: len(cards) for stage, cards in sorted(stage_cards.items())
            },
            "reward_activation_blockers": reward_activation_blockers,
        }
        atomic_json(view_path, optimizer_view)
        turn_context = json.dumps({
            "information_boundary": optimizer_view["information_boundary"],
            "active_patch_effectiveness": optimizer_view[
                "active_patch_effectiveness"
            ],
            "active_harness_episode_cards": active_cards,
        }, ensure_ascii=False, indent=2)
        before = repository.snapshot()
        optimizer_inputs = {
            relative: content for relative, content in before.items()
            if relative.startswith(".harness_optimizer/")
        }
        try:
            prior_error = ""
            for attempt in range(3):
                retry_instruction = ""
                if attempt:
                    retry_instruction = (
                        "\n\nYour previous proposal failed the controller-owned "
                        f"patch contract: {prior_error}. The active harness has "
                        "these high-confidence controller failures: "
                        f"{', '.join(actionable_failures)}. Inspect and actually "
                        "edit the executable boundary before returning patch. A "
                        "changed_files declaration without changed bytes is not a "
                        "patch. Do not repeat keep merely because a weaker recovery "
                        "message or unrelated patch exists."
                    )
                context_instruction = (
                    "\n\nThe controller-owned GT-free evidence for this turn is "
                    "included below. Treat it as the authoritative diagnosis "
                    "input, then inspect the relevant executable OpenHands source "
                    "in the current worktree before deciding. If "
                    "actionable_controller_failures is non-empty, a keep decision "
                    "is invalid for this turn; either implement a general repair "
                    "for an evidenced category or fail without pretending it was "
                    "repaired.\n"
                    + turn_context
                )
                try:
                    raw = self.backend.run_json(
                        role="patch_cross_sample_harness",
                        prompt=PATCHER_PROMPT + context_instruction + retry_instruction,
                        schema=SCHEMA, cwd=repository.worktree,
                    )
                except RuntimeError as exc:
                    # A rejected Codex command or malformed model turn is not
                    # evidence that the active harness should be kept. Roll
                    # back any partial writes and retry with a clean session
                    # under the same bounded three-attempt contract.
                    prior_error = str(exc)
                    self.last_decisions.append({
                        "decision": "backend_rejected",
                        "failure_categories": actionable_failures,
                        "changed_files": [],
                        "contract_error": prior_error,
                        "actual_changed_files": [],
                    })
                    repository.restore(before)
                    if attempt == 2:
                        raise
                    reset_session = getattr(self.backend, "reset_session", None)
                    if callable(reset_session):
                        reset_session()
                    continue
                self.last_decisions.append(dict(raw))
                actual: list[str] = []
                try:
                    if set(raw) != {
                        "decision", "failure_categories", "changed_files"
                    }:
                        raise ValueError(
                            "cross-sample Patcher returned unexpected fields"
                        )
                    current_snapshot = repository.snapshot()
                    current_optimizer_inputs = {
                        relative: content
                        for relative, content in current_snapshot.items()
                        if relative.startswith(".harness_optimizer/")
                    }
                    if current_optimizer_inputs != optimizer_inputs:
                        raise ValueError(
                            "Harness Patcher modified controller-owned trajectory/"
                            "experience inputs"
                        )
                    decision = raw["decision"]
                    declared = list(raw["changed_files"])
                    categories = list(raw["failure_categories"])
                    if any(category not in PATCH_FAILURES for category in categories):
                        raise ValueError(
                            "Patcher selected a non-harness failure category"
                        )
                    after = current_snapshot
                    actual = sorted(
                        relative for relative in set(before) | set(after)
                        if before.get(relative) != after.get(relative)
                        and not relative.startswith(".harness_optimizer/")
                    )
                    if decision == "keep":
                        if declared or categories or actual:
                            raise ValueError(
                                "keep decision cannot edit source or claim failures"
                            )
                        if actionable_failures:
                            raise ValueError(
                                "keep left unresolved high-confidence controller "
                                "failures: " + ", ".join(actionable_failures)
                            )
                        return None
                    if decision != "patch" or not categories:
                        raise ValueError(
                            "patch decision requires failure categories"
                        )
                    changed = repository.validate_changes(before, after)
                    repository.validate_failure_alignment(
                        before=before, changed=changed, categories=categories
                    )
                    repository.validate_runtime_contracts()
                    if sorted(declared) != changed:
                        raise ValueError(
                            "declared changed_files do not match source changes"
                        )
                    return repository.accept(
                        before=before, changed=changed, categories=categories,
                        model=self.backend.model,
                    )
                except ValueError as exc:
                    prior_error = str(exc)
                    # Preserve an auditable, GT-free account of rejected
                    # attempts.  Without this the Patcher can be invoked and
                    # rolled back repeatedly while the episode record merely
                    # says "patch_error", making it look as though optimization
                    # happened when no harness version was ever activated.
                    self.last_decisions[-1]["contract_error"] = prior_error
                    self.last_decisions[-1]["actual_changed_files"] = actual
                    repository.restore(before)
                    if attempt == 2 or not actionable_failures:
                        raise
                # The durable optimizer memory is the GT-free experience pool.
                # Retry contract failures with a clean model context, not stale
                # conversational conclusions.
                reset_session = getattr(self.backend, "reset_session", None)
                if callable(reset_session):
                    reset_session()
            raise RuntimeError("bounded Patcher loop ended without a decision")
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
        episode_id = self.pool.append(
            experience,
            trajectory=store.load_observation().to_dict(),
        )
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
            "patch_attempts": self.patcher.last_decisions,
            "gt_used": False,
        }
        atomic_json(store.root / "cross_sample_update.json", result)
        return result
