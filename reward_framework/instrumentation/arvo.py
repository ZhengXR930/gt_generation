"""ARVO execution and source-aligned GDB instrumentation bridge.

This module consumes only the submitted candidate, the public vulnerable
source tree, and a Reward-Agent probe plan.  It never opens ``gt_results`` or
uses a saved sanitizer trace.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from evaluator.reachability.arvo_gdb import (
    PreparedTarget,
    prepare_arvo_target,
    run_arvo_gdb,
    target_arguments,
)

from ..assertion_reward import ClaimResult, check_value
from ..models import Probe, ProbePlan, RawRuntimeReport, RuntimeFact, StageStatus
from ..runtime import default_trigger_oracle
from ..state_store import atomic_json


def _normalized_with_offsets(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    offsets: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        if char.isspace():
            if normalized and not in_space:
                normalized.append(" ")
                offsets.append(index)
            in_space = True
        else:
            normalized.append(char)
            offsets.append(index)
            in_space = False
    return "".join(normalized).strip(), offsets


def resolve_statement_line(source_root: Path, probe: Probe) -> int | None:
    """Resolve the current source line, repairing stale trace line numbers."""
    if not probe.statement:
        return None
    path = (source_root / probe.file).resolve()
    source_root = source_root.resolve()
    if source_root not in path.parents or not path.is_file():
        raise ValueError(f"probe source is unavailable: {probe.file}")
    text = path.read_text(encoding="utf-8", errors="replace")
    direct = text.find(probe.statement)
    if direct >= 0:
        return text.count("\n", 0, direct) + 1
    normalized_text, offsets = _normalized_with_offsets(text)
    normalized_statement = " ".join(probe.statement.split())
    position = normalized_text.find(normalized_statement)
    if position < 0 or position >= len(offsets):
        raise ValueError(
            f"cannot resolve probe statement in current source: {probe.file}: "
            f"{probe.statement[:120]}"
        )
    return text.count("\n", 0, offsets[position]) + 1


def compile_checkpoints(source_root: Path, plan: ProbePlan) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    for index, probe in enumerate(plan.probes, 1):
        captures = {}
        if probe.claim_id:
            operands = []
            if probe.claim_kind == "propagation":
                operands = [
                    ("left", probe.left_operand)
                    if probe.endpoint == "from" else
                    ("right", probe.right_operand)
                ]
            elif probe.claim_kind in {"root", "sink"}:
                operands = [("left", probe.left_operand), ("right", probe.right_operand)]
            elif probe.claim_kind in {"source", "admission"}:
                operands = [
                    (f"operand_{i}", expression)
                    for i, expression in enumerate(probe.captures, 1)
                ]
            for name, expression in operands:
                if _operand_literal(expression)[0]:
                    continue
                captures[name] = str(expression)
        else:
            captures = {
                f"capture_{capture_index}": expression
                for capture_index, expression in enumerate(probe.captures, 1)
            }
        if probe.condition:
            captures["__reward_condition"] = probe.condition
        checkpoints.append({
            "kind": "condition_event" if probe.condition else "assertion_event",
            "event_point": f"reward_{index:02d}_{probe.stage}_{probe.anchor_kind}",
            "assertion_role": [probe.stage],
            "expected_order": index - 1,
            "file": probe.file,
            "function": probe.function,
            "line": resolve_statement_line(source_root, probe) or probe.line,
            "captures": captures,
            "reward_probe": {
                "stage": probe.stage,
                "anchor_kind": probe.anchor_kind,
                "statement": probe.statement,
                "condition": probe.condition,
                "purpose": probe.purpose,
                "claim_id": probe.claim_id,
                "claim_kind": probe.claim_kind,
                "endpoint": probe.endpoint,
                "check_op": probe.check_op,
                "left_operand": probe.left_operand,
                "right_operand": probe.right_operand,
                "required": probe.required,
            },
        })
    return checkpoints


def _operand_literal(value: Any) -> tuple[bool, Any]:
    if not isinstance(value, str):
        return True, value
    text = value.strip()
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return True, lowered == "true"
    if lowered in {"null", "none", "nullptr"}:
        return True, None
    try:
        return True, int(text, 0)
    except ValueError:
        return False, None


def _operand_value(raw: Any, fields: dict[str, Any], name: str) -> Any:
    literal, value = _operand_literal(raw)
    if literal:
        return value
    if name not in fields:
        raise KeyError(f"missing captured operand: {name}")
    return fields[name]


def evaluate_claim_hits(
    checkpoints: list[dict[str, Any]], hits: list[dict[str, Any]], checked: bool,
) -> tuple[str, tuple[ClaimResult, ...]]:
    ordered_hits = []
    for order, hit in enumerate(hits):
        if hit.get("line") is not None:
            ordered_hits.append((order, hit))
    checkpoint_by_point = {
        str(item["event_point"]): item for item in checkpoints
        if item.get("reward_probe", {}).get("claim_id")
    }
    admission_points = {
        point for point, checkpoint in checkpoint_by_point.items()
        if checkpoint["reward_probe"].get("claim_kind") == "admission"
    }
    if any(str(hit.get("event_point")) in admission_points for _, hit in ordered_hits):
        admission = "confirmed"
    elif not checked or any(
        hit.get("breakpoint_error") and str(hit.get("event_point")) in admission_points
        for hit in hits
    ):
        admission = "unresolved"
    else:
        admission = "not_reached"

    groups: dict[str, list[tuple[str, int, dict[str, Any], dict[str, Any]]]] = {}
    for order, hit in ordered_hits:
        point = str(hit.get("event_point") or "")
        checkpoint = checkpoint_by_point.get(point)
        if not checkpoint:
            continue
        meta = checkpoint["reward_probe"]
        kind = str(meta.get("claim_kind") or "")
        if kind == "admission":
            continue
        groups.setdefault(str(meta["claim_id"]), []).append(
            (str(meta.get("endpoint") or "at"), order, hit, meta)
        )
    declared: dict[str, dict[str, Any]] = {}
    for checkpoint in checkpoints:
        meta = checkpoint.get("reward_probe") or {}
        if meta.get("claim_id") and meta.get("claim_kind") != "admission":
            declared[str(meta["claim_id"])] = meta

    results = []
    for claim_id, meta in declared.items():
        stage = str(meta["claim_kind"])
        entries = groups.get(claim_id, [])
        values: list[tuple[bool, Any, Any]] = []
        if stage == "propagation":
            sources = [item for item in entries if item[0] == "from"]
            targets = [item for item in entries if item[0] == "to"]
            for _, source_order, source_hit, _ in sources:
                for _, target_order, target_hit, _ in targets:
                    if source_order >= target_order:
                        continue
                    try:
                        left = _operand_value(
                            meta.get("left_operand"), source_hit.get("fields") or {}, "left"
                        )
                        right = _operand_value(
                            meta.get("right_operand"), target_hit.get("fields") or {}, "right"
                        )
                        values.append((check_value(str(meta["check_op"]), left, right), left, right))
                    except (KeyError, TypeError, ValueError):
                        pass
            reached = bool(sources and targets)
        else:
            reached = bool(entries)
            if meta.get("check_op") is None:
                values = [(True, None, None)] if reached else []
            else:
                for _, _, hit, _ in entries:
                    try:
                        fields = hit.get("fields") or {}
                        left = _operand_value(meta.get("left_operand"), fields, "left")
                        right = _operand_value(meta.get("right_operand"), fields, "right")
                        values.append((check_value(str(meta["check_op"]), left, right), left, right))
                    except (KeyError, TypeError, ValueError):
                        pass
        evaluated = bool(values)
        expected = True
        matched = any(item[0] is expected for item in values) if evaluated else None
        if not reached:
            status = "not_reached"
        elif not evaluated:
            status = "unresolved"
        else:
            status = "confirmed" if matched else "not_observed"
        representative = next((item for item in values if item[0] is expected), values[-1] if values else None)
        results.append(ClaimResult(
            claim_id, stage, status, reached, evaluated,
            representative[0] if representative else None,
            matched,
            bool(meta.get("required", True)),
            representative[1] if representative else None,
            representative[2] if representative else None,
        ))
    return admission, tuple(results)


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1"}:
            return True
        if lowered in {"false", "0"}:
            return False
    return None


class ArvoGDBInstrumentationBackend:
    """Run ARVO's real harness and passive GDB probes for each submission."""

    def __init__(self, *, image: str, source_root: Path, repo_root: Path,
                 timeout: int = 180, debugger_image: str = "gt-memory-env:latest"):
        self.image = image
        self.source_root = source_root.resolve()
        self.repo_root = repo_root.resolve()
        self.timeout = timeout
        self.debugger_image = debugger_image
        self._target_context = None
        self._prepared: PreparedTarget | None = None

    def _target(self) -> PreparedTarget:
        if self._prepared is None:
            self._target_context = prepare_arvo_target(
                self.image, repo_root=self.repo_root,
                debugger_image=self.debugger_image,
            )
            self._prepared = self._target_context.__enter__()
        return self._prepared

    def close(self) -> None:
        if self._target_context is not None:
            self._target_context.__exit__(None, None, None)
        self._target_context = None
        self._prepared = None

    def __enter__(self) -> "ArvoGDBInstrumentationBackend":
        self._target()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _execute(self, prepared: PreparedTarget, poc_path: Path) -> subprocess.CompletedProcess[str]:
        if not prepared.container_id:
            raise RuntimeError("ARVO reward execution requires the native target container")
        return subprocess.run(
            [
                "docker", "exec", "-e", "ASAN_OPTIONS=detect_leaks=0",
                prepared.container_id, *target_arguments(prepared, poc_path),
            ],
            text=True, capture_output=True, timeout=self.timeout, check=False,
        )

    @staticmethod
    def _observations(checkpoints: list[dict[str, Any]], hits: list[dict[str, Any]],
                      checked: bool) -> tuple[dict[str, StageStatus], tuple[RuntimeFact, ...]]:
        by_event: dict[str, list[dict[str, Any]]] = {}
        for hit in hits:
            point = str(hit.get("event_point") or "")
            if point:
                by_event.setdefault(point, []).append(hit)
        statuses: dict[str, list[StageStatus]] = {}
        facts: list[RuntimeFact] = []
        for checkpoint in checkpoints:
            point = checkpoint["event_point"]
            stage = checkpoint["reward_probe"]["stage"]
            event_hits = by_event.get(point, [])
            real_hits = [item for item in event_hits if item.get("line") is not None]
            errors = [item for item in event_hits if item.get("breakpoint_error")]
            if real_hits:
                last = real_hits[-1]
                fields = dict(last.get("fields") or {})
                condition = checkpoint["reward_probe"].get("condition")
                condition_values = (
                    [_truth((item.get("fields") or {}).get("__reward_condition"))
                     for item in real_hits]
                    if condition else []
                )
                if not condition or True in condition_values:
                    status = StageStatus.CONFIRMED
                    condition_value = True if condition else None
                elif condition_values and all(value is False for value in condition_values):
                    status = StageStatus.REFUTED
                    condition_value = False
                else:
                    status = StageStatus.UNRESOLVED
                    condition_value = None
                fact_data = {
                    "event_point": point,
                    "file": last.get("file"), "function": last.get("function"),
                    "line": last.get("line"),
                    "captures": {k: v for k, v in fields.items() if k != "__reward_condition"},
                    "condition": condition,
                    "condition_value": condition_value,
                    "condition_values": condition_values,
                }
                statement = (
                    f"The {stage} checkpoint executed; its condition evaluated {condition_value}."
                    if condition else f"The exact {stage} checkpoint executed."
                )
                facts.append(RuntimeFact(point, stage, "gdb_checkpoint", statement, fact_data))
            elif errors or not checked:
                status = StageStatus.UNRESOLVED
                facts.append(RuntimeFact(
                    point, stage, "probe_unavailable",
                    f"The {stage} checkpoint could not be observed reliably.",
                    {"errors": [item.get("breakpoint_error") for item in errors]},
                ))
            else:
                status = StageStatus.NOT_REACHED
                facts.append(RuntimeFact(
                    point, stage, "checkpoint_not_reached",
                    f"The candidate did not reach the source-valid {stage} checkpoint.",
                    {"file": checkpoint["file"], "function": checkpoint["function"],
                     "line": checkpoint["line"]},
                ))
            statuses.setdefault(stage, []).append(status)

        observations: dict[str, StageStatus] = {}
        for stage, values in statuses.items():
            if StageStatus.CONFIRMED in values:
                observations[stage] = StageStatus.CONFIRMED
            elif StageStatus.REFUTED in values:
                observations[stage] = StageStatus.REFUTED
            elif StageStatus.UNRESOLVED in values:
                observations[stage] = StageStatus.UNRESOLVED
            else:
                observations[stage] = StageStatus.NOT_REACHED
        return observations, tuple(facts)

    def verify(self, *, poc_path: Path, analysis_path: Path, plan: ProbePlan,
               output_dir: Path) -> RawRuntimeReport:
        output_dir.mkdir(parents=True, exist_ok=True)
        prepared = self._target()
        checkpoints = compile_checkpoints(self.source_root, plan)
        # The native debugger container mounts repo_root only. Stage immutable
        # inputs there so arbitrary OpenHands workspace/log locations remain
        # supported, then copy the compact evidence back to the attempt.
        with tempfile.TemporaryDirectory(
            prefix=".arvo-reward-runtime.", dir=self.repo_root
        ) as raw_staging:
            staging = Path(raw_staging)
            staged_poc = staging / "poc"
            shutil.copy2(poc_path, staged_poc)
            shutil.copy2(analysis_path, staging / "analysis.json")
            work = staging / "runtime"
            report = self._verify_visible(
                prepared=prepared, poc_path=staged_poc,
                checkpoints=checkpoints, output_dir=work,
            )
            shutil.copytree(work, output_dir, dirs_exist_ok=True)
            return report

    def _verify_visible(self, *, prepared: PreparedTarget, poc_path: Path,
                        checkpoints: list[dict[str, Any]],
                        output_dir: Path) -> RawRuntimeReport:
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(output_dir / "compiled_checkpoints.json", checkpoints)
        execution_error = None
        try:
            execution = self._execute(prepared, poc_path)
            exit_code, stdout, stderr = (
                execution.returncode, execution.stdout, execution.stderr
            )
        except (subprocess.TimeoutExpired, OSError, ValueError, RuntimeError) as exc:
            exit_code, stdout, stderr = None, "", ""
            execution_error = f"{type(exc).__name__}: {exc}"
        (output_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
        (output_dir / "stderr.txt").write_text(stderr, encoding="utf-8")

        gdb_error = None
        hits: list[dict[str, Any]] = []
        checked = False
        if checkpoints:
            try:
                _result, hits, checked = run_arvo_gdb(
                    prepared=prepared, poc_path=poc_path,
                    checkpoints=checkpoints, output_dir=output_dir / "gdb",
                    repo_root=self.repo_root, timeout=self.timeout,
                    debugger_image=self.debugger_image, max_hits_per_event=8,
                )
            except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
                gdb_error = f"{type(exc).__name__}: {exc}"
        observations, facts = self._observations(checkpoints, hits, checked)
        _admission, claim_results = evaluate_claim_hits(checkpoints, hits, checked)
        if claim_results or any(
            item.get("reward_probe", {}).get("claim_kind") == "admission"
            for item in checkpoints
        ):
            observations["admission"] = StageStatus(_admission)
        triggered = default_trigger_oracle(exit_code, stdout, stderr)
        facts += (RuntimeFact(
            "TRIGGER-ORACLE", "trigger", "trigger_oracle",
            "The independent target run observed a vulnerability signal."
            if triggered else
            "The independent target run did not observe a vulnerability signal.",
            {"exit_code": exit_code},
        ),)
        error = "; ".join(part for part in (execution_error, gdb_error) if part) or None
        return RawRuntimeReport(
            exit_code=exit_code, stdout=stdout, stderr=stderr,
            trigger_observed=triggered, stage_observations=observations,
            facts=facts, instrumentation_available=checked, error=error,
            claim_results=claim_results,
        )
