"""Unified deterministic controller around one persistent Reward Agent."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters.base import PlatformAdapter
from .backend import RewardAgentBackend
from .feedback_agent import FeedbackAgent
from .models import (
    EvidenceRecord,
    ObservationState,
    ProbePlan,
    RawRuntimeReport,
    RuntimeFact,
    StageStatus,
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


class RewardFramework:
    def __init__(self, *, store: StateStore, backend: RewardAgentBackend,
                 instrumentation: InstrumentationBackend,
                 platform: PlatformAdapter):
        self.store = store
        self.backend = backend
        self.instrumentation = instrumentation
        self.platform = platform
        self.agent_root = store.root / "agent_view"
        self.spec_agent = SpecAgent(backend)
        self.observer = TrajectoryObserver(backend)
        self.probe_planner = ProbePlanner(backend)
        self.feedback_agent = FeedbackAgent(backend)

    @classmethod
    def create(cls, *, task_id: str, issue_description: str,
               codebase_root: Path, state_dir: Path,
               backend: RewardAgentBackend,
               instrumentation: InstrumentationBackend,
               platform: PlatformAdapter) -> "RewardFramework":
        store = StateStore(state_dir)
        framework = cls(
            store=store, backend=backend,
            instrumentation=instrumentation, platform=platform,
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
        state = ObservationState()
        state.append(
            timestamp=utc_now(), source="controller", kind="task_initialized",
            payload={
                "task_id": task_id,
                "reward_spec_constructable": task.reward_spec.constructable,
                "declared_stages": [
                    stage for stage, claim in task.reward_spec.claims.items() if claim
                ],
            },
        )
        store.save_observation(state)
        framework._refresh_agent_view()
        return framework

    @classmethod
    def resume(cls, *, state_dir: Path, backend: RewardAgentBackend,
               instrumentation: InstrumentationBackend,
               platform: PlatformAdapter) -> "RewardFramework":
        """Resume a crash-safe episode without regenerating its frozen Spec."""
        store = StateStore(state_dir)
        if not store.task_path.is_file():
            raise FileNotFoundError(f"task state does not exist: {store.task_path}")
        framework = cls(
            store=store, backend=backend,
            instrumentation=instrumentation, platform=platform,
        )
        store.load_task()  # validate the persisted schema before installing hooks
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
        })
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

    def observe_trajectory(self) -> str:
        task = self.store.load_task()
        state = self.store.load_observation()
        self._refresh_agent_view()
        try:
            decision = self.observer.decide(
                task=task, state=state, agent_root=self.agent_root
            )
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
            # A supervisor outage cannot block the subject agent.
            decision = "continue"
            state.append(
                timestamp=utc_now(), source="controller",
                kind="observer_unavailable",
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
        latest_sequence = state.events[-1].sequence if state.events else 0
        state.last_observer_sequence = latest_sequence
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
            "assessment": assessment.to_dict(),
        }

    def submit_candidate(self, arguments: str | dict[str, Any]) -> dict[str, Any]:
        task = self.store.load_task()
        state = self.store.load_observation()
        if state.terminal_reason:
            raise RuntimeError(f"episode already terminated: {state.terminal_reason}")
        poc_path, trace_path = parse_submission(
            self.platform.workspace_root, arguments
        )
        self._validate_trace(trace_path)
        checkpoint = self.platform.checkpoint(
            f"submission_{self.store.candidate_stats()['total_submissions'] + 1:04d}"
        )
        registered = self.store.register_candidate(
            poc_path=poc_path, trace_path=trace_path,
            checkpoint_path=checkpoint,
        )
        stored_poc = registered.candidate_dir / "poc"
        stored_trace = registered.attempt_dir / "trace.json"
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
                self.probe_planner.design(agent_root=self.agent_root)
                if task.reward_spec.constructable else ProbePlan(())
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
            )
        assessment = evaluate_stages(task.reward_spec, report)
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
                "attempt_number": registered.attempt_number,
                "candidate_id": registered.candidate_id,
                "trigger_observed": report.trigger_observed,
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

    def reach_iteration_limit(self, *, iteration: int, maximum: int) -> bool:
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
            "trajectory_events": len(state.events),
            "candidate_stats": self.store.candidate_stats(),
        }
