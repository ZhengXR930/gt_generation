"""GDB command file used to collect GT context traces.

The script is intentionally anchor-based instead of line-by-line stepping.  It
sets breakpoints at deterministic GT checkpoints, records a bounded backtrace
when a checkpoint is reached, then continues execution.  This keeps one PoC run
cheap enough to use during GT generation and avoids turning context collection
into whole-program tracing.
"""

import io
import json
import os
import time

import gdb


BREAKPOINTS_PATH = os.environ.get("CONTEXT_BREAKPOINTS", "context_breakpoints.json")
OUTPUT_PATH = os.environ.get("CONTEXT_OUTPUT", "context_hits.json")
SOURCE_ROOT = os.environ.get("CONTEXT_SOURCE_ROOT", "")
MAX_EVENTS = int(os.environ.get("CONTEXT_MAX_EVENTS", "200"))
MAX_HITS_PER_BREAKPOINT = int(os.environ.get("CONTEXT_MAX_HITS_PER_BREAKPOINT", "1"))
BACKTRACE_LIMIT = int(os.environ.get("CONTEXT_BACKTRACE_LIMIT", "32"))

events = []
final_stop = {}
breakpoints = []
breakpoints_by_spec = {}


def _load_breakpoints():
    with io.open(BREAKPOINTS_PATH, "r", encoding="utf-8", errors="replace") as handle:
        data = json.load(handle)
    items = data.get("breakpoints") if isinstance(data, dict) else data
    return [item for item in items if isinstance(item, dict)]


def _frame_dict(frame):
    try:
        sal = frame.find_sal()
        symtab = sal.symtab.filename if sal and sal.symtab else ""
        line = int(sal.line) if sal and sal.line else None
    except Exception:
        symtab = ""
        line = None
    try:
        function = frame.name() or ""
    except Exception:
        function = ""
    return {
        "function": function,
        "file": symtab,
        "line": line,
        "code": _read_code_line(symtab, line),
    }


def _read_code_line(file, line):
    if not file or not line:
        return ""
    candidates = _source_candidates(file)
    for path in candidates:
        try:
            with io.open(path, "r", encoding="utf-8", errors="replace") as handle:
                for index, raw_line in enumerate(handle, 1):
                    if index == line:
                        return raw_line.strip()
        except Exception:
            pass
    return ""


def _source_candidates(file):
    normalized = str(file).replace("\\", "/").strip("/")
    candidates = [file, "/" + normalized]
    if SOURCE_ROOT:
        parts = normalized.split("/")
        for index in range(len(parts)):
            suffix = "/".join(parts[index:])
            if suffix:
                candidates.append(os.path.join(SOURCE_ROOT, suffix))
        if parts:
            candidates.append(os.path.join(SOURCE_ROOT, parts[-1]))
    result = []
    seen = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _backtrace(limit=BACKTRACE_LIMIT):
    frames = []
    try:
        frame = gdb.newest_frame()
        while frame is not None and len(frames) < limit:
            frames.append(_frame_dict(frame))
            frame = frame.older()
    except Exception:
        pass
    return frames


class ContextBreakpoint(gdb.Breakpoint):
    def __init__(self, spec, checkpoint):
        gdb.Breakpoint.__init__(self, spec, internal=False)
        self.spec_value = spec
        self.checkpoints = [checkpoint]
        self.observed_hit_count = 0

    def add_checkpoint(self, checkpoint):
        self.checkpoints.append(checkpoint)

    def stop(self):
        self.observed_hit_count += 1
        frame = gdb.selected_frame()
        hit = _frame_dict(frame)
        stack = _backtrace()
        for checkpoint in self.checkpoints:
            if len(events) >= MAX_EVENTS:
                break
            events.append({
                "sequence": len(events),
                "timestamp": time.time(),
                "anchor": {
                    "kind": checkpoint.get("kind"),
                    "event_point": checkpoint.get("event_point"),
                    "assertion_role": checkpoint.get("assertion_role"),
                    "expected_file": checkpoint.get("file"),
                    "expected_function": checkpoint.get("function"),
                    "expected_line": checkpoint.get("line"),
                    "expected_code": checkpoint.get("code"),
                },
                "hit": hit,
                "breakpoint_spec": self.spec_value,
                "hit_count": self.observed_hit_count,
                "stack": stack,
            })
        if self.observed_hit_count >= MAX_HITS_PER_BREAKPOINT:
            self.enabled = False
        if len(events) >= MAX_EVENTS:
            return True
        return False


