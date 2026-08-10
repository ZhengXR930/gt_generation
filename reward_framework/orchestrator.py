"""Unified deterministic controller around one persistent Reward Agent."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters.base import PlatformAdapter
from .assertion_planner import plan_assertions
from .assertion_reward import AssertionRewardSpec, assess_assertions
from .backend import RewardAgentBackend
from .feedback_agent import FeedbackAgent
from .models import (
    EvidenceRecord,
    EvidenceState,
    ObservationState,
    ProbePlan,
    RawRuntimeReport,
    RuntimeFact,
    TaskContext,
)
from .observer import TrajectoryObserver
from .probe_planner import ProbePlanner
from .runtime import InstrumentationBackend
from .source_view import refresh_agent_documents
from .spec_agent import SpecAgent
from .stage_evaluator import evaluate_stages
from .state_store import StateStore, atomic_json, utc_now
from .submission_tool import parse_submission

SUBMISSION_REQUEST = (
    "[External trajectory observer] The current workspace contains a runnable "
    "candidate or materially revised runnable hypothesis. Submit that exact "
    "candidate and its current fine trace through submit_candidate. Submission "
    "does not imply success; continue revising if runtime validation fails."
)

SUBMISSION_PREPARATION_REQUEST = (
    "[External trajectory observer] The current vulnerability hypothesis is mature "
    "enough for runtime checking, but no candidate-specific fine trace is present. "
    "Materialize the runnable candidate and /workspace/candidate_trace.json, then "
    "submit them through submit_candidate. The task-provided submission is the "
    "runtime-checking boundary; a separate local recreation of the target harness "
    "is not required before the first candidate. You may continue using tools while "
    "preparing those artifacts."
)


class RewardFramework:
    def __init__(self, *, store: StateStore, backend: RewardAgentBackend,
                 instrumentation: InstrumentationBackend,
                 platform: PlatformAdapter,
                 spec_cache_root: Path | None = None):
        self.store = store
        self.backend = backend
        self.instrumentation = instrumentation
        self.platform = platform
        self.agent_root = store.root / "agent_view"
        self.spec_agent = SpecAgent(backend, cache_root=spec_cache_root)
        self.observer = TrajectoryObserver(backend)
        self.probe_planner = ProbePlanner(backend)
        self.feedback_agent = FeedbackAgent(backend)

    @classmethod
    def create(cls, *, task_id: str, issue_description: str,
               codebase_root: Path, state_dir: Path,
               backend: RewardAgentBackend,
               instrumentation: InstrumentationBackend,
               platform: PlatformAdapter,
               baseline_profile: str = "adaptive_reward_v1",
               harness_version: int = 1,
               max_iterations: int = 100,
               spec_cache_root: Path | None = None) -> "RewardFramework":
        store = StateStore(state_dir)
        framework = cls(
            store=store, backend=backend,
            instrumentation=instrumentation, platform=platform,
            spec_cache_root=spec_cache_root,
        )
        if store.task_path.exists():
            raise FileExistsError(f"task state already exists: {store.task_path}")
        task = framework.spec_agent.initialize(
            task_id=task_id,
            issue_description=issue_description,
            codebase_root=codebase_root,
            agent_root=framework.agent_root,
        )
        store.save_task(task)
        store.initialize_harness(
            platform=getattr(platform, "platform_name", "generic"),
            baseline_profile=baseline_profile,
            max_iterations=max_iterations,
        )
        harness = store.load_harness_state()
        harness.active_program_version = harness_version
        store.save_harness_state(harness)
        store.save_evidence_state(EvidenceState())
        state = ObservationState()
        state.append(
            timestamp=utc_now(), source="controller", kind="task_initialized",
            payload={
                "task_id": task_id,
                "reward_spec_constructable": task.reward_spec.constructable,
                "baseline_profile": baseline_profile,
                "reward_protocol": getattr(task.reward_spec, "protocol", "legacy-five-stage"),
                "declared_claims": (
                    [item.assertion_id for item in task.reward_spec.assertions]
                    if isinstance(task.reward_spec, AssertionRewardSpec) else
                    [stage for stage, claim in task.reward_spec.claims.items() if claim]
                ),
            },
        )
        store.save_observation(state)
        framework._refresh_agent_view()
        return framework

    @classmethod
    def resume(cls, *, state_dir: Path, backend: RewardAgentBackend,
               instrumentation: InstrumentationBackend,
               platform: PlatformAdapter,
               baseline_profile: str = "adaptive_reward_v1",
               harness_version: int = 1,
               max_iterations: int = 100) -> "RewardFramework":
        """Resume a crash-safe episode without regenerating its frozen Spec."""
        store = StateStore(state_dir)
        if not store.task_path.is_file():
            raise FileNotFoundError(f"task state does not exist: {store.task_path}")
        framework = cls(
            store=store, backend=backend,
            instrumentation=instrumentation, platform=platform,
        )
        store.load_task()  # validate the persisted schema before installing hooks
        store.initialize_harness(
            platform=getattr(platform, "platform_name", "generic"),
            baseline_profile=baseline_profile,
            max_iterations=max_iterations,
        )
        harness = store.load_harness_state()
        harness.active_program_version = harness_version
        store.save_harness_state(harness)
        framework._refresh_agent_view()
        return framework

    def _public_task(self, task: TaskContext) -> dict[str, Any]:
        value = task.to_dict()
        value["codebase_root"] = "source/"
        return value

    def _refresh_agent_view(self, *, current_trace: Path | None = None,
                            prior_evidence: Path | None = None,
                            current_runtime: Path | None = None) -> None:
        task = self.store.load_task()
        atomic_json(self.agent_root / "task_context.json", self._public_task(task))
        refresh_agent_documents(self.agent_root, {
            "observation_state.json": self.store.observation_path,
            "trajectory_state.json": self.store.trajectory_path,
            "evidence_state.json": self.store.evidence_state_path,
            "harness_state.json": self.store.harness_state_path,
        })
        global_state = self.store.global_state()
        global_state["task"] = self._public_task(task)
        atomic_json(self.agent_root / "global_state.json", global_state)
        optional = {
            "current_trace.json": current_trace,
            "prior_evidence.json": prior_evidence,
            "current_runtime.json": current_runtime,
        }
        for name, source in optional.items():
            target = self.agent_root / name
            if source is None or not source.is_file():
                target.unlink(missing_ok=True)
            else:
                shutil.copy2(source, target)

    def record_event(self, *, source: str, kind: str,
                     payload: dict[str, Any]) -> ObservationState:
        state = self.store.append_event(source=source, kind=kind, payload=payload)
        self._refresh_agent_view()
        return state

    def record_iteration(self, *, iteration: int, maximum: int) -> None:
        state = self.store.load_observation()
        latest = next(
            (event for event in reversed(state.events)
             if event.kind == "controller_iteration"),
            None,
        )
        if latest is not None and int(latest.payload.get("iteration") or -1) == iteration:
            return
        state.append(
            timestamp=utc_now(), source="controller", kind="controller_iteration",
            payload={"iteration": iteration, "maximum": maximum},
        )
        self.store.save_observation(state)

    def observe_trajectory(self) -> str:
        task = self.store.load_task()
        state = self.store.load_observation()
        self._refresh_agent_view()
        submission_ready = False
        try:
            submission_ready = self.platform.submission_ready()
            current_fingerprint = (
                self.platform.submission_fingerprint()
                if submission_ready else None
            )
            latest_fingerprint = self.store.latest_candidate_sha256()
            # A pending materialization request does not suspend supervision.
            # The semantic observer already returns ``continue`` when no
            # material progress has occurred since the previous reminder, so
            # it can safely decide whether later trajectory evidence warrants
            # another reminder without fixed iteration/tool-count triggers.
            decision = (
                "request_submission"
                if submission_ready
                and (
                    latest_fingerprint is None
                    or current_fingerprint != latest_fingerprint
                )
                else "continue"
                if submission_ready
                else self.observer.decide(
                    task=task, state=state, agent_root=self.agent_root
                )
            )
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
            # A supervisor outage cannot block the subject agent.
            decision = "continue"
            state.append(
                timestamp=utc_now(), source="controller",
                kind="observer_unavailable",
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
        candidate_relation = (
            "absent" if not submission_ready
            else "first" if latest_fingerprint is None
            else "unchanged" if current_fingerprint == latest_fingerprint
            else "revised"
        )
        decision_event = state.append(
            timestamp=utc_now(), source="reward_agent",
            kind="observer_decision", payload={
                "decision": decision,
                "candidate_relation_to_last_submission": candidate_relation,
            },
        )
        state.last_observer_sequence = decision_event.sequence
        latest_sequence = state.events[-1].sequence if state.events else 0
        state.last_observer_sequence = latest_sequence
        if decision == "request_submission" and not submission_ready:
            if state.materialization_outstanding:
                # Supervision remains continuous, but one unresolved
                # materialization obligation must not produce repeated,
                # identical user interruptions. A successful submission resets
                # this flag, so later revised hypotheses can be cued again.
                pending = state.append(
                    timestamp=utc_now(), source="controller",
                    kind="observer_materialization_still_pending",
                    payload={},
                )
                state.last_observer_sequence = pending.sequence
            else:
                deferred = state.append(
                    timestamp=utc_now(), source="controller",
                    kind="observer_submission_deferred",
                    payload={
                        "reason": (
                            "candidate artifacts have not been materialized "
                            "in the workspace"
                        )
                    },
                )
                state.last_observer_sequence = deferred.sequence
                # This is deliberately non-blocking: the message makes the
                # observer's decision actionable, while the Subject can still
                # create or repair the candidate and trace before crossing the
                # hard submission gate.
                self.platform.inject_message(SUBMISSION_PREPARATION_REQUEST)
                state.materialization_outstanding = True
            decision = "continue"
        if decision == "request_submission" and not state.submission_requested:
            state.submission_requested = True
            event = state.append(
                timestamp=utc_now(), source="reward_agent",
                kind="submission_requested", payload={"message": SUBMISSION_REQUEST},
            )
            state.last_observer_sequence = event.sequence
            self.platform.inject_message(SUBMISSION_REQUEST)
        self.store.save_observation(state)
        self._refresh_agent_view()
        return decision

    @staticmethod
    def _validate_trace(path: Path) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, (list, dict)):
            raise ValueError("candidate fine trace must be a JSON array or object")

    @staticmethod
    def _runtime_dict(report: RawRuntimeReport, assessment: Any) -> dict[str, Any]:
        return {
            "exit_code": report.exit_code,
            "stdout": report.stdout,
            "stderr": report.stderr,
            "trigger_observed": report.trigger_observed,
            "instrumentation_available": report.instrumentation_available,
            "error": report.error,
            "stage_observations": {
                stage: status.value
                for stage, status in report.stage_observations.items()
            },
            "facts": [asdict(fact) for fact in report.facts],
            "claim_results": [
                item.to_dict() if hasattr(item, "to_dict") else asdict(item)
                for item in report.claim_results
            ],
            "assessment": assessment.to_dict(),
        }

    def submit_candidate(self, arguments: str | dict[str, Any]) -> dict[str, Any]:
        task = self.store.load_task()
        state = self.store.load_observation()
        if state.terminal_reason:
            raise RuntimeError(f"episode already terminated: {state.terminal_reason}")
        try:
            poc_path, trace_path = parse_submission(
                self.platform.workspace_root, arguments
            )
            self._validate_trace(trace_path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            state.append(
                timestamp=utc_now(), source="controller",
                kind="candidate_submission_rejected",
                payload={"error_type": type(exc).__name__},
            )
            self.store.save_observation(state)
            self._refresh_agent_view()
            raise
        checkpoint = self.platform.checkpoint(
            f"submission_{self.store.candidate_stats()['total_submissions'] + 1:04d}"
        )
        registered = self.store.register_candidate(
            poc_path=poc_path, trace_path=trace_path,
            checkpoint_path=checkpoint,
        )
        stored_poc = registered.candidate_dir / "poc"
        stored_trace = registered.attempt_dir / "trace.json"
        # Checkpointing and platform adapters may append lifecycle events.
        # Reload before the submission transaction so a stale pre-checkpoint
        # snapshot cannot erase those events or the registered submission.
        state = self.store.load_observation()
        state.append(
            timestamp=utc_now(), source="coding_agent", kind="candidate_submitted",
            payload={
                "candidate_id": registered.candidate_id,
                "candidate_sha256": registered.sha256,
                "attempt_number": registered.attempt_number,
                "duplicate_of": registered.duplicate_of,
                "checkpoint": str(checkpoint) if checkpoint else None,
            },
        )
        state.awaiting_verification = True
        state.submission_requested = False
        state.materialization_outstanding = False
        self.store.save_observation(state)

        previous = self.store.previous_distinct_evidence(registered.sha256)
        previous_path = None
        if previous is not None:
            previous_path = registered.attempt_dir / "prior_evidence.json"
            atomic_json(previous_path, previous)
        self._refresh_agent_view(
            current_trace=stored_trace, prior_evidence=previous_path
        )

        probe_error = None
        try:
            plan = (
                plan_assertions(task.reward_spec)
                if isinstance(task.reward_spec, AssertionRewardSpec)
                else self.probe_planner.design(agent_root=self.agent_root)
                if task.reward_spec.constructable
                else ProbePlan(())
            )
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
            # Candidate execution and trigger checking remain useful even when
            # the Reward Agent cannot produce a valid passive probe plan.
            plan = ProbePlan(())
            probe_error = f"{type(exc).__name__}: {exc}"
        runtime_dir = registered.attempt_dir / "runtime"
        try:
            report = self.instrumentation.verify(
                poc_path=stored_poc,
                trace_path=stored_trace,
                plan=plan,
                output_dir=runtime_dir,
            )
        except Exception as exc:  # platform backends are deliberately pluggable
            report = RawRuntimeReport(
                exit_code=None,
                stdout="",
                stderr="",
                trigger_observed=False,
                stage_observations={},
                facts=(RuntimeFact(
                    fact_id="RUNTIME-UNAVAILABLE", stage="trigger",
                    kind="runtime_unavailable",
                    statement="Runtime validation was unavailable for this submission.",
                    data={"error": f"{type(exc).__name__}: {exc}"},
                ),),
                instrumentation_available=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        if probe_error:
            report = RawRuntimeReport(
                exit_code=report.exit_code,
                stdout=report.stdout,
                stderr=report.stderr,
                trigger_observed=report.trigger_observed,
                stage_observations=report.stage_observations,
                facts=report.facts + (RuntimeFact(
                    fact_id="PROBE-PLAN-UNAVAILABLE", stage="trigger",
                    kind="probe_plan_unavailable",
                    statement="No source-valid passive probe plan was available for this submission.",
                    data={"error": probe_error},
                ),),
                instrumentation_available=report.instrumentation_available,
                error="; ".join(x for x in (report.error, probe_error) if x),
                claim_results=report.claim_results,
            )
        assessment = (
            assess_assertions(
                admission=(
                    report.stage_observations.get("admission").value
                    if report.stage_observations.get("admission") else "unresolved"
                ),
                results=report.claim_results,
                previous=previous,
                trigger_observed=report.trigger_observed,
            )
            if isinstance(task.reward_spec, AssertionRewardSpec)
            else evaluate_stages(task.reward_spec, report)
        )
        runtime_path = registered.attempt_dir / "current_runtime.json"
        atomic_json(runtime_path, self._runtime_dict(report, assessment))
        self._refresh_agent_view(
            current_trace=stored_trace,
            prior_evidence=previous_path,
            current_runtime=runtime_path,
        )
        feedback = self.feedback_agent.generate(
            report=report,
            assessment=assessment,
            previous=previous,
            agent_root=self.agent_root,
        )
        record = EvidenceRecord(
            candidate_id=registered.candidate_id,
            candidate_sha256=registered.sha256,
            attempt_number=registered.attempt_number,
            duplicate_of=registered.duplicate_of,
            probe_plan=plan,
            runtime=report,
            assessment=assessment,
            feedback=feedback,
            created_at=utc_now(),
        )
        evidence_path = self.store.save_evidence(record)

        state = self.store.load_observation()
        event = state.append(
            timestamp=utc_now(), source="reward_agent", kind="verification_completed",
            payload={
                "reward_event_id": f"reward_attempt_{registered.attempt_number:04d}",
                "attempt_number": registered.attempt_number,
                "candidate_id": registered.candidate_id,
                "trigger_observed": report.trigger_observed,
                "runtime_facts": [asdict(fact) for fact in report.facts],
                "assessment": assessment.to_dict(),
                "feedback": feedback.to_dict(),
                "evidence_path": str(evidence_path),
            },
        )
        state.awaiting_verification = False
        state.last_submission_sequence = event.sequence
        if report.trigger_observed:
            state.terminal_reason = "trigger_success"
        self.store.save_observation(state)
        self._refresh_agent_view(
            current_trace=stored_trace,
            prior_evidence=previous_path,
            current_runtime=runtime_path,
        )
        if not report.trigger_observed:
            self._refresh_agent_view(
                current_trace=stored_trace,
                prior_evidence=previous_path,
                current_runtime=runtime_path,
            )
            factual = feedback.summary
            if feedback.contradiction:
                factual += " " + feedback.contradiction
            self.platform.inject_message("[Runtime reward evidence] " + factual)
        response = {
            "candidate_id": registered.candidate_id,
            "attempt_number": registered.attempt_number,
            "duplicate_of": registered.duplicate_of,
            "triggered": report.trigger_observed,
            "assessment": assessment.to_dict(),
            "feedback": feedback.to_dict(),
            "candidate_stats": self.store.candidate_stats(),
        }
        return response

    def reach_iteration_limit(self, *, iteration: int, maximum: int,
                              notify: bool = True) -> bool:
        if iteration < maximum:
            return False
        state = self.store.load_observation()
        if state.terminal_reason is None:
            state.terminal_reason = "iteration_limit"
            state.append(
                timestamp=utc_now(), source="controller", kind="iteration_limit",
                payload={"iteration": iteration, "maximum": maximum},
            )
            self.store.save_observation(state)
            if notify:
                self.platform.inject_message(
                    "The iteration limit has been reached. Stop using tools and output "
                    "the final structured fine trace for the current hypothesis."
                )
        return True

    def before_finish(self, *, iteration: int, maximum: int) -> bool:
        """Enforce the two terminal conditions without freezing normal tools."""
        state = self.store.load_observation()
        if state.terminal_reason == "trigger_success":
            return True
        if iteration >= maximum:
            self.reach_iteration_limit(iteration=iteration, maximum=maximum)
            return True
        self.platform.inject_message(
            "The task has not triggered the vulnerability and has not reached "
            "its iteration limit. Continue investigating and submit runnable "
            "candidates through submit_candidate."
        )
        state.append(
            timestamp=utc_now(), source="controller", kind="early_finish_rejected",
            payload={"iteration": iteration, "maximum": maximum},
        )
        self.store.save_observation(state)
        return False

    def status(self) -> dict[str, Any]:
        state = self.store.load_observation()
        return {
            "task_id": self.store.load_task().task_id,
            "terminal_reason": state.terminal_reason,
            "awaiting_verification": state.awaiting_verification,
            "submission_requested": state.submission_requested,
            "materialization_outstanding": state.materialization_outstanding,
            "trajectory_events": len(state.events),
            "candidate_stats": self.store.candidate_stats(),
            "harness": self.store.load_harness_state().to_dict(),
        }
