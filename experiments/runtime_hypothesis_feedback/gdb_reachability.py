"""Experiment-local GDB command file for bounded hypothesis observations."""

import io
import json
import os
import time

import gdb


BREAKPOINTS_PATH = os.environ.get(
    "REACHABILITY_BREAKPOINTS", "reachability_breakpoints.json"
)
OUTPUT_PATH = os.environ.get("REACHABILITY_OUTPUT", "reachability_hits.json")
MAX_HITS_PER_BREAKPOINT = int(
    os.environ.get("REACHABILITY_MAX_HITS_PER_BREAKPOINT", "1")
)

hits = []
event_sequence = 0
call_sequence = 0


def _load_breakpoints():
    with io.open(BREAKPOINTS_PATH, "r", encoding="utf-8", errors="replace") as handle:
        data = json.load(handle)
    breakpoints = data.get("breakpoints") if isinstance(data, dict) else data
    return [item for item in breakpoints if isinstance(item, dict)]


class ReachabilityBreakpoint(gdb.Breakpoint):
    def __init__(self, spec, checkpoint):
        gdb.Breakpoint.__init__(self, spec, internal=False)
        self.checkpoint = checkpoint
        self.observed_hit_count = 0
        self.max_hits = int(
            checkpoint.get("max_hits_per_breakpoint") or MAX_HITS_PER_BREAKPOINT
        )

    def stop(self):
        global event_sequence, call_sequence
        frame = gdb.selected_frame()
        observed_frame = frame
        caller_frame = None
        if self.checkpoint.get("kind") == "call_observation" and self.checkpoint.get(
            "break_function"
        ):
            caller_frame = frame.older()
            if caller_frame is None or not _caller_matches_checkpoint(
                caller_frame, self.checkpoint
            ):
                return False
            observed_frame = caller_frame
        self.observed_hit_count += 1
        event_sequence += 1
        sal = observed_frame.find_sal()
        symtab = sal.symtab.filename if sal and sal.symtab else ""
        hit = {
            "kind": self.checkpoint.get("kind"),
            "event_point": self.checkpoint.get("event_point"),
            "assertion_role": self.checkpoint.get("assertion_role"),
            "expected_order": self.checkpoint.get("expected_order"),
            "expected_file": self.checkpoint.get("file"),
            "expected_function": self.checkpoint.get("function"),
            "expected_line": self.checkpoint.get("line"),
            "file": symtab,
            "function": observed_frame.name(),
            "line": sal.line if sal else None,
            "timestamp": time.time(),
            "event_sequence": event_sequence,
            "breakpoint_spec": self.location,
            "hit_count": self.observed_hit_count,
        }
        if self.checkpoint.get("static_branch_facts"):
            hit["static_branch_facts"] = dict(
                self.checkpoint.get("static_branch_facts") or {}
            )
        if self.checkpoint.get("branch_predicate"):
            hit["branch_predicate"] = self.checkpoint.get("branch_predicate")
            hit["branch_outcome"] = bool(self.checkpoint.get("branch_outcome"))
        fields = {}
        capture_errors = {}
        for name, expression in (self.checkpoint.get("captures") or {}).items():
            try:
                value = gdb.parse_and_eval(str(expression))
                fields[str(name)] = _json_value(value)
            except (gdb.error, ValueError, TypeError) as exc:
                capture_errors[str(name)] = str(exc)
        if caller_frame is not None:
            newest = frame
            try:
                caller_frame.select()
                for name, expression in (
                    self.checkpoint.get("caller_captures") or {}
                ).items():
                    try:
                        value = gdb.parse_and_eval(str(expression))
                        fields[str(name)] = _json_value(value)
                    except (gdb.error, ValueError, TypeError) as exc:
                        capture_errors[str(name)] = str(exc)
            finally:
                newest.select()
        if fields:
            hit["fields"] = fields
        if capture_errors:
            hit["capture_errors"] = capture_errors
        if self.checkpoint.get("kind") == "call_observation":
            call_sequence += 1
            instance = "{}:{}".format(
                self.checkpoint.get("event_point"), call_sequence
            )
            hit["call_instance_id"] = instance
            hit["call_name"] = self.checkpoint.get("call_name")
            hit["requested_capture"] = self.checkpoint.get("requested_capture")
            hit["return_capture"] = self.checkpoint.get("return_capture")
            hit["branch_captures"] = self.checkpoint.get("branch_captures") or []
            hit["argument_metadata"] = self.checkpoint.get("argument_metadata") or []
            hit["derived_relations"] = self.checkpoint.get("derived_relations") or []
            hit["source_code"] = self.checkpoint.get("code")
            hit["source_requested_expression"] = self.checkpoint.get(
                "source_requested_expression"
            )
            try:
                ReturnObservationBreakpoint(frame, self.checkpoint, instance)
            except (gdb.error, ValueError, TypeError) as exc:
                hit["return_capture_error"] = str(exc)
        hits.append(hit)
        if self.observed_hit_count >= self.max_hits:
            self.enabled = False
        return False


def _normalized_file(value):
    return str(value or "").replace("\\", "/").strip("/")