def _breakpoint_specs(checkpoint):
    file = str(checkpoint.get("file") or "")
    line = checkpoint.get("line")
    function = str(checkpoint.get("function") or "")
    specs = []
    if file and line:
        for candidate in _file_candidates(file):
            specs.append("{}:{}".format(candidate, line))
        if function:
            specs.append(function)
    elif function:
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
    markers = ("repo-vul/src-vul/", "src-vul/", "src/", "source/", "_work/src/")
    for marker in markers:
        idx = normalized.find(marker)
        if idx >= 0:
            candidates.append(normalized[idx + len(marker):])
    for idx, part in enumerate(parts):
        if part in ("libarchive", "src", "source", "src-vul") and idx + 1 < len(parts):
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


def _record_final_stop(reason):
    global final_stop
    if final_stop:
        return
    try:
        frame = gdb.selected_frame()
        final_stop = {
            "reason": reason,
            "hit": _frame_dict(frame),
            "stack": _backtrace(),
        }
    except Exception:
        final_stop = {"reason": reason}


def _record_inferior_exit(event):
    try:
        code = event.exit_code
    except AttributeError:
        code = None
    _record_final_stop("inferior_exit")
    final_stop["inferior_exit_code"] = code


def _write_output():
    payload = {
        "events": events,
        "final_stop": final_stop,
        "truncated": len(events) >= MAX_EVENTS,
    }
    with io.open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def main():
    gdb.execute("set pagination off")
    gdb.execute("set confirm off")
    gdb.execute("set breakpoint pending off")
    if SOURCE_ROOT:
        try:
            gdb.execute("directory {}".format(SOURCE_ROOT))
        except gdb.error:
            pass
    checkpoints = _load_breakpoints()
    for checkpoint in checkpoints:
        specs = _breakpoint_specs(checkpoint)
        if not specs:
            events.append({
                "anchor": {
                    "kind": checkpoint.get("kind"),
                    "event_point": checkpoint.get("event_point"),
                    "expected_file": checkpoint.get("file"),
                    "expected_function": checkpoint.get("function"),
                    "expected_line": checkpoint.get("line"),
                },
                "breakpoint_error": "no breakpoint spec",
            })
            continue
        created = []
        errors = []
        for spec in specs:
            existing = breakpoints_by_spec.get(spec)
            if existing is not None:
                existing.add_checkpoint(checkpoint)
                created.append(existing)
                break
            try:
                breakpoint = ContextBreakpoint(spec, checkpoint)
                breakpoints_by_spec[spec] = breakpoint
                created.append(breakpoint)
                break
            except gdb.error as exc:
                errors.append("{}: {}".format(spec, exc))
        if created:
            breakpoints.extend(created)
        else:
            events.append({
                "anchor": {
                    "kind": checkpoint.get("kind"),
                    "event_point": checkpoint.get("event_point"),
                    "expected_file": checkpoint.get("file"),
                    "expected_function": checkpoint.get("function"),
                    "expected_line": checkpoint.get("line"),
                },
                "breakpoint_error": "; ".join(errors),
            })
    gdb.events.exited.connect(_record_inferior_exit)
    try:
        gdb.execute("run")
    except gdb.error as exc:
        _record_final_stop(str(exc))
    if not final_stop:
        _record_final_stop("stopped")
    _write_output()


main()
