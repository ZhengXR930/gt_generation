from __future__ import annotations

import binascii
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import reachability_core
from recorder_core.core import load_reasoning_events, reduce_records
from reachability_core.core import (
    load_hits,
    parse_sanitizer_trace,
    CommandResult as GdbCommandResult,
    run_gdb_reachability,
    write_breakpoint_spec,
)


@dataclass
class CommandResult:
    command: str
    cwd: str
    returncode: int
    stdout: str
    stderr: str


def record_candidate_plan(
    *,
    plan: dict[str, Any],
    workspace: Path,
    reasoning_events_path: Path | None = None,
    reasoning_state_path: Path | None = None,
    allow_unguided: bool = False,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    reasoning_events_path = reasoning_events_path.resolve() if reasoning_events_path else None
    reasoning_state_path = reasoning_state_path.resolve() if reasoning_state_path else None
    workspace.mkdir(parents=True, exist_ok=True)
    plans_path = workspace / "candidate_plans.jsonl"
    existing = _read_jsonl(plans_path)
    plan_id = str(plan.get("plan_id") or f"plan_{len(existing) + 1:04d}")
    normalized = _normalize_plan(plan, plan_id)
    format_memory = _format_memory_for_plan(normalized)
    if format_memory.get("matches"):
        normalized["format_memory"] = format_memory
    if not normalized.get("construction_support_ids"):
        latest_support = _latest_jsonl_record(workspace / "construction_support_requests.jsonl")
        if latest_support and latest_support.get("accepted") is True:
            normalized["construction_support_ids"] = [
                str(latest_support.get("support_id") or "")
            ]
    latest_feedback = _latest_failed_construction_feedback(workspace)
    if latest_feedback:
        normalized["latest_feedback_to_address"] = latest_feedback
    require_complete = _env_flag("CANDIDATE_SYNTHESIS_REQUIRE_COMPLETE_REASONING", False)
    reasoning_check = _validate_reasoning_link(
        normalized,
        reasoning_events_path=reasoning_events_path,
        reasoning_state_path=reasoning_state_path,
        allow_unguided=allow_unguided,
        require_complete=require_complete,
        require_minimal=not require_complete,
    )
    errors = _validate_plan(normalized)
    errors.extend(_validate_builder_executable(normalized, workspace))
    errors.extend(_validate_known_format_output_contract(normalized))
    if latest_feedback and not _plan_addresses_feedback(normalized, latest_feedback):
        errors.append(
            "candidate plan must include previous_feedback or feedback_response "
            f"addressing prior feedback stage {latest_feedback.get('stage')}"
        )
    errors.extend(_validate_plan_progress_after_feedback(normalized, workspace, latest_feedback))
    errors.extend(reasoning_check["errors"])
    normalized["guided"] = reasoning_check["guided"]
    normalized["reasoning_link"] = reasoning_check["reasoning_link"]
    normalized["reasoning_complete"] = reasoning_check["reasoning_complete"]
    normalized["reasoning_state_snapshot"] = reasoning_check["reasoning_state_snapshot"]
    normalized["accepted"] = not errors
    normalized["errors"] = errors
    normalized["created_at"] = _now()
    if normalized["accepted"]:
        _append_jsonl(plans_path, normalized)
    return {
        "accepted": normalized["accepted"],
        "plan_id": plan_id,
        "errors": errors,
        "guided": normalized["guided"],
        "plan": normalized,
        "artifacts": {"plans": str(plans_path)},
    }


def record_construction_support_request(
    *,
    request: dict[str, Any],
    workspace: Path,
    reasoning_events_path: Path | None = None,
    reasoning_state_path: Path | None = None,
    allow_unguided: bool = False,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    reasoning_events_path = reasoning_events_path.resolve() if reasoning_events_path else None
    reasoning_state_path = reasoning_state_path.resolve() if reasoning_state_path else None
    workspace.mkdir(parents=True, exist_ok=True)
    requests_path = workspace / "construction_support_requests.jsonl"
    existing = _read_jsonl(requests_path)
    support_id = str(request.get("support_id") or f"support_{len(existing) + 1:04d}")
    normalized = _normalize_support_request(request, support_id)
    reasoning_check = _validate_reasoning_link(
        normalized,
        reasoning_events_path=reasoning_events_path,
        reasoning_state_path=reasoning_state_path,
        allow_unguided=allow_unguided,
        require_complete=False,
    )
    errors = _validate_support_request(normalized)
    errors.extend(reasoning_check["errors"])
    normalized["guided"] = reasoning_check["guided"]
    normalized["reasoning_link"] = reasoning_check["reasoning_link"]
    normalized["reasoning_complete"] = reasoning_check["reasoning_complete"]
    normalized["reasoning_state_snapshot"] = reasoning_check["reasoning_state_snapshot"]
    normalized["external_memory"] = lookup_format_memory(
        format_or_protocol=normalized.get("format_or_protocol") or "",
        query=" ".join(
            [
                normalized.get("construction_goal") or "",
                " ".join(str(item) for item in normalized.get("needed_knowledge") or []),
                " ".join(str(item) for item in normalized.get("known_constraints") or []),
            ]
        ),
    )
    normalized["accepted"] = not errors
    normalized["errors"] = errors
    normalized["created_at"] = _now()
    if normalized["accepted"]:
        _append_jsonl(requests_path, normalized)
    return {
        "accepted": normalized["accepted"],
        "support_id": support_id,
        "errors": errors,
        "guided": normalized["guided"],
        "request": normalized,
        "external_memory": normalized["external_memory"],
        "artifacts": {"requests": str(requests_path)},
    }


def lookup_format_memory(
    *,
    format_or_protocol: str,
    query: str = "",
    memory_dir: Path | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    """Retrieve generic format-construction memories.

    The memory store is intentionally GT-free: records describe public/generic
    format structure and builder constraints, not target PoCs or vulnerability
    trigger bytes.
    """
    records = _load_format_memory(memory_dir)
    query_terms = _memory_terms(f"{format_or_protocol} {query}")
    matches: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        terms = _memory_terms(
            " ".join(
                [
                    str(record.get("format_or_protocol") or ""),
                    " ".join(str(alias) for alias in record.get("aliases") or []),
                    " ".join(str(note) for note in record.get("construction_notes") or []),
                    json.dumps(record.get("builder_guidance") or {}, ensure_ascii=False),
                ]
            )
        )
        score = len(query_terms & terms)
        normalized_format = str(format_or_protocol or "").strip().lower()
        aliases = [str(alias).strip().lower() for alias in record.get("aliases") or []]
        canonical = str(record.get("format_or_protocol") or "").strip().lower()
        if normalized_format and (
            normalized_format == canonical or normalized_format in aliases
        ):
            score += 20
        if score > 0:
            matches.append((score, record))
    matches.sort(key=lambda item: item[0], reverse=True)
    selected = [record for _, record in matches[: max(1, limit)]]
    return {
        "query": {"format_or_protocol": format_or_protocol, "text": query},
        "policy": {
            "scope": "generic_format_construction_only",
            "disallowed": [
                "target CVE PoC",
                "hidden benchmark sample",
                "vulnerability-specific trigger bytes",
            ],
        },
        "matches": selected,
    }


def build_candidate(
    *,
    plan_id: str,
    workspace: Path,
    timeout: int = 120,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if not plan_id:
        latest_plan = _latest_jsonl_record(workspace / "candidate_plans.jsonl")
        plan_id = str((latest_plan or {}).get("plan_id") or "")
    plan = _find_jsonl_record(workspace / "candidate_plans.jsonl", "plan_id", plan_id)
    if not plan:
        return {"built": False, "candidate_id": "", "errors": [f"unknown plan_id: {plan_id}"]}
    candidate_id = f"cand_{_next_index(workspace / 'candidates') :04d}"
    candidate_dir = workspace / "candidates" / candidate_id
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = candidate_dir / str(plan.get("candidate_filename") or "candidate_input")
    errors: list[str] = []
    command_result: CommandResult | None = None
    try:
        builder = plan.get("builder") or {}
        kind = str(builder.get("kind") or "seed_edits")
        plan_snapshot_path = _write_plan_snapshot(candidate_dir, plan)
        if kind == "external_command":
            command = _format_command(
                str(builder.get("command") or ""),
                candidate_path=candidate_path,
                seed_path=_seed_path(plan),
                plan_path=plan_snapshot_path,
            )
            if not command:
                errors.append("external_command builder requires builder.command")
            else:
                command_result, stdout_bytes = _run_command_with_bytes(
                    command, cwd=workspace, timeout=timeout
                )
                if command_result.returncode != 0:
                    errors.append("external_command builder failed")
                if not candidate_path.exists():
                    discovered = _discover_candidate_output(workspace, candidate_path)
                    if discovered:
                        shutil.copyfile(discovered, candidate_path)
                    elif stdout_bytes:
                        candidate_path.write_bytes(stdout_bytes)
                    else:
                        errors.append("external_command did not create candidate_path")
        else:
            data = _initial_candidate_bytes(plan)
            data = _apply_edits(data, plan.get("edits") or [])
            candidate_path.write_bytes(data)
        artifact_validation = _validate_candidate_artifact(
            plan=plan,
            candidate_path=candidate_path,
            workspace=workspace,
            seed_path=_seed_path(plan),
            plan_path=plan_snapshot_path,
            timeout=timeout,
        )
        errors.extend(artifact_validation["errors"])
    except Exception as exc:  # noqa: BLE001 - convert tool failures to report JSON.
        errors.append(f"{type(exc).__name__}: {exc}")
        artifact_validation = {"errors": [], "checks": {}}

    metadata = {
        "candidate_id": candidate_id,
        "plan_id": plan_id,
        "candidate_path": str(candidate_path),
        "built": not errors,
        "errors": errors,
        "builder": plan.get("builder") or {},
        "construction_strategy": plan.get("construction_strategy") or {},
        "construction_support_ids": plan.get("construction_support_ids") or [],
        "reasoning_link": plan.get("reasoning_link") or {},
        "reasoning_complete": bool(plan.get("reasoning_complete")),
        "reasoning_state_snapshot": plan.get("reasoning_state_snapshot") or {},
        "artifact_validation": artifact_validation,
        "created_at": _now(),
    }
    if command_result is not None:
        metadata["builder_command"] = asdict(command_result)
    _write_json(candidate_dir / "candidate.json", metadata)
    _append_jsonl(workspace / "candidates.jsonl", metadata)
    return metadata


def adopt_candidate(
    *,
    candidate_path: Path,
    workspace: Path,
    reasoning_events_path: Path | None = None,
    reasoning_state_path: Path | None = None,
    allow_unguided: bool = False,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    candidate_path = candidate_path.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if not candidate_path.exists():
        return {
            "built": False,
            "candidate_id": "",
            "errors": [f"candidate_path does not exist: {candidate_path}"],
        }

    plan_id = f"adopted_plan_{_next_index(workspace / 'adopted_plans') :04d}"
    plan = {
        "plan_id": plan_id,
        "stage": "adopt_existing_candidate",
        "hypothesis": {"summary": "Adopt candidate produced by the coding agent before submit."},
        "target_input_component": {"description": "Existing candidate file submitted by the coding agent."},
        "construction_strategy": {
            "mode": "direct",
            "description": "The agent constructed the candidate outside the builder tool; the harness binds it to the current reasoning state at submit time.",
        },
        "expected_effect": {"description": "Use submit_candidate and H1-H5 feedback to diagnose reachability."},
        "builder": {"kind": "adopt_existing_file", "origin_path": str(candidate_path)},
        "candidate_filename": candidate_path.name or "candidate_input",
    }
    reasoning_check = _validate_reasoning_link(
        plan,
        reasoning_events_path=reasoning_events_path.resolve() if reasoning_events_path else None,
        reasoning_state_path=reasoning_state_path.resolve() if reasoning_state_path else None,
        allow_unguided=allow_unguided,
        require_complete=False,
        require_minimal=False,
    )
    errors = reasoning_check["errors"]
    plan["guided"] = reasoning_check["guided"]
    plan["reasoning_link"] = reasoning_check["reasoning_link"]
    plan["reasoning_complete"] = reasoning_check["reasoning_complete"]
    plan["reasoning_state_snapshot"] = reasoning_check["reasoning_state_snapshot"]
    plan["accepted"] = not errors
    plan["errors"] = errors
    plan["created_at"] = _now()
    if errors:
        return {
            "built": False,
            "candidate_id": "",
            "plan_id": plan_id,
            "errors": errors,
            "plan": plan,
        }

    _append_jsonl(workspace / "candidate_plans.jsonl", plan)
    candidate_id = f"cand_{_next_index(workspace / 'candidates') :04d}"
    candidate_dir = workspace / "candidates" / candidate_id
    candidate_dir.mkdir(parents=True, exist_ok=True)
    adopted_path = candidate_dir / plan["candidate_filename"]
    if candidate_path != adopted_path:
        shutil.copyfile(candidate_path, adopted_path)

    metadata = {
        "candidate_id": candidate_id,
        "plan_id": plan_id,
        "candidate_path": str(adopted_path),
        "built": True,
        "adopted": True,
        "origin_path": str(candidate_path),
        "origin_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        "errors": [],
        "builder": plan["builder"],
        "construction_strategy": plan["construction_strategy"],
        "construction_support_ids": [],
        "reasoning_link": plan.get("reasoning_link") or {},
        "reasoning_complete": bool(plan.get("reasoning_complete")),
        "reasoning_state_snapshot": plan.get("reasoning_state_snapshot") or {},
        "created_at": _now(),
    }
    _write_json(candidate_dir / "candidate.json", metadata)
    _append_jsonl(workspace / "candidates.jsonl", metadata)
    return metadata


def run_candidate(
    *,
    candidate_id: str,
    workspace: Path,
    run_command: str,
    debug_command: str = "",
    probes: list[dict[str, Any]] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    candidate = _load_candidate(workspace, candidate_id)
    if not candidate:
        return {"ran": False, "errors": [f"unknown candidate_id: {candidate_id}"]}
    candidate_path = Path(candidate["candidate_path"])
    run_id = f"run_{_next_index(workspace / 'runs') :04d}"
    run_dir = workspace / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    command = _format_command(run_command, candidate_path=candidate_path)
    result = _run_command(command, cwd=workspace, timeout=timeout)
    trace = result.stdout + "\n" + result.stderr
    trace_path = run_dir / "run_trace.txt"
    trace_path.write_text(trace, encoding="utf-8", errors="replace")
    sanitizer = parse_sanitizer_trace(trace)

    probe_hits: list[dict[str, Any]] = []
    gdb_result: CommandResult | None = None
    if debug_command and probes:
        breakpoints_path = run_dir / "probe_breakpoints.json"
        hits_path = run_dir / "probe_hits.json"
        write_breakpoint_spec(_probe_breakpoints(probes), breakpoints_path)
        gdb = run_gdb_reachability(
            command=_format_command(debug_command, candidate_path=candidate_path),
            breakpoints_path=breakpoints_path,
            hits_path=hits_path,
            gdb_script=Path(reachability_core.__file__).resolve().parent / "gdb_reachability.py",
            timeout=timeout,
        )
        gdb_result = CommandResult(
            command=" ".join(shlex.quote(part) for part in gdb.command),
            cwd=str(workspace),
            returncode=gdb.returncode,
            stdout=gdb.stdout,
            stderr=gdb.stderr,
        )
        (run_dir / "gdb_stdout.txt").write_text(gdb.stdout, encoding="utf-8", errors="replace")
        (run_dir / "gdb_stderr.txt").write_text(gdb.stderr, encoding="utf-8", errors="replace")
        probe_hits = load_hits(hits_path)

    delivery = _classify_delivery(result)
    report = {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "plan_id": candidate.get("plan_id"),
        "ran": True,
        "delivered": delivery["delivered"],
        "delivery_error": delivery["delivery_error"],
        "returncode": result.returncode,
        "sanitizer_crash": bool(sanitizer.get("crash_type")),
        "sanitizer_observed": sanitizer,
        "probes_requested": probes or [],
        "probes_reached": _summarize_probe_hits(probe_hits),
        "trace_path": str(trace_path),
        "command": asdict(result),
        "created_at": _now(),
    }
    if gdb_result is not None:
        report["debug_command"] = asdict(gdb_result)
        report["probe_hits"] = probe_hits
    _write_json(run_dir / "candidate_run.json", report)
    _append_jsonl(workspace / "candidate_runs.jsonl", report)
    return report


def submit_candidate(
    *,
    candidate_id: str,
    workspace: Path,
    submit_command: str,
    timeout: int = 120,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    if not candidate_id:
        latest_candidate = _latest_jsonl_record(workspace / "candidates.jsonl")
        candidate_id = str((latest_candidate or {}).get("candidate_id") or "")
    candidate = _load_candidate(workspace, candidate_id)
    if not candidate:
        return {"submitted": False, "errors": [f"unknown candidate_id: {candidate_id}"]}
    if candidate.get("built") is not True:
        return {
            "submitted": False,
            "candidate_id": candidate_id,
            "plan_id": candidate.get("plan_id"),
            "errors": ["candidate was not built successfully; revise plan and rebuild before submit"],
        }
    candidate_path = Path(candidate["candidate_path"])
    submit_id = f"submit_{_next_index(workspace / 'submissions') :04d}"
    submit_dir = workspace / "submissions" / submit_id
    submit_dir.mkdir(parents=True, exist_ok=True)
    reachability_warnings = _missing_required_reachability_config()
    command = _format_command(submit_command, candidate_path=candidate_path)
    submit_server = os.environ.get("CANDIDATE_SYNTHESIS_SUBMIT_SERVER")
    if submit_server:
        command = f"CYBERGYM_SUBMIT_SERVER={shlex.quote(submit_server)} {command}"
    result = _run_command(command, cwd=workspace, timeout=timeout)
    output = result.stdout + "\n" + result.stderr
    delivery = _classify_delivery(result)
    sanitizer = parse_sanitizer_trace(output)
    cybergym_response = _parse_cybergym_submit_response(output)
    reachability = _optional_reachability_feedback(
        workspace=workspace,
        submit_dir=submit_dir,
        candidate_path=candidate_path,
        reasoning_state=candidate.get("reasoning_state_snapshot") or {},
        submit_output=output,
        timeout=timeout,
    )
    construction_feedback = _construction_feedback(
        result=result,
        delivery=delivery,
        sanitizer=sanitizer,
        cybergym_response=cybergym_response,
        reachability=reachability,
    )
    (submit_dir / "submit_output.txt").write_text(output, encoding="utf-8", errors="replace")
    report = {
        "submit_id": submit_id,
        "candidate_id": candidate_id,
        "plan_id": candidate.get("plan_id"),
        "submitted": True,
        "delivered": delivery["delivered"],
        "delivery_error": delivery["delivery_error"],
        "success": construction_feedback["stage"] == "H5",
        "sanitizer_crash": bool(sanitizer.get("crash_type")),
        "sanitizer_observed": sanitizer,
        "cybergym_response": cybergym_response,
        "reasoning_link": candidate.get("reasoning_link") or {},
        "reasoning_complete": bool(candidate.get("reasoning_complete")),
        "reasoning_state_snapshot": candidate.get("reasoning_state_snapshot") or {},
        "reachability_feedback": reachability.get("agent_feedback") if reachability else {},
        "construction_feedback": construction_feedback,
        "configuration_warnings": reachability_warnings,
        "returncode": result.returncode,
        "command": asdict(result),
        "output_excerpt": output[:4000],
        "created_at": _now(),
    }
    if reachability:
        report["reachability_artifact"] = reachability.get("artifact", "")
    _write_json(submit_dir / "candidate_submission.json", report)
    _append_jsonl(workspace / "candidate_submissions.jsonl", report)
    return report


def _parse_cybergym_submit_response(output: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for idx, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (
            "exit_code" in value or "output" in value or "status" in value
        ):
            candidates.append(value)
    return candidates[-1] if candidates else {}


def _optional_reachability_feedback(
    *,
    workspace: Path,
    submit_dir: Path,
    candidate_path: Path,
    reasoning_state: dict[str, Any],
    submit_output: str,
    timeout: int,
) -> dict[str, Any]:
    reach_dir = submit_dir / "reachability"
    reach_dir.mkdir(parents=True, exist_ok=True)
    debug_command = os.environ.get("CANDIDATE_SYNTHESIS_REACHABILITY_DEBUG_COMMAND", "")
    coverage_command = os.environ.get("CANDIDATE_SYNTHESIS_REACHABILITY_COVERAGE_COMMAND", "")
    sanitizer_command = os.environ.get("CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND", "")
    hits: list[dict[str, Any]] = []
    sanitizer_text = submit_output
    breakpoints = _extract_hypothesis_checkpoints(reasoning_state)

    if debug_command and not coverage_command:
        breakpoints_path = reach_dir / "reachability_breakpoints.json"
        hits_path = reach_dir / "reachability_hits.json"

        write_breakpoint_spec(breakpoints, breakpoints_path)
        gdb_script = Path(reachability_core.__file__).resolve().parent / "gdb_reachability.py"
        gdb = _run_hypothesis_debug_command(
            command_template=debug_command,
            candidate_path=candidate_path,
            breakpoints_path=breakpoints_path,
            hits_path=hits_path,
            gdb_script=gdb_script,
            timeout=timeout,
            workspace=workspace,
        )
        (reach_dir / "gdb_stdout.txt").write_text(gdb.stdout, encoding="utf-8", errors="replace")
        (reach_dir / "gdb_stderr.txt").write_text(gdb.stderr, encoding="utf-8", errors="replace")
        hits = load_hits(hits_path)

    if coverage_command:
        coverage_path = reach_dir / "coverage.json"
        coverage_result = _run_command(
            _format_coverage_command(
                coverage_command,
                candidate_path=candidate_path,
                coverage_path=coverage_path,
            ),
            cwd=workspace,
            timeout=timeout,
        )
        (reach_dir / "coverage_stdout.txt").write_text(
            coverage_result.stdout,
            encoding="utf-8",
            errors="replace",
        )
        (reach_dir / "coverage_stderr.txt").write_text(
            coverage_result.stderr,
            encoding="utf-8",
            errors="replace",
        )
        coverage_hits = _coverage_hits_from_export(
            coverage_path=coverage_path,
            checkpoints=breakpoints,
        )
        coverage_diagnostics = _coverage_diagnostics_from_export(
            coverage_path=coverage_path,
            checkpoints=breakpoints,
        )
        hits = _merge_hits(hits, coverage_hits)
    else:
        coverage_diagnostics = {}

    if sanitizer_command:
        sanitizer_result = _run_command(
            _format_command(sanitizer_command, candidate_path=candidate_path),
            cwd=workspace,
            timeout=timeout,
        )
        sanitizer_text = sanitizer_result.stdout + "\n" + sanitizer_result.stderr
        (reach_dir / "sanitizer_trace.txt").write_text(
            sanitizer_text,
            encoding="utf-8",
            errors="replace",
        )

    full = _evaluate_h1_h5(
        reasoning_state=reasoning_state,
        checkpoints=breakpoints,
        hits=hits,
        sanitizer_trace=sanitizer_text,
        coverage_diagnostics=coverage_diagnostics,
    )
    (reach_dir / "hypothesis_reachability_report.json").write_text(
        json.dumps(full, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "artifact": str(reach_dir / "hypothesis_reachability_report.json"),
        "agent_feedback": _agent_hypothesis_feedback(full),
    }


def _run_hypothesis_debug_command(
    *,
    command_template: str,
    candidate_path: Path,
    breakpoints_path: Path,
    hits_path: Path,
    gdb_script: Path,
    timeout: int,
    workspace: Path,
) -> GdbCommandResult:
    formatted = _format_debug_wrapper_command(
        command_template,
        candidate_path=candidate_path,
        breakpoints_path=breakpoints_path,
        hits_path=hits_path,
        gdb_script=gdb_script,
    )
    if _is_debug_wrapper_command(command_template):
        proc = subprocess.run(
            formatted,
            shell=True,
            text=True,
            capture_output=True,
            cwd=workspace,
            timeout=timeout,
            executable=_shell_executable(),
            env=_command_env(),
        )
        return GdbCommandResult(
            command=["/bin/sh", "-lc", formatted],
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    return run_gdb_reachability(
        command=_format_command(command_template, candidate_path=candidate_path),
        breakpoints_path=breakpoints_path,
        hits_path=hits_path,
        gdb_script=gdb_script,
        timeout=timeout,
    )


def _is_debug_wrapper_command(command_template: str) -> bool:
    return any(
        token in command_template
        for token in (
            "{breakpoints_path}",
            "{breakpoints_dir}",
            "{hits_path}",
            "{hits_dir}",
            "{gdb_script}",
        )
    )


def _format_debug_wrapper_command(
    template: str,
    *,
    candidate_path: Path,
    breakpoints_path: Path,
    hits_path: Path,
    gdb_script: Path,
) -> str:
    result = _format_command(template, candidate_path=candidate_path)
    replacements = {
        "breakpoints_path": str(breakpoints_path),
        "breakpoints_dir": str(breakpoints_path.parent),
        "hits_path": str(hits_path),
        "hits_dir": str(hits_path.parent),
        "gdb_script": str(gdb_script),
    }
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", shlex.quote(value))
    return result


def _format_coverage_command(
    template: str,
    *,
    candidate_path: Path,
    coverage_path: Path,
) -> str:
    result = _format_command(template, candidate_path=candidate_path)
    replacements = {
        "coverage_path": str(coverage_path),
        "coverage_dir": str(coverage_path.parent),
    }
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", shlex.quote(value))
    return result


def _coverage_hits_from_export(
    *,
    coverage_path: Path,
    checkpoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not coverage_path.exists():
        return []
    try:
        data = json.loads(coverage_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    covered_lines_by_file = _covered_lines_by_file(data)
    hits: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        expected_file = str(checkpoint.get("file") or "")
        expected_function = str(checkpoint.get("function") or "")
        expected_line = _safe_int(checkpoint.get("line"))
        matched_file, covered_lines = _match_covered_file(expected_file, covered_lines_by_file)
        if expected_line is not None:
            reached = expected_line in covered_lines
        else:
            reached = bool(matched_file) and (
                not expected_function or _function_covered(data, matched_file, expected_function)
            )
        if reached:
            hits.append({
                "kind": checkpoint.get("kind"),
                "expected_file": expected_file,
                "expected_function": expected_function,
                "expected_line": expected_line,
                "file": matched_file,
                "function": expected_function,
                "line": expected_line,
                "hit_count": 1,
                "source": "llvm_cov",
            })
    return hits


def _coverage_diagnostics_from_export(
    *,
    coverage_path: Path,
    checkpoints: list[dict[str, Any]],
) -> dict[str, Any]:
    if not coverage_path.exists():
        return {}
    try:
        data = json.loads(coverage_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    covered_lines_by_file = _covered_lines_by_file(data)
    covered_functions_by_file = _covered_functions_by_file(data)
    files: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for checkpoint in checkpoints:
        expected_file = str(checkpoint.get("file") or "")
        expected_line = _safe_int(checkpoint.get("line"))
        matched_file, covered_lines = _match_covered_file(expected_file, covered_lines_by_file)
        if not matched_file or matched_file in seen_files:
            continue
        seen_files.add(matched_file)
        sorted_lines = sorted(covered_lines)
        nearest_before = None
        nearest_after = None
        if expected_line is not None:
            before = [line for line in sorted_lines if line <= expected_line]
            after = [line for line in sorted_lines if line >= expected_line]
            nearest_before = before[-1] if before else None
            nearest_after = after[0] if after else None
        files.append(
            {
                "expected_file": expected_file,
                "matched_file": matched_file,
                "covered_line_count": len(sorted_lines),
                "covered_line_min": sorted_lines[0] if sorted_lines else None,
                "covered_line_max": sorted_lines[-1] if sorted_lines else None,
                "expected_line": expected_line,
                "expected_line_reached": expected_line in covered_lines
                if expected_line is not None
                else None,
                "nearest_covered_line_before_or_at": nearest_before,
                "nearest_covered_line_after_or_at": nearest_after,
                "nearest_covered_function_before_or_at": _function_name_for_line(
                    covered_functions_by_file.get(matched_file) or [],
                    nearest_before,
                ),
                "nearest_covered_function_after_or_at": _function_name_for_line(
                    covered_functions_by_file.get(matched_file) or [],
                    nearest_after,
                ),
                "nearest_covered_code_before_or_at": _source_line_for_covered_file(
                    coverage_path,
                    matched_file,
                    nearest_before,
                ),
                "nearest_covered_code_after_or_at": _source_line_for_covered_file(
                    coverage_path,
                    matched_file,
                    nearest_after,
                ),
            }
        )
    return {"files": files} if files else {}


def _covered_lines_by_file(data: dict[str, Any]) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for item in data.get("data") or []:
        for file_record in item.get("files") or []:
            filename = str(file_record.get("filename") or "")
            if not filename:
                continue
            covered: set[int] = set()
            for segment in file_record.get("segments") or []:
                if not isinstance(segment, list) or len(segment) < 3:
                    continue
                line = _safe_int(segment[0])
                count = _safe_int(segment[2])
                if line is not None and count and count > 0:
                    covered.add(line)
            result[filename] = covered
    return result


def _covered_functions_by_file(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in data.get("data") or []:
        for function in item.get("functions") or []:
            if not isinstance(function, dict):
                continue
            if _safe_int(function.get("count")) in (None, 0):
                continue
            name = str(function.get("name") or "")
            filenames = [
                str(filename)
                for filename in function.get("filenames") or []
                if str(filename)
            ]
            regions = function.get("regions") or []
            for file_index, filename in enumerate(filenames):
                ranges = []
                for region in regions:
                    if not isinstance(region, list) or len(region) < 3:
                        continue
                    region_file_index = _safe_int(region[5]) if len(region) > 5 else 0
                    if region_file_index not in (None, file_index):
                        continue
                    start = _safe_int(region[0])
                    end = _safe_int(region[2])
                    if start is None or end is None:
                        continue
                    ranges.append((min(start, end), max(start, end)))
                if ranges:
                    result.setdefault(filename, []).append(
                        {"name": name, "ranges": ranges}
                    )
    return result


def _function_name_for_line(functions: list[dict[str, Any]], line: int | None) -> str:
    if line is None:
        return ""
    for function in functions:
        for start, end in function.get("ranges") or []:
            if start <= line <= end:
                return str(function.get("name") or "")
    return ""


def _source_line_for_covered_file(
    coverage_path: Path,
    matched_file: str,
    line: int | None,
) -> str:
    if line is None or not matched_file:
        return ""
    source_path = Path(matched_file)
    if not source_path.exists():
        source_path = _find_local_source_for_coverage_file(coverage_path, matched_file)
    if not source_path.exists():
        return ""
    try:
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


def _find_local_source_for_coverage_file(coverage_path: Path, matched_file: str) -> Path:
    parts = [part for part in Path(matched_file).parts if part not in {"/", ""}]
    suffixes = []
    for start in range(len(parts)):
        suffix = Path(*parts[start:])
        if len(suffix.parts) >= 2:
            suffixes.append(suffix)
    for base in [coverage_path.parent, *coverage_path.parents]:
        for suffix in suffixes:
            for prefix in (
                base,
                base / "repo-vul" / "src-vul",
                base / "workspace" / "repo-vul" / "src-vul",
            ):
                candidate = prefix / suffix
                if candidate.exists():
                    return candidate
    return Path(matched_file)


def _function_covered(data: dict[str, Any], matched_file: str, function: str) -> bool:
    if not function:
        return False
    for item in data.get("data") or []:
        for fn in item.get("functions") or []:
            name = str(fn.get("name") or "")
            if function not in name:
                continue
            filenames = [str(value or "") for value in fn.get("filenames") or []]
            if not filenames or any(_same_or_suffix_file(matched_file, filename) for filename in filenames):
                counts = fn.get("count")
                if isinstance(counts, int):
                    return counts > 0
                return True
    return False


def _match_covered_file(
    expected_file: str,
    covered_lines_by_file: dict[str, set[int]],
) -> tuple[str, set[int]]:
    for covered_file, lines in covered_lines_by_file.items():
        if _same_or_suffix_file(expected_file, covered_file):
            return covered_file, lines
    return "", set()


def _same_or_suffix_file(left: str, right: str) -> bool:
    left_norm = left.replace("\\", "/").strip("/")
    right_norm = right.replace("\\", "/").strip("/")
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    return left_norm.endswith("/" + right_norm) or right_norm.endswith("/" + left_norm) or (
        left_norm.split("/")[-1] == right_norm.split("/")[-1]
    )


def _merge_hits(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for group in groups:
        for hit in group:
            key = (
                hit.get("kind"),
                hit.get("file"),
                hit.get("function"),
                hit.get("line"),
                hit.get("source"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
    return merged


def _missing_required_reachability_config() -> list[str]:
    missing: list[str] = []
    debug_command = os.environ.get("CANDIDATE_SYNTHESIS_REACHABILITY_DEBUG_COMMAND")
    coverage_command = os.environ.get("CANDIDATE_SYNTHESIS_REACHABILITY_COVERAGE_COMMAND")
    sanitizer_command = os.environ.get("CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND")
    if not debug_command and not coverage_command:
        missing.append("CANDIDATE_SYNTHESIS_REACHABILITY_DEBUG_COMMAND or CANDIDATE_SYNTHESIS_REACHABILITY_COVERAGE_COMMAND is not configured; H1-H4 feedback may be coarse")
    if not sanitizer_command:
        missing.append("CANDIDATE_SYNTHESIS_REACHABILITY_SANITIZER_COMMAND is not configured; H5 feedback will rely on submit output")
    return missing


def _extract_hypothesis_checkpoints(reasoning_state: dict[str, Any]) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    source = _state_location(reasoning_state, "source")
    if isinstance(source, dict):
        checkpoint = _checkpoint_from_location(source, "claimed_source")
        if checkpoint:
            checkpoints.append(checkpoint)

    root = _state_location(reasoning_state, "root_cause")
    if isinstance(root, dict):
        root_function = _checkpoint_from_location(root, "claimed_root_cause_function")
        if root_function:
            root_function["line"] = None
            checkpoints.append(root_function)
        root_line = _checkpoint_from_location(root, "claimed_root_cause_line")
        if root_line:
            checkpoints.append(root_line)

    sink = _state_location(reasoning_state, "sink")
    if isinstance(sink, dict):
        sink_function = _checkpoint_from_location(sink, "claimed_sink_function")
        if sink_function:
            sink_function["line"] = None
            checkpoints.append(sink_function)
        sink_line = _checkpoint_from_location(sink, "claimed_sink_line")
        if sink_line:
            checkpoints.append(sink_line)

    for index, step in enumerate(reasoning_state.get("trace") or [], start=1):
        if not isinstance(step, dict):
            continue
        checkpoint = _checkpoint_from_location(step, "claimed_trace_step")
        if checkpoint:
            checkpoint["step"] = step.get("step") or index
            checkpoints.append(checkpoint)
    return _dedupe_hypothesis_checkpoints(checkpoints)


def _checkpoint_from_location(location: dict[str, Any], kind: str) -> dict[str, Any]:
    file = str(location.get("file") or "").strip()
    function = str(location.get("function") or "").strip()
    line = _safe_int(location.get("line"))
    if not file and not function:
        return {}
    return {
        "kind": kind,
        "file": file,
        "function": function,
        "line": line,
        "code": str(location.get("code") or "").strip(),
        "note": str(location.get("note") or location.get("description") or "").strip(),
    }


def _dedupe_hypothesis_checkpoints(checkpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        key = (
            checkpoint.get("kind"),
            checkpoint.get("file"),
            checkpoint.get("function"),
            checkpoint.get("line"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(checkpoint)
    return result


def _evaluate_h1_h5(
    *,
    reasoning_state: dict[str, Any],
    checkpoints: list[dict[str, Any]],
    hits: list[dict[str, Any]],
    sanitizer_trace: str,
    coverage_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hit_kinds = {
        str(hit.get("kind"))
        for hit in hits
        if not hit.get("breakpoint_error") and not hit.get("run_error")
    }
    checkpoint_kinds = {str(checkpoint.get("kind")) for checkpoint in checkpoints}
    sanitizer_observed = parse_sanitizer_trace(sanitizer_trace or "") if sanitizer_trace else {}
    hit_kinds.update(_sanitizer_stack_hit_kinds(checkpoints, sanitizer_observed))
    h1 = _kind_reached("claimed_source", checkpoint_kinds, hit_kinds)
    h2_root = _kind_reached("claimed_root_cause_function", checkpoint_kinds, hit_kinds)
    h2_sink = _kind_reached("claimed_sink_function", checkpoint_kinds, hit_kinds)
    h2 = _bool_or_none(h2_root, h2_sink)
    h3 = _kind_reached("claimed_root_cause_line", checkpoint_kinds, hit_kinds)
    h4 = _kind_reached("claimed_sink_line", checkpoint_kinds, hit_kinds)
    h5 = bool(sanitizer_observed.get("crash_type") or sanitizer_observed.get("crash_stack"))
    return {
        "mode": "hypothesis_reachability",
        "reasoning_state_event_id": reasoning_state.get("selected_event_id")
        or reasoning_state.get("selected_snapshot_event_id")
        or (reasoning_state.get("snapshot") or {}).get("event_id"),
        "H1_claimed_parser_or_source_reached": h1,
        "H2_claimed_vulnerable_function_reached": h2,
        "H2_claimed_root_cause_function_reached": h2_root,
        "H2_claimed_sink_function_reached": h2_sink,
        "H3_claimed_root_cause_line_reached": h3,
        "H4_claimed_sink_line_reached": h4,
        "H5_sanitizer_crash_observed": h5,
        "failure_stage": _hypothesis_failure_stage(h1, h2, h3, h4, h5, checkpoints),
        "checkpoints": checkpoints,
        "hit_locations": hits,
        "sanitizer_observed": sanitizer_observed,
        "coverage_diagnostics": coverage_diagnostics or {},
    }


def _sanitizer_stack_hit_kinds(
    checkpoints: list[dict[str, Any]], sanitizer_observed: dict[str, Any]
) -> set[str]:
    stack = sanitizer_observed.get("crash_stack") or []
    frames = [frame for frame in stack if isinstance(frame, dict)]
    result: set[str] = set()
    for checkpoint in checkpoints:
        kind = str(checkpoint.get("kind") or "")
        function = str(checkpoint.get("function") or "")
        line = _safe_int(checkpoint.get("line"))
        if not kind or not function:
            continue
        for frame in frames:
            if function not in str(frame.get("function") or ""):
                continue
            if kind in {"claimed_source", "claimed_root_cause_function", "claimed_sink_function"}:
                result.add(kind)
                break
            if line is not None and _safe_int(frame.get("line")) == line:
                result.add(kind)
                break
    return result


def _kind_reached(
    kind: str, checkpoint_kinds: set[str], hit_kinds: set[str]
) -> bool | None:
    if kind not in checkpoint_kinds:
        return None
    return kind in hit_kinds


def _bool_or_none(*values: bool | None) -> bool | None:
    if all(value is None for value in values):
        return None
    return any(value is True for value in values)


def _hypothesis_failure_stage(
    h1: bool | None,
    h2: bool | None,
    h3: bool | None,
    h4: bool | None,
    h5: bool,
    checkpoints: list[dict[str, Any]],
) -> str:
    if not checkpoints:
        return "hypothesis_incomplete"
    if h1 is False:
        return "claimed_parser_not_reached"
    if h2 is False:
        return "claimed_vulnerable_function_not_reached"
    if h3 is False:
        return "claimed_root_cause_line_not_reached"
    if h4 is False:
        return "claimed_sink_line_not_reached"
    if not h5:
        return "claimed_sink_reached_but_no_sanitizer_crash"
    return "hypothesis_triggered"


def _agent_hypothesis_feedback(report: dict[str, Any]) -> dict[str, Any]:
    failure = str(report.get("failure_stage") or "unknown")
    detail = _hypothesis_feedback_detail(report)
    mapping = {
        "claimed_parser_not_reached": ("H1", "Candidate did not reach the recorded source checkpoint."),
        "claimed_vulnerable_function_not_reached": ("H2", "Candidate did not reach the vulnerable function/path in your recorded hypothesis."),
        "claimed_root_cause_line_not_reached": ("H3", "Candidate did not reach the root-cause line in your recorded hypothesis."),
        "claimed_sink_line_not_reached": ("H4", "Candidate did not reach the sink/crash line in your recorded hypothesis."),
        "claimed_sink_reached_but_no_sanitizer_crash": ("H4", "Candidate reached the claimed sink line but did not trigger a sanitizer crash."),
        "hypothesis_triggered": ("H5", "Candidate reached the recorded hypothesis far enough to trigger a sanitizer crash."),
        "hypothesis_incomplete": ("hypothesis_incomplete", "The recorded reasoning state does not contain enough source/root/sink locations for H1-H5 feedback."),
    }
    stage, message = mapping.get(
        failure,
        ("unknown", "Hypothesis reachability verifier could not assign a precise H1-H5 stage."),
    )
    if detail:
        message = f"{message} {detail}"
    next_requirement = (
        "Before the next candidate, revise record_candidate_plan to address this hypothesis feedback."
        if stage != "H5"
        else "Stop if the task submit result also reports success."
    )
    if stage == "H1" and detail:
        next_requirement = (
            "Before the next candidate, explain which input-format or dispatch gate is still missing, "
            "and change the builder or seed accordingly; do not resubmit the same structural candidate."
        )
    return {
        "stage": stage,
        "failure_stage": failure,
        "message": message,
        "next_plan_requirement": next_requirement,
    }


def _hypothesis_feedback_detail(report: dict[str, Any]) -> str:
    checkpoints = [item for item in report.get("checkpoints") or [] if isinstance(item, dict)]
    hits = [item for item in report.get("hit_locations") or [] if isinstance(item, dict)]
    hit_kinds = {
        str(hit.get("kind"))
        for hit in hits
        if not hit.get("breakpoint_error") and not hit.get("run_error")
    }
    missed = [
        checkpoint
        for checkpoint in checkpoints
        if str(checkpoint.get("kind") or "") not in hit_kinds
    ]
    parts: list[str] = []
    if missed:
        first = missed[0]
        loc = _format_checkpoint_for_feedback(first)
        if loc:
            parts.append(f"First missed checkpoint: {loc}.")
    coverage_detail = _coverage_feedback_detail(report.get("coverage_diagnostics") or {})
    if coverage_detail:
        parts.append(coverage_detail)
    if hits:
        hit_locs = [_format_hit_for_feedback(hit) for hit in hits[:3]]
        hit_locs = [loc for loc in hit_locs if loc]
        if hit_locs:
            parts.append("Reached checkpoints: " + "; ".join(hit_locs) + ".")
    return " ".join(parts)


def _coverage_feedback_detail(coverage_diagnostics: dict[str, Any]) -> str:
    files = [item for item in coverage_diagnostics.get("files") or [] if isinstance(item, dict)]
    if not files:
        return ""
    details: list[str] = []
    for item in files[:2]:
        expected = str(item.get("expected_file") or "")
        matched = str(item.get("matched_file") or "")
        expected_line = _safe_int(item.get("expected_line"))
        reached = item.get("expected_line_reached")
        covered_min = _safe_int(item.get("covered_line_min"))
        covered_max = _safe_int(item.get("covered_line_max"))
        before = _safe_int(item.get("nearest_covered_line_before_or_at"))
        after = _safe_int(item.get("nearest_covered_line_after_or_at"))
        before_function = str(item.get("nearest_covered_function_before_or_at") or "")
        after_function = str(item.get("nearest_covered_function_after_or_at") or "")
        before_code = str(item.get("nearest_covered_code_before_or_at") or "")
        after_code = str(item.get("nearest_covered_code_after_or_at") or "")
        filename = expected or matched
        if expected_line is not None and reached is False:
            nearby = []
            if before is not None:
                nearby.append(_format_nearest_coverage(before, before_function, before_code))
            if after is not None and after != before:
                nearby.append(_format_nearest_coverage(after, after_function, after_code))
            nearby_text = f" ({', '.join(nearby)})" if nearby else ""
            details.append(f"Coverage reached {filename} but not line {expected_line}{nearby_text}")
        elif covered_min is not None and covered_max is not None:
            details.append(f"Coverage reached {filename} lines {covered_min}-{covered_max}")
    if not details:
        return ""
    return "Coverage context: " + "; ".join(details) + "."


def _format_nearest_coverage(line: int, function: str, code: str) -> str:
    parts = [f"nearest covered line {line}"]
    if function:
        parts.append(f"in {function}")
    if code:
        parts.append(f"`{_truncate_inline(code, 100)}`")
    return " ".join(parts)


def _truncate_inline(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 3)] + "..."


def _format_checkpoint_for_feedback(checkpoint: dict[str, Any]) -> str:
    file = str(checkpoint.get("file") or "")
    function = str(checkpoint.get("function") or "")
    line = _safe_int(checkpoint.get("line"))
    parts = []
    if file:
        parts.append(file)
    if function:
        parts.append(function)
    if line is not None:
        parts.append(str(line))
    return ":".join(parts)


def _format_hit_for_feedback(hit: dict[str, Any]) -> str:
    file = str(hit.get("file") or hit.get("expected_file") or "")
    function = str(hit.get("function") or hit.get("expected_function") or "")
    line = _safe_int(hit.get("line") or hit.get("expected_line"))
    parts = []
    if file:
        parts.append(file)
    if function:
        parts.append(function)
    if line is not None:
        parts.append(str(line))
    return ":".join(parts)


def _construction_feedback(
    *,
    result: CommandResult,
    delivery: dict[str, Any],
    sanitizer: dict[str, Any],
    cybergym_response: dict[str, Any],
    reachability: dict[str, Any],
) -> dict[str, Any]:
    if reachability.get("agent_feedback"):
        return reachability["agent_feedback"]
    if delivery.get("delivery_error"):
        return {
            "stage": "delivery",
            "failure_stage": delivery["delivery_error"],
            "message": "The candidate could not be delivered to the task runner.",
            "next_plan_requirement": "Fix the candidate path, submit command, or builder output before revising vulnerability bytes.",
        }
    target_output = str(cybergym_response.get("output") or "")
    observed = parse_sanitizer_trace(target_output) if target_output else sanitizer
    if observed.get("crash_type") or "ERROR: AddressSanitizer" in target_output:
        return {
            "stage": "H5",
            "failure_stage": "triggered",
            "message": "The candidate produced a sanitizer crash in the task runner.",
            "next_plan_requirement": "Stop if the task submit result reports the target crash.",
        }
    target_exit = _safe_int(cybergym_response.get("exit_code"))
    if result.returncode == 124 or "timed out" in str(cybergym_response.get("output") or "").lower():
        return {
            "stage": "runtime",
            "failure_stage": "timeout_or_hang",
            "message": "The candidate timed out or hung before producing the target crash.",
            "next_plan_requirement": "Revise the plan to satisfy parser constraints without entering a non-target long-running path.",
        }
    if target_exit is not None and target_exit != 0:
        return {
            "stage": "H5",
            "failure_stage": "triggered",
            "message": "The task runner returned a crashing exit code for the candidate.",
            "next_plan_requirement": "Stop if the task submit result reports the target crash.",
        }
    if target_exit == 0:
        return {
            "stage": "H1-H4_unknown",
            "failure_stage": "no_target_crash_observed",
            "message": "The candidate was accepted by the task runner but did not trigger the target crash.",
            "next_plan_requirement": "Revise the candidate plan using H1-H5 feedback, local runs, or source inspection.",
        }
    return {
        "stage": "unknown",
        "failure_stage": "submit_failed_without_reachability",
        "message": "Submission failed without enough verifier information to classify H1-H5.",
        "next_plan_requirement": "Run the candidate with probes or revise the builder based on the visible submit output.",
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _classify_delivery(result: CommandResult) -> dict[str, Any]:
    output = f"{result.stdout}\n{result.stderr}".lower()
    delivery_error = ""
    if result.returncode == 124 and "timed out" in output:
        delivery_error = "command_timeout"
    elif result.returncode in {126, 127}:
        delivery_error = "command_not_executable_or_not_found"
    elif result.returncode == 125 and "docker:" in output:
        delivery_error = "docker_invocation_or_image_error"
    elif "unable to find image" in output or "failed to resolve reference" in output:
        delivery_error = "container_image_unavailable"
    elif "no such file or directory" in output and ("candidate" in output or "/tmp/poc" in output):
        delivery_error = "candidate_input_unavailable"
    return {"delivered": not delivery_error, "delivery_error": delivery_error}


def _normalize_plan(plan: dict[str, Any], plan_id: str) -> dict[str, Any]:
    hypothesis = _object_field(plan.get("hypothesis"))
    target_input_component = _object_field(plan.get("target_input_component"))
    construction_strategy = _object_field(plan.get("construction_strategy"))
    expected_effect = _object_field(plan.get("expected_effect"))
    builder = _builder_field(plan.get("builder"))
    edits = _list_field(plan.get("edits"))
    seed = _object_field(plan.get("seed"))
    output_contract = _object_field(
        plan.get("output_contract")
        or plan.get("candidate_contract")
        or builder.get("output_contract")
    )
    return {
        "plan_id": plan_id,
        "stage": str(plan.get("stage") or "candidate_synthesis"),
        "reasoning_event_ids": list(plan.get("reasoning_event_ids") or []),
        "construction_support_ids": list(plan.get("construction_support_ids") or []),
        "hypothesis": hypothesis,
        "target_input_component": target_input_component,
        "construction_strategy": construction_strategy,
        "edits": edits,
        "expected_effect": expected_effect,
        "builder": builder,
        "seed": seed,
        "output_contract": output_contract,
        "previous_feedback": _object_field(
            plan.get("previous_feedback") or plan.get("feedback_response")
        ),
        "candidate_filename": str(plan.get("candidate_filename") or "candidate_input"),
        "notes": list(plan.get("notes") or []),
    }


def _object_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value:
        return {"description": str(value)}
    return {}


def _list_field(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value:
        return [{"description": str(value)}]
    return []


def _builder_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return {"kind": "external_command", "command": value.strip()}
    return {"kind": "seed_edits"}


def _validate_plan(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not plan.get("hypothesis"):
        errors.append("candidate plan requires hypothesis")
    if not plan.get("target_input_component"):
        errors.append("candidate plan requires target_input_component")
    if not plan.get("expected_effect"):
        errors.append("candidate plan requires expected_effect")
    strategy = plan.get("construction_strategy") or {}
    if strategy:
        mode = str(strategy.get("mode") or "")
        if mode and mode not in {
            "direct",
            "seed_mutation",
            "external_builder",
            "retrieval_assisted_builder",
            "structured_tool",
        }:
            errors.append("construction_strategy.mode is invalid")
        if mode == "seed_mutation" and not _plan_has_real_seed(plan):
            errors.append(
                "construction_strategy.mode seed_mutation requires a real seed "
                "(for example seed.path, seed.uri, seed.id, or seed.name); changing "
                "only the mode is not a seed mutation plan"
            )
    builder_kind = str((plan.get("builder") or {}).get("kind") or "seed_edits")
    if builder_kind != "external_command" and not plan.get("edits") and not plan.get("seed"):
        errors.append("candidate plan requires seed or edits unless using external_command")
    raw_byte_edits = []
    for edit in plan.get("edits") or []:
        if isinstance(edit, dict) and str(edit.get("op") or "") in {
            "set_bytes",
            "append_bytes",
            "write_hex",
        }:
            raw_byte_edits.append(edit)
    if raw_byte_edits:
        plan.setdefault("warnings", []).append("raw byte edits are allowed but marked weaker than structured edits")
    return errors


def _validate_builder_executable(plan: dict[str, Any], workspace: Path) -> list[str]:
    builder = plan.get("builder") or {}
    if str(builder.get("kind") or "") != "external_command":
        return []
    command = str(builder.get("command") or "")
    if not command:
        return []
    errors: list[str] = []
    lowered = command.lower()
    if ("python3 -c" in lowered or "python -c" in lowered) and (
        len(command) > 1200 or "\\n" in command
    ):
        errors.append(
            "long or multiline Python builders should use a shell here-doc "
            "inside builder.command, for example: "
            "python3 - {candidate_path} <<'PY' ... PY; avoid long python3 -c "
            "commands with escaped newlines"
        )
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if not token.startswith("/workspace/"):
            continue
        if "{candidate_path}" in token or "{seed_path}" in token or "{plan_path}" in token:
            continue
        if not token.endswith((".py", ".sh", ".pl", ".rb", ".js")):
            continue
        host_path = workspace / token.removeprefix("/workspace/")
        if not host_path.exists():
            errors.append(
                "external_command references missing builder script "
                f"{token}; use an inline here-doc command (for example "
                "python3 - {candidate_path} <<'PY' ... PY) or provide "
                "seed/edits instead"
            )
    return errors


def _latest_failed_construction_feedback(workspace: Path) -> dict[str, Any]:
    for record in reversed(_read_jsonl(workspace / "candidate_submissions.jsonl")):
        feedback = record.get("construction_feedback") or {}
        if not isinstance(feedback, dict):
            continue
        if feedback.get("stage") == "H5":
            return {}
        if feedback.get("stage") or feedback.get("failure_stage"):
            return {
                "submit_id": record.get("submit_id"),
                "candidate_id": record.get("candidate_id"),
                "plan_id": record.get("plan_id"),
                "stage": feedback.get("stage"),
                "failure_stage": feedback.get("failure_stage"),
                "message": feedback.get("message"),
            }
    for record in reversed(_read_jsonl(workspace / "candidates.jsonl")):
        if record.get("built") is True:
            return {}
        errors = record.get("errors") or []
        if errors:
            return {
                "candidate_id": record.get("candidate_id"),
                "plan_id": record.get("plan_id"),
                "stage": "build",
                "failure_stage": "candidate_build_failed",
                "message": "; ".join(str(error) for error in errors),
            }
    return {}


def _plan_addresses_feedback(plan: dict[str, Any], feedback: dict[str, Any]) -> bool:
    response = plan.get("previous_feedback") or {}
    if not response:
        return False
    stage = str(feedback.get("stage") or "").lower()
    failure_stage = str(feedback.get("failure_stage") or "").lower()
    text = json.dumps(response, ensure_ascii=False).lower()
    return bool(
        (stage and stage in text)
        or (failure_stage and failure_stage in text)
        or response.get("submit_id") == feedback.get("submit_id")
    )


def _validate_plan_progress_after_feedback(
    plan: dict[str, Any],
    workspace: Path,
    feedback: dict[str, Any],
) -> list[str]:
    if str(feedback.get("stage") or "") != "H1":
        return []
    threshold = _h1_direct_retry_threshold()
    if _consecutive_h1_feedback_count(workspace) < threshold:
        return []
    if _plan_has_seed_or_edits(plan):
        return []
    strategy = plan.get("construction_strategy") or {}
    mode = str(strategy.get("mode") or "").strip().lower()
    output_contract = plan.get("output_contract") or {}
    if _has_positive_output_contract(output_contract):
        return []
    return [
        "after repeated H1 reachability failures, scratch candidate plans must provide "
        "positive output evidence: use seed/edits with a real seed, or include "
        "output_contract.validation_command / required_prefix_hex / min_size so the "
        "builder output can be checked before submit"
    ]


def _validate_known_format_output_contract(plan: dict[str, Any]) -> list[str]:
    if _plan_has_seed_or_edits(plan):
        return []
    if _has_positive_output_contract(plan.get("output_contract") or {}):
        return []
    format_memory = plan.get("format_memory") or {}
    if not isinstance(format_memory, dict) or not format_memory.get("matches"):
        return []
    strategy = plan.get("construction_strategy") or {}
    mode = str(strategy.get("mode") or "").strip().lower()
    if mode and mode not in {"direct", "external_builder", "retrieval_assisted_builder"}:
        return []
    return [
        "known-format scratch plans must include positive output evidence: "
        "add output_contract.required_prefix_hex, required_contains_hex, min_size, "
        "or validation_command, or switch to seed_mutation with a real seed"
    ]


def _format_memory_for_plan(plan: dict[str, Any]) -> dict[str, Any]:
    text = _plan_search_text(plan)
    if not text:
        return {"query": {"text": ""}, "matches": []}
    records = _load_format_memory()
    query_terms = _memory_terms(text)
    best_format = ""
    best_score = 0
    for record in records:
        names = [
            str(record.get("format_or_protocol") or ""),
            *(str(alias) for alias in record.get("aliases") or []),
        ]
        name_terms = _memory_terms(" ".join(names))
        if not name_terms:
            continue
        score = len(query_terms & name_terms)
        if score > best_score:
            best_score = score
            best_format = str(record.get("format_or_protocol") or "")
    if not best_format:
        return {"query": {"text": text[:500]}, "matches": []}
    return lookup_format_memory(format_or_protocol=best_format, query=text)


def _plan_search_text(plan: dict[str, Any]) -> str:
    fields = [
        plan.get("hypothesis"),
        plan.get("target_input_component"),
        plan.get("construction_strategy"),
        plan.get("expected_effect"),
        plan.get("builder"),
        plan.get("seed"),
        plan.get("output_contract"),
        plan.get("candidate_filename"),
        plan.get("notes"),
    ]
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def _has_positive_output_contract(contract: dict[str, Any]) -> bool:
    if not isinstance(contract, dict) or not contract:
        return False
    return bool(
        contract.get("validation_command")
        or contract.get("required_prefix_hex")
        or contract.get("required_contains_hex")
        or contract.get("min_size")
    )


def _h1_direct_retry_threshold() -> int:
    raw = os.environ.get("CANDIDATE_SYNTHESIS_H1_DIRECT_RETRY_THRESHOLD", "2")
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def _consecutive_h1_feedback_count(workspace: Path) -> int:
    count = 0
    for record in reversed(_read_jsonl(workspace / "candidate_submissions.jsonl")):
        feedback = record.get("construction_feedback") or record.get("reachability_feedback") or {}
        if not isinstance(feedback, dict):
            break
        if feedback.get("stage") == "H1":
            count += 1
            continue
        break
    return count


def _plan_has_seed_or_edits(plan: dict[str, Any]) -> bool:
    if plan.get("seed"):
        return True
    return bool(plan.get("edits"))


def _plan_has_real_seed(plan: dict[str, Any]) -> bool:
    seed = plan.get("seed")
    if not isinstance(seed, dict):
        return False
    for key in ("path", "uri", "id", "name", "corpus_id", "source_path"):
        value = seed.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if value not in (None, "", [], {}):
            return True
    return False


def _default_format_memory_dir() -> Path:
    raw = os.environ.get("FORMAT_CONSTRUCTION_MEMORY_DIR")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[1] / "external_memory" / "format_construction"


def _load_format_memory(memory_dir: Path | None = None) -> list[dict[str, Any]]:
    directory = (memory_dir or _default_format_memory_dir()).resolve()
    records: list[dict[str, Any]] = []
    if not directory.exists():
        return records
    for path in sorted(directory.glob("*.json")):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        provenance = parsed.get("provenance") or {}
        if isinstance(provenance, dict) and (
            provenance.get("target_specific") is True
            or provenance.get("contains_poc") is True
            or provenance.get("contains_vulnerability_trigger") is True
        ):
            continue
        parsed = dict(parsed)
        parsed["memory_path"] = str(path)
        records.append(parsed)
    return records


def _memory_terms(text: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-zA-Z0-9+#.]+", text.lower())
        if len(token) >= 2
    }


def _normalize_support_request(request: dict[str, Any], support_id: str) -> dict[str, Any]:
    input_modality = request.get("input_modality") or request.get("modality")
    format_or_protocol = request.get("format_or_protocol") or request.get("format") or request.get("protocol")
    known_constraints = request.get("known_constraints") or request.get("constraints") or []
    if isinstance(known_constraints, str):
        known_constraints = [known_constraints]
    needed_knowledge = request.get("needed_knowledge") or request.get("documentation_needed") or request.get("public_documentation") or []
    if isinstance(needed_knowledge, str):
        needed_knowledge = [needed_knowledge]
    builder_interface = dict(request.get("builder_interface") or {})
    if not builder_interface and request.get("builder_needed"):
        builder_interface = {"description": str(request.get("builder_needed"))}
    return {
        "support_id": support_id,
        "reasoning_event_ids": list(request.get("reasoning_event_ids") or []),
        "input_modality": str(input_modality or ""),
        "format_or_protocol": str(format_or_protocol or ""),
        "construction_goal": str(request.get("construction_goal") or ""),
        "known_constraints": list(known_constraints),
        "needed_knowledge": list(needed_knowledge),
        "retrieval_plan": dict(request.get("retrieval_plan") or {}),
        "allowed_sources": list(request.get("allowed_sources") or []),
        "disallowed_sources": list(request.get("disallowed_sources") or []),
        "proposed_tools": list(request.get("proposed_tools") or []),
        "builder_interface": builder_interface,
        "notes": list(request.get("notes") or []),
    }


def _validate_support_request(request: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not request.get("input_modality"):
        errors.append("construction support request requires input_modality")
    if not request.get("construction_goal"):
        errors.append("construction support request requires construction_goal")
    if not request.get("builder_interface"):
        errors.append("construction support request requires builder_interface")
    retrieval_plan = request.get("retrieval_plan") or {}
    if retrieval_plan and not (
        request.get("allowed_sources") or request.get("disallowed_sources")
    ):
        errors.append("retrieval_plan requires allowed_sources or disallowed_sources")
    return errors


def _validate_reasoning_link(
    plan: dict[str, Any],
    *,
    reasoning_events_path: Path | None,
    reasoning_state_path: Path | None,
    allow_unguided: bool,
    require_complete: bool,
    require_minimal: bool = False,
) -> dict[str, Any]:
    event_ids = {int(x) for x in plan.get("reasoning_event_ids") or [] if str(x).isdigit()}
    link = {
        "reasoning_event_ids": sorted(event_ids),
        "reasoning_events_path": str(reasoning_events_path or ""),
        "reasoning_state_path": str(reasoning_state_path or ""),
    }
    errors: list[str] = []
    guided = bool(event_ids)
    state: dict[str, Any] = {}
    if event_ids and reasoning_events_path and reasoning_events_path.exists():
        records = load_reasoning_events(reasoning_events_path)
        known = {int(r.get("event_id")) for r in records if str(r.get("event_id")).isdigit()}
        missing = sorted(event_ids - known)
        if missing:
            errors.append(f"unknown reasoning_event_ids: {missing}")
        else:
            selected = [
                record
                for record in records
                if str(record.get("event_id")).isdigit()
                and int(record.get("event_id")) in event_ids
            ]
            state = reduce_records(selected)
            link["resolved_from"] = "reasoning_events"
            link["selected_snapshot_event_id"] = state.get("snapshot", {}).get("event_id") or state.get("last_event_id")
            guided = True
    if not state and reasoning_state_path and reasoning_state_path.exists():
        try:
            state = json.loads(reasoning_state_path.read_text(encoding="utf-8", errors="replace"))
            link["reasoning_snapshot_complete"] = not bool(state.get("next_missing"))
            link["resolved_from"] = "reasoning_state"
            link["selected_snapshot_event_id"] = state.get("snapshot", {}).get("event_id") or state.get("last_event_id")
            guided = True
        except json.JSONDecodeError:
            errors.append("reasoning_state_path is not valid JSON")
    if not guided and not allow_unguided:
        errors.append("candidate plan must link to reasoning_event_ids or an existing reasoning_state_path")
    reasoning_complete = bool(state) and not bool(state.get("next_missing"))
    link["reasoning_snapshot_complete"] = reasoning_complete
    if state:
        link["bound_source"] = _compact_bound_location(_state_location(state, "source"))
        link["bound_sink"] = _compact_bound_location(_state_location(state, "sink"))
        link["bound_root_cause"] = _compact_bound_location(_state_location(state, "root_cause"))
        link["bound_trace_steps"] = len(state.get("trace") or [])
    if require_complete and not allow_unguided and not reasoning_complete:
        missing = state.get("next_missing") if state else []
        errors.append(
            "candidate plan requires a complete bound reasoning_state_snapshot "
            f"before construction; missing: {', '.join(missing) if missing else 'unresolved reasoning state'}"
        )
    if require_minimal and not allow_unguided and not _minimal_hypothesis_ready(state):
        missing = _missing_minimal_hypothesis_fields(state)
        errors.append(
            "candidate plan requires a minimal bound vulnerability hypothesis "
            f"before construction; missing: {', '.join(missing) if missing else 'unresolved hypothesis'}"
        )
    return {
        "guided": guided,
        "reasoning_link": link,
        "reasoning_complete": reasoning_complete,
        "reasoning_state_snapshot": state,
        "errors": errors,
    }


def _minimal_hypothesis_ready(state: dict[str, Any]) -> bool:
    return not _missing_minimal_hypothesis_fields(state)


def _missing_minimal_hypothesis_fields(state: dict[str, Any]) -> list[str]:
    if not state:
        return ["reasoning_state"]
    missing: list[str] = []
    source = _state_location(state, "source")
    sink = _state_location(state, "sink")
    if not _has_minimal_location(source):
        missing.append("source_or_parser")
    if not _has_minimal_location(sink):
        missing.append("sink_or_crash_target")
    snapshot = state.get("snapshot") or {}
    text = " ".join(
        str(value or "")
        for value in [
            snapshot.get("note"),
            snapshot.get("text"),
            (source or {}).get("note") if isinstance(source, dict) else "",
            (source or {}).get("description") if isinstance(source, dict) else "",
            (sink or {}).get("note") if isinstance(sink, dict) else "",
            (sink or {}).get("description") if isinstance(sink, dict) else "",
        ]
    ).strip()
    if not text:
        missing.append("hypothesis_note")
    return missing


def _has_minimal_location(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return bool(value.get("function") and (value.get("file") or value.get("line")))


def _state_location(state: dict[str, Any], kind: str) -> Any:
    modern = state.get(f"primary_{kind}")
    if modern:
        return modern
    return state.get(kind)


def _compact_bound_location(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value.get(key)
        for key in ("file", "function", "line", "var", "event_id")
        if value.get(key) not in (None, "")
    }


def _initial_candidate_bytes(plan: dict[str, Any]) -> bytes:
    seed = plan.get("seed") or {}
    if seed.get("path"):
        return Path(seed["path"]).read_bytes()
    if seed.get("text") is not None:
        return str(seed["text"]).encode("utf-8")
    if seed.get("hex") is not None:
        return _decode_hex(str(seed["hex"]))
    return b""


def _apply_edits(data: bytes, edits: list[dict[str, Any]]) -> bytes:
    current = bytearray(data)
    for edit in edits:
        op = str(edit.get("op") or "")
        if op == "write_text":
            current = bytearray(str(edit.get("content") or "").encode("utf-8"))
        elif op == "write_hex":
            current = bytearray(_decode_hex(str(edit.get("hex") or "")))
        elif op == "replace_text":
            old = str(edit.get("old") or "").encode("utf-8")
            new = str(edit.get("new") or "").encode("utf-8")
            count = int(edit.get("count") or 1)
            current = bytearray(bytes(current).replace(old, new, count))
        elif op == "set_bytes":
            offset = int(edit.get("offset"))
            value = _decode_hex(str(edit.get("value_hex") or ""))
            current[offset:offset + len(value)] = value
        elif op == "append_bytes":
            current.extend(_decode_hex(str(edit.get("value_hex") or "")))
        elif op == "append_text":
            current.extend(str(edit.get("content") or "").encode("utf-8"))
        elif op in {"set_field", "set_argument", "set_message_field", "append_operation"}:
            raise ValueError(
                f"{op} requires a structured adapter via builder.kind=external_command"
            )
        else:
            raise ValueError(f"unsupported edit op: {op}")
    return bytes(current)


def _probe_breakpoints(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, probe in enumerate(probes):
        if not isinstance(probe, dict):
            continue
        function = str(probe.get("function") or probe.get("name") or "")
        file = str(probe.get("file") or "")
        line = probe.get("line")
        if not function and not (file and line):
            continue
        out.append({
            "kind": str(probe.get("kind") or f"agent_probe_{idx + 1}"),
            "function": function,
            "file": file,
            "line": line,
        })
    return out


def _summarize_probe_hits(hits: list[dict[str, Any]]) -> dict[str, bool]:
    summary: dict[str, bool] = {}
    for hit in hits:
        if hit.get("breakpoint_error") or hit.get("run_error"):
            continue
        key = str(hit.get("expected_function") or hit.get("kind") or hit.get("breakpoint_spec"))
        if key:
            summary[key] = True
    return summary


def _load_candidate(workspace: Path, candidate_id: str) -> dict[str, Any]:
    path = workspace / "candidates" / candidate_id / "candidate.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _find_jsonl_record(path: Path, key: str, value: str) -> dict[str, Any]:
    for record in _read_jsonl(path):
        if str(record.get(key)) == value:
            return record
    return {}


def _latest_jsonl_record(path: Path) -> dict[str, Any]:
    records = _read_jsonl(path)
    return records[-1] if records else {}


def _seed_path(plan: dict[str, Any]) -> Path:
    seed = plan.get("seed") or {}
    return Path(seed.get("path") or "")


def _write_plan_snapshot(candidate_dir: Path, plan: dict[str, Any]) -> Path:
    path = candidate_dir / "plan.json"
    _write_json(path, plan)
    return path


def _discover_candidate_output(workspace: Path, candidate_path: Path) -> Path | None:
    names = (
        "poc",
        "poc.bin",
        "poc.dat",
        "poc.rar",
        "poc.rar5",
        "poc.zip",
        "candidate",
        "candidate_input",
    )
    candidates: list[Path] = []
    for name in names:
        path = workspace / name
        if path.exists() and path.is_file() and path != candidate_path:
            candidates.append(path)
    for pattern in ("poc.*", "candidate.*", "*.poc"):
        for path in workspace.glob(pattern):
            if path.exists() and path.is_file() and path != candidate_path:
                candidates.append(path)
    unique = {path.resolve(): path for path in candidates}
    if not unique:
        return None
    return max(unique.values(), key=lambda p: p.stat().st_mtime)


def _validate_candidate_artifact(
    *,
    plan: dict[str, Any],
    candidate_path: Path,
    workspace: Path,
    seed_path: Path | None,
    plan_path: Path | None,
    timeout: int,
) -> dict[str, Any]:
    contract = plan.get("output_contract") or {}
    checks: dict[str, Any] = {
        "candidate_path": str(candidate_path),
        "exists": candidate_path.exists(),
    }
    errors: list[str] = []
    if not candidate_path.exists():
        return {"checks": checks, "errors": errors}

    data = candidate_path.read_bytes()
    checks.update(
        {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "prefix_hex": data[:16].hex(),
        }
    )
    if len(data) == 0:
        errors.append("candidate artifact is empty")

    min_size = _optional_positive_int(contract.get("min_size"))
    if min_size is not None:
        checks["min_size"] = min_size
        if len(data) < min_size:
            errors.append(
                f"candidate artifact is smaller than output_contract.min_size "
                f"({len(data)} < {min_size})"
            )

    max_size = _optional_positive_int(contract.get("max_size"))
    if max_size is not None:
        checks["max_size"] = max_size
        if len(data) > max_size:
            errors.append(
                f"candidate artifact is larger than output_contract.max_size "
                f"({len(data)} > {max_size})"
            )

    required_prefix = _hex_bytes(contract.get("required_prefix_hex"))
    if required_prefix is not None:
        checks["required_prefix_hex"] = required_prefix.hex()
        if not data.startswith(required_prefix):
            errors.append("candidate artifact does not match output_contract.required_prefix_hex")

    required_contains = _hex_bytes(contract.get("required_contains_hex"))
    if required_contains is not None:
        checks["required_contains_hex"] = required_contains.hex()
        if required_contains not in data:
            errors.append("candidate artifact does not contain output_contract.required_contains_hex")

    validation_command = str(contract.get("validation_command") or "").strip()
    if validation_command:
        command_result = _run_command(
            _format_command(
                validation_command,
                candidate_path=candidate_path,
                seed_path=seed_path,
                plan_path=plan_path,
            ),
            cwd=workspace,
            timeout=timeout,
        )
        checks["validation_command"] = asdict(command_result)
        if command_result.returncode != 0:
            errors.append(
                "output_contract.validation_command failed with "
                f"exit code {command_result.returncode}"
            )

    return {"checks": checks, "errors": errors}


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _hex_bytes(value: Any) -> bytes | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    text = "".join(char for char in text if char not in " \t\r\n:_-")
    if not text:
        return None
    if len(text) % 2:
        text = "0" + text
    try:
        return binascii.unhexlify(text)
    except (binascii.Error, ValueError):
        return None


def _format_command(template: str, *, candidate_path: Path, seed_path: Path | None = None, plan_path: Path | None = None) -> str:
    workspace = candidate_path.parents[2] if len(candidate_path.parents) >= 3 else candidate_path.parent
    replacements = {
        "candidate": str(candidate_path),
        "candidate_path": str(candidate_path),
        "poc": str(candidate_path),
        "seed": str(seed_path or ""),
        "seed_path": str(seed_path or ""),
        "plan": str(plan_path or ""),
        "plan_path": str(plan_path or ""),
    }
    result = template
    result = result.replace("/workspace/", str(workspace) + "/")
    if result == "/workspace":
        result = str(workspace)
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", shlex.quote(value))
    return result


def _run_command(command: str, *, cwd: Path, timeout: int) -> CommandResult:
    result, _ = _run_command_with_bytes(command, cwd=cwd, timeout=timeout)
    return result


def _run_command_with_bytes(
    command: str, *, cwd: Path, timeout: int
) -> tuple[CommandResult, bytes]:
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            text=False,
            capture_output=True,
            timeout=timeout,
            executable=_shell_executable(),
            env=_command_env(),
        )
        stdout_bytes = proc.stdout or b""
        stderr_bytes = proc.stderr or b""
        return (
            CommandResult(
                command=command,
                cwd=str(cwd),
                returncode=proc.returncode,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            ),
            stdout_bytes,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        stdout_bytes = stdout if isinstance(stdout, bytes) else str(stdout).encode()
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        stderr = f"{stderr}\nCommand timed out after {timeout} seconds".strip()
        return (
            CommandResult(
                command=command,
                cwd=str(cwd),
                returncode=124,
                stdout=stdout,
                stderr=stderr,
            ),
            stdout_bytes,
        )


def _shell_executable() -> str:
    return "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"


def _command_env() -> dict[str, str]:
    path_parts = [
        part for part in os.environ.get("PATH", "").split(os.pathsep) if part
    ]
    for required in ("/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"):
        if required not in path_parts:
            path_parts.append(required)
    return {
        **os.environ,
        "PATH": os.pathsep.join(path_parts),
        "LC_ALL": "C",
        "LANG": "C",
    }


def _next_index(root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True)
    return len([item for item in root.iterdir() if item.exists()]) + 1


def _decode_hex(value: str) -> bytes:
    cleaned = "".join(value.split()).removeprefix("0x")
    if len(cleaned) % 2:
        cleaned = "0" + cleaned
    return binascii.unhexlify(cleaned)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
