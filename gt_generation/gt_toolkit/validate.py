"""Validate a fine-grained memory-safety ground_truth.json.

Two tiers:

- errors   : structural violations of the canonical schema. Always fail.
- warnings : quality expectations (value_from, description, reachability
             checkpoint, bootstrap wording, line-exactness). Promoted to errors
             under --strict.

The validator is deterministic and dependency-free. If the optional
`jsonschema` package is importable it is additionally used for a structural
cross-check, but the manual checks are authoritative so the toolkit works in a
bare Python environment (any coding-agent CLI can call it).
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "ground_truth.schema.json"

TOP_LEVEL_REQUIRED = [
    "sample_id",
    "vuln_id",
    "project",
    "classification",
    "bug_description",
    "source",
    "sink",
    "root_cause",
    "tainted_value_origin",
    "coarse_trace",
    "fine_trace",
    "sanitizer_ground_truth",
    "poc",
]

LEGACY_KEYS = {
    "binary_gt",
    "trace_hint",
    "slice_gt",
    "execution_trace",
    "trace",
    "call_chain",
    "data_flow_chain",
    "patch",
    "harness_context",
    "watchpoint_trace",
}

BOOTSTRAP_WORDING = ("first-pass", "selected as", "requires review")

# libFuzzer entry points are the documented unscored test boundary, never the scored
# source. Deliberately limited to fuzzer entries: a CLI-repro sample may legitimately
# read its input in main(), and widening this set would retroactively invalidate such
# ground truth on a rule the contract does not actually state.
HARNESS_ENTRY_FUNCTIONS = {
    "LLVMFuzzerTestOneInput",
    "LLVMFuzzerInitialize",
}


@dataclass
class Report:
    ground_truth: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def as_dict(self, strict: bool) -> dict[str, Any]:
        errors = list(self.errors)
        warnings = list(self.warnings)
        if strict:
            errors = errors + warnings
            warnings = []
        return {
            "ground_truth": self.ground_truth,
            "ok": not errors,
            "strict": strict,
            "errors": errors,
            "warnings": warnings,
        }


def _require_keys(obj: Any, keys: list[str], where: str, report: Report) -> None:
    if not isinstance(obj, dict):
        report.err(f"{where} must be an object")
        return
    for key in keys:
        if key not in obj:
            report.err(f"{where} missing {key}")


def _check_location(data: dict, key: str, report: Report) -> None:
    loc = data.get(key, {})
    if not isinstance(loc, dict):
        report.err(f"{key} must be an object")
        return
    for field_name in ["file", "function", "line"]:
        if field_name not in loc:
            report.err(f"{key} missing {field_name}")
    # Quality: human-readable explanation on the scored locations.
    if key in ("source", "sink", "root_cause") and not str(loc.get("description", "")).strip():
        report.warn(f"{key} missing non-empty description")
    if key == "source" and not str(loc.get("value_from", "")).strip():
        report.warn("source missing value_from (untrusted-input provenance)")


def _check_scored_source_boundary(data: dict, report: Report) -> None:
    """`source` must be project code, not the libFuzzer entry point.

    LLVMFuzzerTestOneInput is the unscored test boundary: every libFuzzer sample
    shares it, so scoring it gives the answer away for free. The scored source is
    the project parser/helper statement that reads `data,size` into a length,
    count, object, ownership state, or dispatch key on the vulnerable path.
    Shallow traces are the most exposed, because the harness sits close to the sink.
    """
    source = data.get("source")
    if not isinstance(source, dict):
        return
    function = str(source.get("function") or "").strip()
    if function in HARNESS_ENTRY_FUNCTIONS:
        report.err(
            f"source.function is the unscored harness boundary '{function}'; "
            "use the project statement that first consumes the fuzzer buffer"
        )


def _check_bootstrap_wording(data: dict, report: Report) -> None:
    hay = json.dumps(data, ensure_ascii=False).lower()
    for phrase in BOOTSTRAP_WORDING:
        if phrase in hay:
            report.warn(f"bootstrap-draft wording present: '{phrase}'")


def _check_source_lines(data: dict, source_root: Path, report: Report) -> None:
    """Best-effort: confirm scored location lines exist in the checkout."""
    for key in ("source", "sink", "root_cause"):
        loc = data.get(key, {})
        if not isinstance(loc, dict):
            continue
        rel = str(loc.get("file", "")).strip()
        line = loc.get("line")
        if not rel or not isinstance(line, int):
            continue
        candidate = source_root / rel
        if not candidate.exists():
            # Try a suffix match, since GT file paths may be repo-relative.
            matches = list(source_root.rglob(Path(rel).name))
            if len(matches) != 1:
                report.warn(f"{key}: file not found under source-root: {rel}")
                continue
            candidate = matches[0]
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            report.warn(f"{key}: cannot read {candidate}")
            continue
        if line < 1 or line > len(text):
            report.warn(f"{key}: line {line} out of range in {rel} ({len(text)} lines)")


def validate_data(
    data: Any,
    *,
    ground_truth: str = "<memory>",
    source_root: Path | None = None,
) -> Report:
    report = Report(ground_truth=ground_truth)

    if not isinstance(data, dict):
        report.err("ground_truth must be a JSON object")
        return report

    present_legacy = sorted(LEGACY_KEYS & set(data))
    if present_legacy:
        report.err(f"legacy/expanded keys are not allowed: {', '.join(present_legacy)}")

    missing = sorted(set(TOP_LEVEL_REQUIRED) - set(data))
    if missing:
        report.err(f"missing top-level keys: {', '.join(missing)}")

    _require_keys(data.get("project"), ["id", "repo", "vulnerable_commit", "fixed_commit"], "project", report)
    _require_keys(data.get("classification"), ["class", "cwe"], "classification", report)

    bug = data.get("bug_description", {})
    _require_keys(bug, ["original", "original_source", "normalized"], "bug_description", report)
    if isinstance(bug, dict):
        for key in ["original", "original_source", "normalized"]:
            if key in bug and not str(bug.get(key, "")).strip():
                report.err(f"bug_description {key} is empty")

    for key in ("source", "sink", "root_cause", "tainted_value_origin"):
        _check_location(data, key, report)
    _check_scored_source_boundary(data, report)

    # tainted_value_origin should carry the concrete value fields.
    tvo = data.get("tainted_value_origin", {})
    if isinstance(tvo, dict):
        for key in ["var", "code"]:
            if key not in tvo:
                report.warn(f"tainted_value_origin missing {key}")

    # reachability_checkpoints is optional structurally, recommended for R1.
    reach = data.get("reachability_checkpoints")
    if reach is None:
        report.warn("reachability_checkpoints absent (needed for R1 reachability eval)")
    elif not isinstance(reach, dict):
        report.err("reachability_checkpoints must be an object")
    else:
        pa = reach.get("parser_admitted")
        if not isinstance(pa, dict):
            report.err("reachability_checkpoints.parser_admitted must be an object")
        else:
            for key in ["file", "function", "line"]:
                if key not in pa:
                    report.err(f"reachability_checkpoints.parser_admitted missing {key}")

    coarse = data.get("coarse_trace")
    if not isinstance(coarse, list) or not coarse:
        report.err("coarse_trace must be a non-empty list")
    else:
        for idx, step in enumerate(coarse):
            _require_keys(step, ["step", "file", "function", "summary"], f"coarse_trace[{idx}]", report)
            if isinstance(step, dict) and ({"role", "kind"} & step.keys()):
                report.err(f"coarse_trace[{idx}] must not contain role or kind")

    fine = data.get("fine_trace")
    if not isinstance(fine, list) or not fine:
        report.err("fine_trace must be a non-empty list")
    else:
        for idx, step in enumerate(fine):
            _require_keys(
                step,
                ["step", "file", "function", "line", "var", "code", "note"],
                f"fine_trace[{idx}]",
                report,
            )
            if isinstance(step, dict) and ({"role", "kind"} & step.keys()):
                report.err(f"fine_trace[{idx}] must not contain role or kind")
        for anchor_name in ("source", "root_cause", "sink"):
            anchor = data.get(anchor_name)
            if not isinstance(anchor, dict):
                continue
            trace_step = anchor.get("trace_step")
            if not isinstance(trace_step, int):
                report.err(f"top-level {anchor_name} missing integer trace_step")
                continue
            matched = next(
                (
                    step for step in fine
                    if isinstance(step, dict) and step.get("step") == trace_step
                ),
                None,
            )
            if matched is None:
                report.err(
                    f"top-level {anchor_name}.trace_step={trace_step} does not exist in fine_trace"
                )
            elif not _trace_step_matches_location(matched, anchor):
                report.warn(
                    f"top-level {anchor_name} location differs from fine_trace step {trace_step}"
                )
        _check_edges(fine, report)

    _check_sanitizer_gt(data, report)
    _check_poc(data, report)
    _check_bootstrap_wording(data, report)

    if source_root is not None:
        _check_source_lines(data, source_root, report)

    return report


EDGE_TYPES = {"data", "control", "order"}
ORDER_RELATIONS = {
    "free_before_use", "double_free", "use_before_init",
    "use_after_return", "use_after_scope",
}
# Two+ word-like tokens with no operator/punctuation between them = a prose sentence.
_PROSE_RE = re.compile(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}")
_OPERATOR_RE = re.compile(r"[<>=!+\-*/\[\]&|%().]")


def _trace_step_matches_location(step: dict, anchor: dict) -> bool:
    """Check the source-location consistency of an explicit top-level trace_step link."""
    anchor_file = str(anchor.get("file") or "")
    anchor_function = str(anchor.get("function") or "")
    try:
        anchor_line = int(anchor.get("line"))
    except (TypeError, ValueError):
        return False
    if str(step.get("file") or "") != anchor_file:
        return False
    if str(step.get("function") or "") != anchor_function:
        return False
    try:
        line = int(step.get("line"))
        line_end = int(step.get("line_end", line))
    except (TypeError, ValueError):
        return False
    return line <= anchor_line <= line_end


def _check_edges(fine: list, report: Report) -> None:
    """Validate the typed depends_on edges (the source->sink association)."""
    any_edge = False
    any_control_or_order = False
    for idx, step in enumerate(fine):
        if not isinstance(step, dict):
            continue
        deps = step.get("depends_on")
        if deps is None:
            if idx > 0:
                report.warn(f"fine_trace[{idx}] has no depends_on edge (source->sink association missing)")
            continue
        if not isinstance(deps, list):
            report.err(f"fine_trace[{idx}].depends_on must be a list")
            continue
        for edge in deps:
            if not isinstance(edge, dict):
                report.err(f"fine_trace[{idx}].depends_on edge must be an object {{on,type,via}}")
                continue
            any_edge = True
            if "on" not in edge:
                report.err(f"fine_trace[{idx}].depends_on edge missing 'on'")
            etype = edge.get("type")
            if etype not in EDGE_TYPES:
                report.err(f"fine_trace[{idx}].depends_on edge type must be one of {sorted(EDGE_TYPES)}")
            elif etype in ("control", "order"):
                any_control_or_order = True
                _check_relation_via(idx, etype, edge, report)
    if fine and not any_edge:
        report.warn("fine_trace has no typed depends_on edges; data/control/order association not represented")
    elif any_edge and not any_control_or_order:
        report.warn("fine_trace has only data edges; control/order association (why unsafe / free-before-use) may be missing")


def _check_relation_via(idx: int, etype: str, edge: dict, report: Report) -> None:
    """control `via` is a guard expression (code); order `via` is a relation keyword.
    Neither may be a natural-language sentence."""
    via = edge.get("via")
    if not isinstance(via, str) or not via.strip():
        report.warn(f"fine_trace[{idx}].depends_on {etype} edge has no `via`")
        return
    via = via.strip()
    if etype == "order":
        if via not in ORDER_RELATIONS:
            report.warn(f"fine_trace[{idx}].depends_on order edge `via`={via!r} is not a known relation ({sorted(ORDER_RELATIONS)})")
        return
    # control: expect a guard expression, reject prose (words without operators).
    if _PROSE_RE.search(via) and not _OPERATOR_RE.search(via):
        report.err(
            f"fine_trace[{idx}].depends_on control edge `via`={via!r} looks like prose; "
            f"use the guard predicate expression (patch-verbatim) and put explanation in the step `note`"
        )


def _check_sanitizer_gt(data: dict, report: Report) -> None:
    sgt = data.get("sanitizer_ground_truth")
    if not isinstance(sgt, dict):
        report.err("sanitizer_ground_truth must be an object")
        return
    required = [
        "detector",
        "trace_format",
        "crash_type",
        "access_type",
        "crash_location",
        "crash_stack",
        "patch_resolves",
        "cross_tool_confirmed",
        "reproduction_rate",
        "flaky",
        "cross_validation",
    ]
    for key in required:
        if key not in sgt:
            report.err(f"sanitizer_ground_truth missing {key}")

    crash_location = sgt.get("crash_location")
    if not isinstance(crash_location, dict):
        report.err("sanitizer_ground_truth.crash_location must be an object")
    else:
        for key in ["file", "function", "line"]:
            if key not in crash_location:
                report.err(f"sanitizer_ground_truth.crash_location missing {key}")

    crash_stack = sgt.get("crash_stack")
    if not isinstance(crash_stack, list) or not crash_stack:
        report.err("sanitizer_ground_truth.crash_stack must be a non-empty list")
    else:
        for idx, frame in enumerate(crash_stack):
            _require_keys(
                frame,
                ["frame", "function", "file", "line"],
                f"sanitizer_ground_truth.crash_stack[{idx}]",
                report,
            )

    cross = sgt.get("cross_validation")
    if not isinstance(cross, dict):
        report.err("sanitizer_ground_truth.cross_validation must be an object")
    else:
        for key in ["sink_matches_crash", "trace_consistent_with_stack", "tainted_value_reaches_sink"]:
            if key not in cross:
                report.err(f"sanitizer_ground_truth.cross_validation missing {key}")


def _check_poc(data: dict, report: Report) -> None:
    poc = data.get("poc")
    if not isinstance(poc, dict) or not poc:
        report.err("poc must be a non-empty object")
        return
    for key in ["path", "trigger", "format"]:
        if key not in poc:
            report.err(f"poc missing {key}")
        elif key != "format" and not str(poc.get(key, "")).strip():
            report.err(f"poc {key} is empty")

    fmt = poc.get("format")
    if not isinstance(fmt, dict) or not fmt:
        report.err("poc.format must be a non-empty object")
    else:
        for key in ["name", "contract"]:
            if key not in fmt:
                report.err(f"poc.format missing {key}")
            elif not str(fmt.get(key, "")).strip():
                report.err(f"poc.format {key} is empty")

    # Quality: a poc.path into a disposable work/ dir dangles after cleanup.
    path = str(poc.get("path", ""))
    if re.match(r"^work[/\\]", path) or "/work/" in path:
        report.warn(
            "poc.path points into a disposable work/ dir; it will dangle after cleanup "
            "(prefer dataset/pocs/<id>/poc or gt_results/<id>/poc)"
        )


def _jsonschema_crosscheck(data: Any, report: Report) -> None:
    """Optional advisory cross-check. The manual checks above are authoritative,
    so any jsonschema-only findings are reported as warnings, never errors."""
    try:
        import jsonschema  # type: ignore
    except Exception:
        report.warn("jsonschema not installed; skipped advisory cross-check")
        return
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for exc in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        report.warn(f"jsonschema: {exc.message} at {list(exc.path)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gt-toolkit validate", description=__doc__)
    parser.add_argument("ground_truth", help="Path to ground_truth.json")
    parser.add_argument("--strict", action="store_true", help="Promote warnings to errors.")
    parser.add_argument("--source-root", type=Path, help="Vulnerable checkout root for line-existence checks.")
    parser.add_argument("--jsonschema", action="store_true", help="Also run the optional jsonschema advisory cross-check.")
    parser.add_argument("--json", action="store_true", help="Emit JSON report only.")
    args = parser.parse_args(argv)

    gt_path = Path(args.ground_truth)
    try:
        data = json.loads(gt_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"ground_truth": str(gt_path), "ok": False, "errors": [f"unreadable: {exc}"]}, indent=2))
        return 1

    report = validate_data(data, ground_truth=str(gt_path), source_root=args.source_root)
    if args.jsonschema:
        _jsonschema_crosscheck(data, report)
    result = report.as_dict(strict=args.strict)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
