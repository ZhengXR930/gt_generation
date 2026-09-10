"""Distillation roles as Codex subsessions.

The diagnostician, Teacher, and Curator are agents, not one-shot completions.
Each gets a working directory of input files and writes one JSON artifact back,
exactly the way the coding agent writes `analysis.json`. That buys three things
a single chat call cannot: the diagnostician reads the *whole* trajectory
instead of a truncated excerpt, every role can grep and diff its inputs with
real tools, and all four agents in the pipeline run on the same harness and
model, so provenance stays comparable.

Role workspaces deliberately get a clean `CODEX_HOME`: the PoC reproduction and
submission skills belong to the agent under study, not to the roles studying it.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_runtime.subsession import SubsessionSpec, run_codex_subsession

from .artifacts import GT_RESULTS, REPO_ROOT, load_json_if_exists, write_json
from .outcome import diagnostics_summary
from .prompts import role_task_prompt
from .reference import synthesize_reference
from .skill_packet import lessons_snapshot

OUTPUT_NAME = "OUTPUT.json"
STATUS_NAME = "STATUS.json"

# A subsession that exited non-zero or ran out of time failed on its own terms:
# there is nothing to salvage, so it starts over. One that ran to completion but
# left no usable artifact only has to finish the write, and its workspace still
# holds whatever it worked out, so it is re-entered instead.
RESUMABLE = ("missing_output", "invalid_output")

# GT files worth handing the diagnostician. The whole sample directory is not
# copied: it also holds the reference PoC and build material the role has no use
# for, and copying less keeps the role's view of the sample legible.
GT_FILES = (
    ("description.txt", "public vulnerability description"),
    ("ground_truth.json", "curated ground truth: source, root cause, sink, fine_trace"),
    ("verified_invariants.json", "verified invariant graph used by the reasoning score"),
    ("field_bindings.json", "expression bindings used to normalize operands"),
    ("sanitizer_trace.txt", "sanitizer output from the reference reproduction"),
    ("default_crash_trace.txt", "reference crash trace"),
    ("reachability_report.json", "reference reachability report"),
)

TRAJECTORY_CANDIDATES = (
    "checkpoint/trajectory",
    "checkpoint/trajectory.json",
    "checkpoint/agent.log",
    "checkpoint/openhands_stdout.txt",
    "checkpoint/codex_stdout.txt",
    "checkpoint/claude_stdout.txt",
)

CHECKPOINT_FILES = (
    "status.json",
    "task.json",
    "workspace_listing.txt",
    "analysis_candidates.json",
    "returncode.txt",
    "args.json",
)

RUN_LOG_PATTERNS = ("*.log", "*.txt", "*/bridge.log")

SUBMISSION_FILES = (
    "result.json",
    "analysis.json",
    "runtime_output.txt",
    "request.json",
    "candidate_trace.json",
    "candidate_trace.response.txt",
    "poc.bin",
)


@dataclass(frozen=True)
class RoleRun:
    role: str
    workspace: Path
    prompt: str
    output_file: Path

    @property
    def inputs(self) -> Path:
        return self.workspace / "inputs"


def plan_role(workspace: Path, output_file: Path) -> tuple[str, str]:
    """Decide how to (re-)run a role from what its workspace already holds.

    Returns one of ``reuse`` (a good artifact is already there), ``resume``
    (re-enter the existing workspace to finish the write), or ``fresh``.
    """
    if isinstance(load_json_if_exists(output_file), dict):
        return "reuse", "artifact already present"
    if not (workspace / "TASK.md").is_file():
        return "fresh", "no previous workspace"
    previous = load_json_if_exists(workspace / STATUS_NAME) or {}
    status = str(previous.get("status") or "")
    if status in RESUMABLE:
        return "resume", f"previous session ended as {status}"
    return "fresh", f"previous session ended as {status or 'unknown'}"


def _source_tree_for_sample(sample_id: str) -> Path | None:
    """Return the same vulnerable source tree shape the coding agent receives."""
    if sample_id.startswith("arvo_"):
        arvo_id = sample_id[len("arvo_"):]
        path = REPO_ROOT / "external" / "cybergym_data_subset" / "data" / "arvo" / arvo_id / "repo-vul" / "src-vul"
    else:
        path = GT_RESULTS / sample_id / "_work" / "src"
    return path if path.is_dir() else None


def resume_role(role: str, workspace: Path, *, reason: str) -> RoleRun:
    """Re-enter a workspace whose session ended without writing its artifact.

    The original task is replayed verbatim from TASK.md so the role sees exactly
    what it saw before, prefixed with what went wrong and an instruction not to
    redo work whose notes are still on disk.
    """
    task = (workspace / "TASK.md").read_text(encoding="utf-8")
    prompt = (
        f"A previous session in this directory ended without a usable `{OUTPUT_NAME}` ({reason}).\n"
        "Its inputs, and any notes or scratch files it left, are still here. Do not start the\n"
        f"analysis over: read what is already in this directory, finish it, and write `{OUTPUT_NAME}`.\n\n"
        "The original task follows.\n\n" + task
    )
    return RoleRun(role, workspace, prompt, workspace / OUTPUT_NAME)


class _Workspace:
    """Builds a role directory and the index of what it contains.

    Every `put`/`copy` both places a file and records the line the role's prompt
    will use to find it, so the directory and its description cannot drift.
    """

    def __init__(self, root: Path) -> None:
        if root.exists():
            shutil.rmtree(root)
        self.root = root
        self.inputs = root / "inputs"
        self.inputs.mkdir(parents=True)
        self.index: list[tuple[str, str]] = []

    def put(self, name: str, payload: Any, description: str) -> None:
        write_json(self.inputs / name, payload)
        self.index.append((f"inputs/{name}", description))

    def copy(self, source: Path, name: str, description: str) -> bool:
        if not source.is_file():
            return False
        shutil.copy2(source, self.inputs / name)
        self.index.append((f"inputs/{name}", description))
        return True

    def copy_first(self, sources: list[Path], name: str, description: str) -> bool:
        return any(self.copy(source, name, description) for source in sources)

    def symlink(self, source: Path | None, name: str, description: str) -> bool:
        if source is None or not source.exists():
            return False
        target = self.inputs / name
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(source.resolve(), target_is_directory=source.is_dir())
        self.index.append((f"inputs/{name}/", description))
        return True

    def copy_files(self, source: Path, name: str, description: str, *, keep: tuple[str, ...]) -> None:
        if not source.is_dir():
            return
        copied = False
        for filename in keep:
            item = source / filename
            if item.is_file():
                target = self.inputs / name
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target / filename)
                copied = True
        if copied:
            self.index.append((f"inputs/{name}/", description))

    def copy_tree(self, source: Path, name: str, description: str, *, keep: tuple[str, ...]) -> None:
        if not source.is_dir():
            return
        copied = False
        for child in sorted(p for p in source.iterdir() if p.is_dir()):
            for filename in keep:
                if (child / filename).is_file():
                    target = self.inputs / name / child.name
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(child / filename, target / filename)
                    copied = True
        if copied:
            self.index.append((f"inputs/{name}/", description))

    def copy_glob_tree(self, source: Path, name: str, description: str, *, patterns: tuple[str, ...]) -> None:
        if not source.is_dir():
            return
        copied = False
        for pattern in patterns:
            for item in sorted(source.glob(pattern)):
                if item.is_file():
                    rel = item.relative_to(source)
                    target = self.inputs / name / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
                    copied = True
        if copied:
            self.index.append((f"inputs/{name}/", description))

    def copy_packet_as(self, packet: Path, name: str, description: str, *, snapshot_name: str | None = None) -> None:
        shutil.copytree(packet, self.inputs / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.index.append((f"inputs/{name}/", description))
        if snapshot_name:
            self.put(snapshot_name, lessons_snapshot(packet),
                     f"lesson ids and capacity for inputs/{name}/")

    def copy_packet(self, packet: Path) -> None:
        self.copy_packet_as(
            packet,
            "skill_packet",
            "the current packet as the agent receives it; read both SKILL.md files",
            snapshot_name="lessons_snapshot.json",
        )

    def finish(self, role: str, template: str) -> RoleRun:
        prompt = role_task_prompt(template, inputs=self.index, output_rel=OUTPUT_NAME)
        (self.root / "TASK.md").write_text(prompt, encoding="utf-8")
        return RoleRun(role, self.root, prompt, self.root / OUTPUT_NAME)


def build_diagnosis_role(
    workspace: Path,
    sample_id: str,
    results_dir: Path,
    eval_row: dict[str, Any] | None,
    outcome_record: dict[str, Any],
) -> RoleRun:
    space = _Workspace(workspace)
    sample_dir = results_dir / sample_id
    space.symlink(
        _source_tree_for_sample(sample_id),
        "source_tree",
        "the staged vulnerable source tree, matching /workspace/repo-vul/src-vul when available",
    )

    for name, description in GT_FILES:
        space.copy(GT_RESULTS / sample_id / name, name, description)
    try:
        space.put("reference_trajectory.json", synthesize_reference(sample_id),
                  "GT-assisted synthetic reference trajectory for this sample; label-side comparison only")
    except (OSError, ValueError, TypeError):
        pass
    space.copy(sample_dir / "analysis.json", "agent_analysis.json",
               "the agent's own analysis artifact for its last candidate")
    space.copy(sample_dir / "manifest.json", "agent_manifest.json",
               "run manifest: submissions, deduplication, stop reason")
    space.copy(sample_dir / "context_visit.json", "context_visit.json",
               "files and functions the coding agent inspected, reconstructed from its checkpoint")
    space.copy_first([sample_dir / rel for rel in TRAJECTORY_CANDIDATES], "trajectory.txt",
                     "the agent's full trajectory; read it with grep/sed, it is long")
    space.copy_files(sample_dir / "checkpoint", "checkpoint",
                     "checkpoint metadata: task/status, workspace listing, and analysis candidate provenance",
                     keep=CHECKPOINT_FILES)
    space.copy_glob_tree(sample_dir / "runs", "run_logs",
                         "adapter logs and model bridge logs for the coding-agent run",
                         patterns=RUN_LOG_PATTERNS)
    space.copy_tree(sample_dir / "submissions", "submissions",
                    "one directory per submitted candidate: request, candidate provenance, result, analysis, runtime output, and raw PoC bytes",
                    keep=SUBMISSION_FILES)

    row = eval_row or {}
    space.put("diagnostics_summary.json", diagnostics_summary(row),
              "the headline numbers of all three diagnostics side by side; start here")
    space.put("diagnostics/trace_coverage.json", row.get("fine_trace_coverage") or {},
              "what the agent WROTE, part 1: node and edge recall of its analysis.json "
              "fine_trace against the GT trace, plus per-step match reports")
    space.put("diagnostics/reasoning.json", row.get("reasoning") or {},
              "what the agent WROTE, part 2: whether its vuln_logic named the right source, "
              "safety obligation, sink and propagation, scored against the invariant graph")
    space.put("diagnostics/reachability.json", row.get("runtime") or {},
              "what the agent DID: how far each submitted PoC actually ran, as the R1-R5 ladder")
    space.put("deterministic_outcome.json", outcome_record,
              "the evaluator's verdict; treat it as fact")
    space.put("sample.json", {"sample_id": sample_id}, "the sample id to echo back")
    return space.finish("diagnose", "diagnostician.md")


def build_teacher_role(
    workspace: Path,
    packet: Path,
    diagnoses: list[dict[str, Any]],
    pools_view: dict[str, Any],
) -> RoleRun:
    space = _Workspace(workspace)
    space.copy_packet(packet)
    space.put("batch_diagnoses.json", diagnoses, "this batch's per-sample diagnoses")
    space.put("pools.json", pools_view,
              "accumulated sample-level failure, success, and infrastructure diagnoses")
    return space.finish("teacher", "teacher.md")


def build_curator_role(workspace: Path, packet: Path, candidate_updates: list[dict[str, Any]]) -> RoleRun:
    space = _Workspace(workspace)
    space.copy_packet(packet)
    space.put("candidate_updates.json", candidate_updates,
              "the Teacher's proposals with their evidence")
    return space.finish("curator", "curator.md")




def build_correction_role(
    workspace: Path,
    previous_packet: Path,
    current_packet: Path,
    batch_evaluation: dict[str, Any],
    batch_diagnoses: list[dict[str, Any]],
    applied_updates: dict[str, Any],
    pools_view: dict[str, Any],
    baseline_evaluations: list[dict[str, Any]] | None = None,
) -> RoleRun:
    space = _Workspace(workspace)
    space.copy_packet_as(previous_packet, "previous_skill",
                         "the packet used before the tentative update",
                         snapshot_name="previous_lessons_snapshot.json")
    space.copy_packet_as(current_packet, "current_skill",
                         "the tentative/current packet being corrected",
                         snapshot_name="current_lessons_snapshot.json")
    space.put("batch_evaluation.json", batch_evaluation,
              "current batch execution/evaluator results for the tentative/current packet")
    space.put("batch_diagnoses.json", batch_diagnoses,
              "per-sample diagnoses explaining the current batch outcomes")
    space.put("applied_updates.json", applied_updates,
              "updates that produced the tentative/current packet, if available")
    space.put("pools.json", pools_view,
              "accumulated success, failure, crash, and infrastructure pools")
    if baseline_evaluations:
        space.put("baseline_evaluations.json", baseline_evaluations,
                  "optional baseline or initial-skill evaluations for comparison")
    return space.finish("correction", "correction.md")


def execute_role(
    run: RoleRun,
    *,
    model: str,
    base_url: str,
    api_key_env: str,
    api_version: str = "2024-03-01-preview",
    reasoning_effort: str = "medium",
    max_output_tokens: int = 4096,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Run one role as a Codex subsession and return its status plus artifact."""
    codex_home = run.workspace / ".codex_home"
    codex_home.mkdir(parents=True, exist_ok=True)
    status = run_codex_subsession(SubsessionSpec(
        name=run.role,
        workspace=run.workspace,
        prompt=run.prompt,
        output_file=run.output_file,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        api_version=api_version,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
        timeout=timeout,
        bridge="modelhub_crawl" if "modelhub" in base_url else "auto",
        log_dir=run.workspace / "logs",
        extra_env={"CODEX_HOME": str(codex_home)},
    ))
    parsed = load_json_if_exists(run.output_file) if status["status"] == "ok" else None
    if status["status"] == "ok" and not isinstance(parsed, dict):
        status["status"] = "invalid_output"
        parsed = None
    # The workspace carries its own last outcome, so a later run can decide
    # whether to reuse, resume, or start over without consulting anything else.
    write_json(run.workspace / STATUS_NAME, {k: v for k, v in status.items() if k != "parsed"})
    status["parsed"] = parsed
    return status
