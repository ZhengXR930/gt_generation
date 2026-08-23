"""GDB command file used by evaluation reachability."""

import io
import json
import os
import time

import gdb


BREAKPOINTS_PATH = os.environ.get('REACHABILITY_BREAKPOINTS', 'reachability_breakpoints.json')
OUTPUT_PATH = os.environ.get('REACHABILITY_OUTPUT', 'reachability_hits.json')
MAX_HITS_PER_BREAKPOINT = int(os.environ.get('REACHABILITY_MAX_HITS_PER_BREAKPOINT', '1'))

hits = []


def _record_inferior_exit(event):
    """Treat dynamic-loader failure as infrastructure failure, not R0."""
    try:
        exit_code = event.exit_code
    except AttributeError:
        return
    if exit_code == 127:
        hits.append({
            'run_error': 'inferior exited 127 before candidate execution',
            'inferior_exit_code': exit_code,
        })


def _load_breakpoints():
    with io.open(BREAKPOINTS_PATH, 'r', encoding='utf-8', errors='replace') as handle:
        data = json.load(handle)
    breakpoints = data.get('breakpoints') if isinstance(data, dict) else data
    return [item for item in breakpoints if isinstance(item, dict)]


class ReachabilityBreakpoint(gdb.Breakpoint):
    def __init__(self, spec, checkpoint):
        gdb.Breakpoint.__init__(self, spec, internal=False)
        self.checkpoint = checkpoint
        self.observed_hit_count = 0

    def stop(self):
        self.observed_hit_count += 1
        frame = gdb.selected_frame()
        sal = frame.find_sal()
        symtab = sal.symtab.filename if sal and sal.symtab else ''
        hit = {
            'kind': self.checkpoint.get('kind'),
            'event_point': self.checkpoint.get('event_point'),
            'assertion_role': self.checkpoint.get('assertion_role'),
            'expected_order': self.checkpoint.get('expected_order'),
            'expected_file': self.checkpoint.get('file'),
            'expected_function': self.checkpoint.get('function'),
            'expected_line': self.checkpoint.get('line'),
            'file': symtab,
            'function': frame.name(),
            'line': sal.line if sal else None,
            'timestamp': time.time(),
            'breakpoint_spec': self.location,
            'exact_source_breakpoint': bool(
                self.checkpoint.get('line') is not None and ':' in self.location
            ),
            'hit_count': self.observed_hit_count,
        }
        fields = {}
        capture_errors = {}
        for name, expression in (self.checkpoint.get('captures') or {}).items():
            try:
                value = gdb.parse_and_eval(str(expression))
                fields[str(name)] = _json_value(value)
            except (gdb.error, ValueError, TypeError) as exc:
                capture_errors[str(name)] = str(exc)
        if fields:
            hit['fields'] = fields
        if capture_errors:
            hit['capture_errors'] = capture_errors
        hits.append(hit)
        if self.observed_hit_count >= MAX_HITS_PER_BREAKPOINT:
            self.enabled = False
        return False


def _json_value(value):
    """Convert scalar/pointer GDB values to stable JSON primitives."""
    text = str(value)
    if text in ('true', 'false'):
        return text == 'true'
    if text in ('0x0', '(void *) 0x0', 'nullptr'):
        return 0
    try:
        return int(value)
    except (gdb.error, ValueError, TypeError):
        try:
            return int(text, 0)
        except ValueError:
            return text


def _breakpoint_specs(checkpoint):
    file = str(checkpoint.get('file') or '')
    line = checkpoint.get('line')
    function = str(checkpoint.get('function') or '')
    specs = []
    if file and line:
        for candidate in _file_candidates(file):
            specs.append('{}:{}'.format(candidate, line))
        if checkpoint.get('kind') == 'parser_admitted' and function:
            # Admission is a control-flow boundary. If optimization removes a
            # line-table PC, entry into its recorded continuation function is a
            # valid weaker observation of the same gate.
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
    normalized = file.replace('\\', '/').strip('/')
    parts = normalized.split('/')
    candidates = [file]
    markers = ('repo-vul/src-vul/', 'src-vul/', 'src/', 'source/')
    for marker in markers:
        idx = normalized.find(marker)
        if idx >= 0:
            candidates.append(normalized[idx + len(marker):])
    for idx, part in enumerate(parts):
        if part in ('libarchive', 'src', 'src-vul') and idx + 1 < len(parts):
            candidates.append('/'.join(parts[idx:]))
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
    with io.open(OUTPUT_PATH, 'w', encoding='utf-8') as handle:
        json.dump({'hits': hits}, handle, indent=2, ensure_ascii=True)
        handle.write('\n')


def main():
    gdb.execute('set pagination off')
    gdb.execute('set breakpoint pending on')
    for command in (
        'set follow-exec-mode same',
        # Shell wrappers often run helper commands such as `find` before
        # finally `exec`-ing the project binary.  Stay with the wrapper process
        # and follow its final exec instead of following the helper child.
        'set follow-fork-mode parent',
        'set detach-on-fork on',
    ):
        try:
            gdb.execute(command)
        except gdb.error:
            pass
    checkpoints = _load_breakpoints()
    breakpoint_groups = []
    for checkpoint in checkpoints:
        specs = _breakpoint_specs(checkpoint)
        if not specs:
            continue
        errors = []
        created = []
        for spec in specs:
            try:
                breakpoint = ReachabilityBreakpoint(spec, checkpoint)
                # A valid location may remain pending until a project shared
                # library is loaded. Keep every normalized spelling and decide
                # observability after the inferior has run.
                created.append(breakpoint)
            except gdb.error as exc:
                errors.append('{}: {}'.format(spec, exc))
        if created:
            breakpoint_groups.append((checkpoint, created, errors))
            continue
        hits.append({
            'kind': checkpoint.get('kind'),
            'event_point': checkpoint.get('event_point'),
            'assertion_role': checkpoint.get('assertion_role'),
            'expected_file': checkpoint.get('file'),
            'expected_function': checkpoint.get('function'),
            'expected_line': checkpoint.get('line'),
            'breakpoint_error': '; '.join(errors),
        })
    gdb.events.exited.connect(_record_inferior_exit)
    try:
        gdb.execute('run')
    except gdb.error as exc:
        hits.append({'run_error': str(exc)})
    for checkpoint, breakpoints, errors in breakpoint_groups:
        if any(item.observed_hit_count for item in breakpoints):
            continue
        if any(not item.pending for item in breakpoints):
            continue
        unresolved = errors + [
            '{}: unresolved pending breakpoint'.format(item.location)
            for item in breakpoints
        ]
        hits.append({
            'kind': checkpoint.get('kind'),
            'event_point': checkpoint.get('event_point'),
            'assertion_role': checkpoint.get('assertion_role'),
            'expected_file': checkpoint.get('file'),
            'expected_function': checkpoint.get('function'),
            'expected_line': checkpoint.get('line'),
            'breakpoint_error': '; '.join(unresolved),
        })
    _write_output()


main()