def _same_file(left, right):
    left = _normalized_file(left)
    right = _normalized_file(right)
    return bool(
        left and right
        and (left == right or left.endswith("/" + right) or right.endswith("/" + left))
    )


def _caller_matches_checkpoint(frame, checkpoint):
    sal = frame.find_sal()
    actual_file = sal.symtab.filename if sal and sal.symtab else ""
    actual_line = sal.line if sal else None
    expected_file = checkpoint.get("file")
    expected_line = checkpoint.get("line")
    expected_function = str(checkpoint.get("function") or "").split("(")[0]
    actual_function = str(frame.name() or "")
    if expected_file and not _same_file(expected_file, actual_file):
        return False
    if isinstance(expected_line, int) and (
        not isinstance(actual_line, int) or abs(actual_line - expected_line) > 1
    ):
        return False
    if expected_function and expected_function not in actual_function:
        return False
    return True


class ReturnObservationBreakpoint(gdb.FinishBreakpoint):
    """Record the real return value for one source-derived callsite hit."""

    def __init__(self, frame, checkpoint, call_instance_id):
        gdb.FinishBreakpoint.__init__(self, frame, internal=True)
        self.checkpoint = checkpoint
        self.call_instance_id = call_instance_id
        self.silent = True

    def stop(self):
        global event_sequence
        event_sequence += 1
        frame = gdb.selected_frame()
        sal = frame.find_sal()
        symtab = sal.symtab.filename if sal and sal.symtab else ""
        hit = {
            "kind": "call_return_observation",
            "event_point": str(self.checkpoint.get("event_point")) + ":return",
            "call_instance_id": self.call_instance_id,
            "call_name": self.checkpoint.get("call_name"),
            "expected_file": self.checkpoint.get("file"),
            "expected_function": self.checkpoint.get("function"),
            "expected_line": self.checkpoint.get("line"),
            "file": symtab,
            "function": frame.name(),
            "line": sal.line if sal else None,
            "timestamp": time.time(),
            "event_sequence": event_sequence,
        }
        capture = str(self.checkpoint.get("return_capture") or "returned_value")
        try:
            value = self.return_value
            if value is None:
                # On optimized/stripped x86-64 builds FinishBreakpoint may not
                # expose return_value, while the integer ABI return register is
                # still authoritative at this stop.
                value = gdb.parse_and_eval("(int)$eax")
            hit["fields"] = {capture: _json_value(value)}
        except (gdb.error, ValueError, TypeError) as exc:
            hit["capture_errors"] = {capture: str(exc)}
        hits.append(hit)
        return False


def _json_value(value):
    text = str(value)
    if text in ("true", "false"):
        return text == "true"
    if text in ("0x0", "(void *) 0x0", "nullptr"):
        return 0
    try:
        return int(value)
    except (gdb.error, ValueError, TypeError):
        try:
            return int(text, 0)
        except ValueError:
            return text


def _breakpoint_specs(checkpoint):
    file = str(checkpoint.get("file") or "")
    line = checkpoint.get("line")
    function = str(checkpoint.get("function") or "")
    specs = []
    break_function = str(checkpoint.get("break_function") or "")
    if break_function:
        return [break_function]
    if file and line:
        for candidate in _file_candidates(file):
            specs.append("{}:{}".format(candidate, line))
    if function and checkpoint.get("allow_function_fallback", True):
        specs.append(function)
    result = []
    seen = set()
    for spec in specs:
        if spec and spec not in seen:
            seen.add(spec)
            result.append(spec)
    return result


def _file_candidates(file):
    normalized = file.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    candidates = [file]
    markers = ("repo-vul/src-vul/", "src-vul/", "src/", "source/")
    for marker in markers:
        idx = normalized.find(marker)
        if idx >= 0:
            candidates.append(normalized[idx + len(marker):])
    for idx, part in enumerate(parts):
        if part in ("libarchive", "src", "src-vul") and idx + 1 < len(parts):
            candidates.append("/".join(parts[idx:]))
    if parts:
        candidates.append(parts[-1])
    result = []
    seen = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _write_output():
    with io.open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump({"hits": hits}, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def main():
    gdb.execute("set pagination off")
    gdb.execute("set breakpoint pending on")
    checkpoints = _load_breakpoints()
    for checkpoint in checkpoints:
        specs = _breakpoint_specs(checkpoint)
        if not specs:
            continue
        errors = []
        created = False
        for spec in specs:
            try:
                ReachabilityBreakpoint(spec, checkpoint)
                created = True
                break
            except gdb.error as exc:
                errors.append("{}: {}".format(spec, exc))
        if created:
            continue
        hits.append({
            "kind": checkpoint.get("kind"),
            "event_point": checkpoint.get("event_point"),
            "assertion_role": checkpoint.get("assertion_role"),
            "expected_file": checkpoint.get("file"),
            "expected_function": checkpoint.get("function"),
            "expected_line": checkpoint.get("line"),
            "breakpoint_error": "; ".join(errors),
        })
    try:
        gdb.execute("run")
    except gdb.error as exc:
        hits.append({"run_error": str(exc)})
    _write_output()


main()
