"""Runtime instrumentation protocol and a command-backed implementation."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import ProbePlan, RawRuntimeReport, RuntimeFact, StageStatus
from .state_store import atomic_json


class InstrumentationBackend(Protocol):
    def verify(self, *, poc_path: Path, analysis_path: Path, plan: ProbePlan,
               output_dir: Path) -> RawRuntimeReport: ...


def default_trigger_oracle(exit_code: int | None, stdout: str, stderr: str) -> bool:
    text = (stdout + "\n" + stderr).lower()
    sanitizer = any(marker in text for marker in (
        "addresssanitizer", "memorysanitizer", "undefinedbehaviorsanitizer",
        "runtime error:", "heap-buffer-overflow", "stack-buffer-overflow",
        "use-after-free", "double-free",
    ))
    # Ordinary non-zero application exits are not vulnerability proof.  A
    # platform with a special exit convention must supply its own oracle.
    signaled = exit_code is not None and (exit_code < 0 or exit_code in {134, 139})
    return sanitizer or signaled


def _replace(tokens: list[str], values: dict[str, str]) -> list[str]:
    result = []
    for token in tokens:
        value = token
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", replacement)
        result.append(value)
    return result


def _load_stage_report(path: Path) -> tuple[dict[str, StageStatus], tuple[RuntimeFact, ...], str | None]:
    if not path.is_file():
        return {}, (), "instrumentation produced no stage report"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        raw_observations = value.get("stage_observations") or {}
        unknown = set(raw_observations) - {
            "admission", "source", "root", "propagation", "sink"
        }
        if unknown:
            raise ValueError(f"unknown stage observations: {sorted(unknown)}")
        observations = {
            stage: StageStatus(status)
            for stage, status in raw_observations.items()
        }
        if any(status in {StageStatus.NOT_DECLARED, StageStatus.OBSERVED_BUT_BLOCKED}
               or status == StageStatus.SPEC_OR_MAPPING_CONFLICT
               for status in observations.values()):
            raise ValueError("instrumentation cannot assign controller-owned statuses")
        facts = tuple(RuntimeFact(**item) for item in value.get("facts", []))
        return observations, facts, value.get("error")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {}, (), f"invalid instrumentation report: {exc}"


class CommandInstrumentationBackend:
    """Execute a candidate and optionally an external probe compiler/runner.

    The optional instrumentation command receives these literal placeholders:
    ``{poc}``, ``{analysis}``, ``{probe_plan}``, ``{stage_report}``, and
    ``{output_dir}``.  It must write a JSON stage report with controller-owned
    stage observations and runtime facts.  This keeps ARVO GDB, local harnesses,
    and future platform-specific instrumentation behind one protocol.
    """

    def __init__(self, *, execution_command: list[str],
                 instrumentation_command: list[str] | None = None,
                 timeout: int = 120,
                 trigger_oracle: Callable[[int | None, str, str], bool] = default_trigger_oracle):
        if not execution_command:
            raise ValueError("execution_command cannot be empty")
        self.execution_command = list(execution_command)
        self.instrumentation_command = (
            list(instrumentation_command) if instrumentation_command else None
        )
        self.timeout = timeout
        self.trigger_oracle = trigger_oracle

    def verify(self, *, poc_path: Path, analysis_path: Path, plan: ProbePlan,
               output_dir: Path) -> RawRuntimeReport:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe_path = output_dir / "probe_plan.json"
        stage_path = output_dir / "stage_report.json"
        atomic_json(probe_path, plan.to_dict())
        values = {
            "poc": str(poc_path.resolve()),
            "analysis": str(analysis_path.resolve()),
            "probe_plan": str(probe_path.resolve()),
            "stage_report": str(stage_path.resolve()),
            "output_dir": str(output_dir.resolve()),
        }
        command = _replace(self.execution_command, values)
        try:
            completed = subprocess.run(
                command, text=True, capture_output=True, timeout=self.timeout,
                check=False,
            )
            exit_code = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
            execution_error = None
        except subprocess.TimeoutExpired as exc:
            exit_code = None
            stdout = exc.stdout or "" if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr or "" if isinstance(exc.stderr, str) else ""
            execution_error = f"candidate execution timed out after {self.timeout}s"
        (output_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
        (output_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
        atomic_json(output_dir / "execution.json", {
            "command": command, "exit_code": exit_code, "error": execution_error,
        })

        observations: dict[str, StageStatus] = {}
        facts: tuple[RuntimeFact, ...] = ()
        instrumentation_error = None
        instrumentation_available = False
        if self.instrumentation_command:
            instrument = _replace(self.instrumentation_command, values)
            try:
                probe_run = subprocess.run(
                    instrument, text=True, capture_output=True,
                    timeout=self.timeout, check=False,
                )
                (output_dir / "instrumentation_stdout.txt").write_text(
                    probe_run.stdout, encoding="utf-8"
                )
                (output_dir / "instrumentation_stderr.txt").write_text(
                    probe_run.stderr, encoding="utf-8"
                )
                observations, facts, instrumentation_error = _load_stage_report(stage_path)
                instrumentation_available = stage_path.is_file() and probe_run.returncode == 0
                if probe_run.returncode != 0:
                    instrumentation_error = (
                        instrumentation_error
                        or f"instrumentation exited {probe_run.returncode}"
                    )
            except subprocess.TimeoutExpired:
                instrumentation_error = f"instrumentation timed out after {self.timeout}s"

        triggered = self.trigger_oracle(exit_code, stdout, stderr)
        trigger_fact = RuntimeFact(
            fact_id="TRIGGER-ORACLE",
            stage="trigger",
            kind="trigger_oracle",
            statement=(
                "The independent runtime trigger oracle observed a vulnerability signal."
                if triggered else
                "The independent runtime trigger oracle did not observe a vulnerability signal."
            ),
            data={"exit_code": exit_code},
        )
        error = "; ".join(
            part for part in (execution_error, instrumentation_error) if part
        ) or None
        return RawRuntimeReport(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            trigger_observed=triggered,
            stage_observations=observations,
            facts=facts + (trigger_fact,),
            instrumentation_available=instrumentation_available,
            error=error,
        )


class StaticInstrumentationBackend:
    """Deterministic backend for integration tests and adapter development."""

    def __init__(self, report: RawRuntimeReport):
        self.report = report
        self.calls: list[dict[str, Any]] = []

    def verify(self, *, poc_path: Path, analysis_path: Path, plan: ProbePlan,
               output_dir: Path) -> RawRuntimeReport:
        output_dir.mkdir(parents=True, exist_ok=True)
        self.calls.append({
            "poc_path": str(poc_path), "analysis_path": str(analysis_path),
            "plan": plan.to_dict(), "output_dir": str(output_dir),
        })
        atomic_json(output_dir / "static_runtime.json", {
            **asdict(self.report),
            "stage_observations": {
                key: value.value for key, value in self.report.stage_observations.items()
            },
            "facts": [asdict(fact) for fact in self.report.facts],
        })
        return self.report
