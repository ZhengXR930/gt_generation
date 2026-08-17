"""Unified deterministic controller around one persistent Reward Agent."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters.base import PlatformAdapter
from .assertion_planner import plan_assertions
from .backend import RewardAgentBackend
from .feedback_agent import FeedbackAgent
from .models import (
    EvidenceRecord,
    EvidenceState,
    Feedback,
    ObservationState,
    RawRuntimeReport,
    RuntimeFact,
    TaskContext,
)
from .runtime import InstrumentationBackend
from .source_view import refresh_agent_documents
from .spec_agent import SpecAgent
from .stage_evaluator import evaluate_stages
from .state_store import StateStore, atomic_json, utc_now
from .submission_tool import parse_submission
from evaluator.reasoning.analysis_artifact import validate_analysis_artifact_quality


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
        self.feedback_agent = FeedbackAgent()

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
                "reward_protocol": "stage-reward-v1",
                "declared_claims": [item.claim_id for item in task.reward_spec.all_claims],
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

    def _refresh_agent_view(self, *, current_analysis: Path | None = None,
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
            "current_analysis.json": current_analysis,
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

    def _current_submission_bundle_fingerprint(self) -> str | None:
        method = getattr(self.platform, "submission_bundle_fingerprint", None)
        if callable(method):
            return method()
        return self.platform.submission_fingerprint()

    def auto_submission_needed(self, *, iteration: int, maximum: int) -> bool:
        """Return true once for each new workspace PoC+analysis bundle.

        This is a harness-level submit pressure mechanism. The Subject remains
        responsible for creating the PoC and its causal analysis; once both are
        materialized at the standard workspace paths, the controller submits the
        exact bundle instead of waiting for another model turn to voluntarily
        call the submit tool.
        """
        state = self.store.load_observation()
        if state.terminal_reason or state.awaiting_verification:
            return False
        try:
            if not self.platform.submission_ready():
                return False
            candidate_fingerprint = self.platform.submission_fingerprint()
            bundle_fingerprint = self._current_submission_bundle_fingerprint()
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
            state.append(
                timestamp=utc_now(), source="controller",
                kind="auto_submission_unavailable",
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
            self.store.save_observation(state)
            return False
        if not bundle_fingerprint:
            return False
        if bundle_fingerprint == self.store.latest_submission_bundle_sha256():
            return False
        if bundle_fingerprint == state.last_auto_submission_fingerprint:
            return False
        state.auto_submission_count += 1
        state.last_auto_submission_fingerprint = bundle_fingerprint
        state.submission_requested = True
        state.materialization_outstanding = False
        event = state.append(
            timestamp=utc_now(), source="controller",
            kind="auto_submission_scheduled",
            payload={
                "iteration": iteration,
                "maximum": maximum,
                "candidate_sha256": candidate_fingerprint,
                "bundle_sha256": bundle_fingerprint,
                "auto_submission_count": state.auto_submission_count,
            },
        )
        state.last_observer_sequence = event.sequence
        self.store.save_observation(state)
        self._refresh_agent_view()
        return True

    @staticmethod
    def _looks_like_candidate_conclusion(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", text or "").strip().lower()
        if not normalized:
            return False
        conclusion = re.search(
            r"\b("
            r"now i (?:understand|have|can see|know)|"
            r"the vulnerable path|"
            r"to trigger(?: this| the vulnerability)?|"
            r"this should trigger|"
            r"input (?:is|should be|that)|"
            r"payload (?:is|should be|that)|"
            r"candidate (?:is|should be|that)|"
            r"poc (?:is|should be|that)|"
            r"reproducer (?:is|should be|that)|"
            r"simply the (?:string|bytes)"
            r")\b",
            normalized,
        )
        candidate = re.search(
            r"\b(candidate|poc|proof-of-concept|reproducer|payload|input|"
            r"bytes?|string|file|stdin|fuzzer)\b",
            normalized,
        )
        causal = re.search(
            r"\b(vulnerab|trigger|root cause|sink|source|parser|decoder|"
            r"overflow|out-of-bounds|uninitialized|use-after-free|double free|"
            r"null dereference|integer overflow|sanitizer|crash)\b",
            normalized,
        )
        return bool(conclusion and candidate and causal)

    @staticmethod
    def _command_materializes_candidate(command: str) -> bool:
        normalized = (command or "").lower()
        return (
            "/workspace/poc.bin" in normalized
            or "/workspace/analysis.json" in normalized
            or "submit_candidate" in normalized
        )

    @classmethod
    def _command_is_candidate_focused(cls, command: str) -> bool:
        """Allow narrow local work needed to materialize a candidate.

        Once a candidate-level hypothesis exists, the harness should stop open
        exploration, but it must still permit the Subject to inspect exact
        source lines, confirm the harness interface, write artifacts, and run a
        local sanity check. This classifier intentionally uses command intent,
        not a fixed number of allowed tool calls.
        """
        normalized = re.sub(r"\s+", " ", command or "").strip().lower()
        if not normalized:
            return False
        if cls._command_materializes_candidate(normalized):
            return True
        forbidden = (
            "submit.sh",
            "http://",
            "https://",
            "google.",
            "duckduckgo.",
            "wget ",
            "curl ",
            "git clone",
            "apt ",
            "apt-get ",
            "pip install",
            "npm install",
            "cargo install",
        )
        if any(token in normalized for token in forbidden):
            return False
        local_reference = (
            "/workspace/description.txt",
            "/workspace/readme.md",
            "description.txt",
            "readme.md",
            "/workspace/repo-vul",
            "repo-vul/src-vul",
            "src-vul/",
        )
        if not any(token in normalized for token in local_reference):
            return False
        broad_exploration = (
            "find /workspace ",
            "find . ",
            "find /workspace/repo-vul/src-vul ",
        )
        if any(token in normalized for token in broad_exploration):
            return False
        read_only = (
            "cat ",
            "sed ",
            "grep ",
            "awk ",
            "nl ",
            "head ",
            "tail ",
            "ls ",
            "python3 - <<",
            "python - <<",
        )
        if any(token in normalized for token in read_only):
            return True
        local_candidate_check = (
            "/workspace/poc.bin" in normalized
            and any(token in normalized for token in ("python", "bash", "./", "file ", "od ", "hexdump "))
        )
        return local_candidate_check

    def materialization_reminder_needed(
        self, *, iteration: int, maximum: int, thought: str, command: str
    ) -> bool:
        """Record a candidate checkpoint and block only unfocused actions.

        This is a deterministic harness control, not an observer model. It does
        not infer a PoC or provide repair advice.  It transitions the episode
        from open exploration into candidate-focused materialization while still
        allowing narrow source/interface checks needed to write a valid PoC and
        analysis artifact.
        """
        del maximum
        state = self.store.load_observation()
        if (
            state.terminal_reason
            or state.awaiting_verification
            or state.materialization_outstanding
        ):
            return False
        if state.materialization_reminder_count >= 1:
            return False
        try:
            if self.platform.submission_ready():
                return False
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError):
            return False
        if self._command_materializes_candidate(command):
            return False
        text = "\n".join(part for part in (thought, command) if part)
        if not self._looks_like_candidate_conclusion(text):
            return False
        current_action_focused = self._command_is_candidate_focused(command)
        state.materialization_outstanding = True
        state.materialization_reminder_count += 1
        event = state.append(
            timestamp=utc_now(), source="controller",
            kind="candidate_materialization_checkpoint",
            payload={
                "iteration": iteration,
                "reason": "subject_action_contains_candidate_level_claim",
                "reminder_count": state.materialization_reminder_count,
                "current_action_allowed": current_action_focused,
            },
        )
        state.last_materialization_reminder_sequence = event.sequence
        self.store.save_observation(state)
        self.platform.inject_message(
            "[Candidate materialization checkpoint]\n"
            "Your previous reasoning/action appears to contain a concrete "
            "candidate-level vulnerability hypothesis. Continue only with "
            "candidate-focused work: inspect exact local source lines or the "
            "local harness interface if needed, then materialize the current "
            "best candidate as `/workspace/poc.bin` and write "
            "`/workspace/analysis.json` for that exact candidate, including "
            "`vuln_logic.issue_alignment` against `/workspace/description.txt`. "
            "The harness will auto-submit the new bundle and return runtime "
            "feedback."
        )
        self._refresh_agent_view()
        return not current_action_focused

    def materialization_gate_blocks_action(
        self, *, iteration: int, maximum: int, command: str
    ) -> bool:
        """Block non-materialization work while a candidate checkpoint is active."""
        del maximum
        state = self.store.load_observation()
        if (
            state.terminal_reason
            or state.awaiting_verification
            or not state.materialization_outstanding
        ):
            return False
        try:
            if self.platform.submission_ready():
                state.materialization_outstanding = False
                state.append(
                    timestamp=utc_now(), source="controller",
                    kind="candidate_materialization_satisfied",
                    payload={"iteration": iteration},
                )
                self.store.save_observation(state)
                self._refresh_agent_view()
                return False
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
            state.append(
                timestamp=utc_now(), source="controller",
                kind="candidate_materialization_state_unavailable",
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
            self.store.save_observation(state)
            self._refresh_agent_view()
            return False
        if self._command_materializes_candidate(command):
            return False
        if self._command_is_candidate_focused(command):
            state.append(
                timestamp=utc_now(), source="controller",
                kind="candidate_materialization_focused_action_allowed",
                payload={
                    "iteration": iteration,
                    "command_excerpt": self._truncate_feedback_text(
                        command, limit=240,
                    ),
                },
            )
            self.store.save_observation(state)
            self._refresh_agent_view()
            return False
        state.append(
            timestamp=utc_now(), source="controller",
            kind="candidate_materialization_gate_blocked_action",
            payload={
                "iteration": iteration,
                "blocked_command_excerpt": self._truncate_feedback_text(
                    command, limit=240,
                ),
            },
        )
        self.store.save_observation(state)
        self.platform.inject_message(self._materialization_gate_message())
        self._refresh_agent_view()
        return True

    @staticmethod
    def _materialization_gate_message() -> str:
        return (
            "[Candidate materialization gate]\n"
            "A candidate-level hypothesis has already been identified. Continue "
            "only with candidate-focused work: inspect exact local source lines or "
            "the local harness interface if needed, write or repair "
            "`/workspace/poc.bin`, and write or repair `/workspace/analysis.json`. "
            "Do not perform broad exploration, external browsing, downloads, or "
            "unrelated code search. Once both artifacts exist, the harness will "
            "auto-submit them and return runtime feedback."
        )

    def record_submission_state(self) -> str:
        state = self.store.load_observation()
        self._refresh_agent_view()
        submission_ready = False
        current_fingerprint = None
        latest_fingerprint = self.store.latest_candidate_sha256()
        bundle_fingerprint = None
        try:
            submission_ready = self.platform.submission_ready()
            current_fingerprint = (
                self.platform.submission_fingerprint()
                if submission_ready else None
            )
            bundle_fingerprint = (
                self._current_submission_bundle_fingerprint()
                if submission_ready else None
            )
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
            state.append(
                timestamp=utc_now(), source="controller",
                kind="submission_state_unavailable",
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
            self.store.save_observation(state)
            self._refresh_agent_view()
            return "continue"
        candidate_relation = (
            "absent" if not submission_ready
            else "first" if latest_fingerprint is None
            else "unchanged" if current_fingerprint == latest_fingerprint
            else "revised"
        )
        if (
            state.events
            and state.events[-1].kind == "submission_state"
            and state.events[-1].payload.get("candidate_relation_to_last_submission")
            == candidate_relation
            and state.events[-1].payload.get("bundle_sha256") == bundle_fingerprint
        ):
            return "continue"
        event = state.append(
            timestamp=utc_now(), source="controller",
            kind="submission_state", payload={
                "candidate_relation_to_last_submission": candidate_relation,
                "candidate_sha256": current_fingerprint,
                "bundle_sha256": bundle_fingerprint,
            },
        )
        state.last_observer_sequence = event.sequence
        self.store.save_observation(state)
        self._refresh_agent_view()
        return "continue"

    @staticmethod
    def _validate_analysis(path: Path) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "sample_id", "fine_trace", "vuln_logic",
        }:
            raise ValueError(
                "analysis.json must contain exactly sample_id, fine_trace, and vuln_logic"
            )
        if not isinstance(value["fine_trace"], list):
            raise ValueError("analysis.json fine_trace must be an array")
        if not isinstance(value["vuln_logic"], dict):
            raise ValueError("analysis.json vuln_logic must be an object")
        if not value["fine_trace"]:
            raise ValueError("analysis.json fine_trace must not be empty")
        quality_error = validate_analysis_artifact_quality(
            path.read_text(encoding="utf-8")
        )
        if quality_error:
            raise ValueError(f"analysis.json quality check failed: {quality_error}")

        allowed_roles = {"source", "root_cause", "sink", "intermediate", "null"}
        role_locations: dict[str, set[tuple[str, str, int]]] = {
            "source": set(),
            "root_cause": set(),
            "sink": set(),
        }
        trace_locations: set[tuple[str, str, int]] = set()
        for index, step in enumerate(value["fine_trace"], start=1):
            if not isinstance(step, dict):
                raise ValueError(f"fine_trace[{index}] must be an object")
            for field in ("step", "file", "function", "line", "var", "code", "role", "note"):
                if field not in step:
                    raise ValueError(f"fine_trace[{index}] missing {field}")
            if not isinstance(step["step"], int) or step["step"] < 1:
                raise ValueError(f"fine_trace[{index}].step must be a positive integer")
            if not isinstance(step["line"], int) or step["line"] < 1:
                raise ValueError(f"fine_trace[{index}].line must be a positive integer")
            for field in ("file", "function", "var", "code", "role", "note"):
                if not isinstance(step[field], str) or not step[field].strip():
                    raise ValueError(f"fine_trace[{index}].{field} must be non-empty")
            role = step["role"]
            if role not in allowed_roles:
                raise ValueError(f"fine_trace[{index}].role is invalid: {role}")
            if role in role_locations:
                location = (
                    step["file"].strip(),
                    step["function"].strip(),
                    int(step["line"]),
                )
                role_locations[role].add(location)
                trace_locations.add(location)
            elif role == "intermediate":
                trace_locations.add((
                    step["file"].strip(),
                    step["function"].strip(),
                    int(step["line"]),
                ))

        logic = value["vuln_logic"]
        required_logic = {
            "source", "root_cause", "sink", "propagation", "issue_alignment",
        }
        missing = sorted(required_logic - set(logic))
        if missing:
            raise ValueError(f"vuln_logic missing required point(s): {', '.join(missing)}")

        alignment = logic["issue_alignment"]
        if not isinstance(alignment, dict):
            raise ValueError("vuln_logic.issue_alignment must be an object")
        required_alignment = {
            "admission", "source", "root_cause", "propagation", "sink",
        }
        if set(alignment) != required_alignment:
            raise ValueError(
                "vuln_logic.issue_alignment must contain exactly admission, "
                "source, root_cause, propagation, and sink"
            )
        for field in sorted(required_alignment):
            if not isinstance(alignment[field], str) or not alignment[field].strip():
                raise ValueError(
                    f"vuln_logic.issue_alignment.{field} must be non-empty"
                )

        def require_location(anchor: Any, name: str) -> tuple[str, str, int]:
            if not isinstance(anchor, dict):
                raise ValueError(f"{name} must be an object")
            for field in ("file", "function", "line"):
                if field not in anchor:
                    raise ValueError(f"{name} missing {field}")
            if not isinstance(anchor["file"], str) or not anchor["file"].strip():
                raise ValueError(f"{name}.file must be non-empty")
            if not isinstance(anchor["function"], str) or not anchor["function"].strip():
                raise ValueError(f"{name}.function must be non-empty")
            if not isinstance(anchor["line"], int) or anchor["line"] < 1:
                raise ValueError(f"{name}.line must be a positive integer")
            return (
                anchor["file"].strip(),
                anchor["function"].strip(),
                int(anchor["line"]),
            )

        def require_operands(anchor: dict[str, Any], name: str) -> None:
            operands = anchor.get("operands")
            if (
                not isinstance(operands, list)
                or not operands
                or not all(isinstance(item, str) and item.strip() for item in operands)
            ):
                raise ValueError(f"{name}.operands must be a non-empty string array")

        def require_relation(anchor: dict[str, Any], name: str) -> None:
            relation = anchor.get("relation")
            if not isinstance(relation, dict) or set(relation) != {"op", "left", "right"}:
                raise ValueError(f"{name}.relation must contain exactly op, left, right")
            if relation["op"] not in {"eq", "ne", "lt", "le", "gt", "ge", "same_object"}:
                raise ValueError(f"{name}.relation.op is invalid")
            for field in ("left", "right"):
                if not isinstance(relation[field], str) or not relation[field].strip():
                    raise ValueError(f"{name}.relation.{field} must be non-empty")

        for role, key in (
            ("source", "source"),
            ("root_cause", "root_cause"),
            ("sink", "sink"),
        ):
            anchor = logic[key]
            location = require_location(anchor, f"vuln_logic.{key}")
            require_operands(anchor, f"vuln_logic.{key}")
            if role != "source":
                require_relation(anchor, f"vuln_logic.{key}")
            if location not in role_locations[role]:
                raise ValueError(
                    f"vuln_logic.{key} location must match a fine_trace step "
                    f"with role={role!r}"
                )

        propagation = logic["propagation"]
        if not isinstance(propagation, list):
            raise ValueError("vuln_logic.propagation must be an array")
        for index, edge in enumerate(propagation, start=1):
            if not isinstance(edge, dict):
                raise ValueError(f"vuln_logic.propagation[{index}] must be an object")
            for endpoint in ("from", "to"):
                location = require_location(
                    edge.get(endpoint), f"vuln_logic.propagation[{index}].{endpoint}"
                )
                if location not in trace_locations:
                    raise ValueError(
                        f"vuln_logic.propagation[{index}].{endpoint} must match "
                        "an existing role-marked fine_trace step"
                    )
                require_operands(
                    edge[endpoint], f"vuln_logic.propagation[{index}].{endpoint}"
                )
            if edge.get("type") not in {"data", "control", "order"}:
                raise ValueError(f"vuln_logic.propagation[{index}].type is invalid")
            via = edge.get("via")
            if (
                not isinstance(via, list)
                or not via
                or not all(isinstance(item, str) and item.strip() for item in via)
            ):
                raise ValueError(f"vuln_logic.propagation[{index}].via must be non-empty")
            if "relation" in edge:
                require_relation(edge, f"vuln_logic.propagation[{index}]")

    @staticmethod
    def _truncate_feedback_text(value: Any, limit: int = 220) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    @classmethod
    def _issue_alignment_review(
        cls, *, analysis_path: Path, issue_description: str,
        assessment: Any,
    ) -> dict[str, Any]:
        """Compare the submitted issue-alignment claims with runtime status.

        This is deliberately not a semantic judge. The Subject has already been
        required to state how its candidate maps to the public issue; the
        controller returns that self-comparison alongside deterministic runtime
        stage evidence so failed submissions identify which issue-aligned claim
        remains unverified.
        """
        value = json.loads(analysis_path.read_text(encoding="utf-8"))
        alignment = (value.get("vuln_logic") or {}).get("issue_alignment") or {}
        stage_map = (
            ("admission", "admission"),
            ("source", "source"),
            ("root_cause", "root"),
            ("propagation", "propagation"),
            ("sink", "sink"),
        )
        stages = []
        for analysis_stage, runtime_stage in stage_map:
            status = assessment.stages.get(runtime_stage)
            stages.append({
                "stage": analysis_stage,
                "runtime_stage": runtime_stage,
                "runtime_status": status.value if status is not None else "unknown",
                "submitted_issue_alignment": cls._truncate_feedback_text(
                    alignment.get(analysis_stage), limit=500,
                ),
            })
        return {
            "basis": (
                "submitted_candidate_issue_alignment_vs_public_issue_description_"
                "with_deterministic_runtime_stage_status"
            ),
            "public_issue_excerpt": cls._truncate_feedback_text(
                issue_description, limit=700,
            ),
            "longest_confirmed_prefix": list(assessment.longest_confirmed_prefix),
            "first_unresolved_boundary": assessment.first_unresolved,
            "stages": stages,
            "llm_judge_used": False,
        }

    @classmethod
    def _issue_alignment_feedback_text(cls, review: dict[str, Any]) -> str:
        parts = []
        for item in review["stages"]:
            parts.append(
                f"{item['stage']}[{item['runtime_status']}]: "
                + cls._truncate_feedback_text(
                    item["submitted_issue_alignment"], limit=160,
                )
            )
        return "Submitted issue-alignment vs runtime status: " + "; ".join(parts) + "."

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
            poc_path, analysis_path = parse_submission(
                self.platform.workspace_root, arguments
            )
            self._validate_analysis(analysis_path)
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
            poc_path=poc_path, analysis_path=analysis_path,
            checkpoint_path=checkpoint,
        )
        stored_poc = registered.candidate_dir / "poc"
        stored_analysis = registered.attempt_dir / "analysis.json"
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
        state.last_materialization_reminder_sequence = 0
        self.store.save_observation(state)

        previous = self.store.previous_distinct_evidence(registered.sha256)
        previous_path = None
        if previous is not None:
            previous_path = registered.attempt_dir / "prior_evidence.json"
            atomic_json(previous_path, previous)
        self._refresh_agent_view(
            current_analysis=stored_analysis, prior_evidence=previous_path
        )

        plan = plan_assertions(task.reward_spec)
        runtime_dir = registered.attempt_dir / "runtime"
        try:
            report = self.instrumentation.verify(
                poc_path=stored_poc,
                analysis_path=stored_analysis,
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
        assessment = evaluate_stages(task.reward_spec, report)
        runtime_path = registered.attempt_dir / "current_runtime.json"
        atomic_json(runtime_path, self._runtime_dict(report, assessment))
        self._refresh_agent_view(
            current_analysis=stored_analysis,
            prior_evidence=previous_path,
            current_runtime=runtime_path,
        )
        feedback = self.feedback_agent.generate(
            report=report,
            assessment=assessment,
            previous=previous,
        )
        issue_alignment_review = self._issue_alignment_review(
            analysis_path=stored_analysis,
            issue_description=task.issue_description,
            assessment=assessment,
        )
        feedback = Feedback(
            summary=feedback.summary + " "
            + self._issue_alignment_feedback_text(issue_alignment_review),
            contradiction=feedback.contradiction,
            delta=feedback.delta,
            evidence_ids=feedback.evidence_ids,
            assessment=feedback.assessment,
            issue_alignment_review=issue_alignment_review,
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
            current_analysis=stored_analysis,
            prior_evidence=previous_path,
            current_runtime=runtime_path,
        )
        if not report.trigger_observed:
            self._refresh_agent_view(
                current_analysis=stored_analysis,
                prior_evidence=previous_path,
                current_runtime=runtime_path,
            )
            factual = feedback.summary
            if feedback.contradiction:
                factual += " " + feedback.contradiction
            factual += (
                " Before preparing the next candidate, compare the submitted "
                "/workspace/analysis.json against /workspace/description.txt "
                "across admission, source, root_cause, propagation, and sink; "
                "revise issue_alignment together with any changed causal claims."
            )
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
            "its iteration limit. If you have a candidate-level conclusion, "
            "do not finish: materialize the candidate as /workspace/poc.bin, "
            "write /workspace/analysis.json with issue_alignment against "
            "/workspace/description.txt, and submit it through submit_candidate. "
            "If no candidate-level conclusion exists yet, continue investigating."
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
            "materialization_reminder_count": state.materialization_reminder_count,
            "auto_submission_count": state.auto_submission_count,
            "trajectory_events": len(state.events),
            "candidate_stats": self.store.candidate_stats(),
            "harness": self.store.load_harness_state().to_dict(),
        }

    def finalize_episode(self) -> Path:
        """Persist a GT-free episode-local summary for inspection."""
        state = self.store.load_observation()
        summary = {
            "task_id": self.store.load_task().task_id,
            "terminal_reason": state.terminal_reason,
            "candidate_stats": self.store.candidate_stats(),
            "latest_evidence": self.store.load_evidence_state().to_dict(),
            "gt_used": False,
        }
        path = self.store.root / "episode_summary.json"
        atomic_json(path, summary)
        return path
