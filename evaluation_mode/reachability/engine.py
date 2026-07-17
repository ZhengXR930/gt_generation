"""Deterministic GDB/sanitizer reachability execution engine."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHECKPOINT_KINDS = (
    'parser_admitted',
    'source',
    'root_cause_function',
    'sink_function',
    'root_cause_line',
    'sink_line',
    'assertion_event',
)

_ASSERTION_EVENT_RE = re.compile(r'ASSERT_EVT\s+point=([A-Za-z0-9_.:-]+)')
_TRACE_EVENT_RE = re.compile(r'^ASSERT_EVT\s+(.*)$')
_TRACE_CASE_RE = re.compile(r'^CASE\s+name=(\S+)\s+')
_SOURCE_SUFFIXES = {'.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.rs', '.go'}


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding='utf-8', errors='replace'))
    if not isinstance(data, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return data


def extract_reachability_checkpoints(gt: dict[str, Any]) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    reachability = gt.get('reachability_checkpoints') or {}
    parser_admitted = _resolve_checkpoint(
        reachability.get('parser_admitted') or reachability.get('format_acceptance'),
        gt,
    )
    if parser_admitted:
        parser_admitted['kind'] = 'parser_admitted'
        checkpoints.append(parser_admitted)

    source = _location_from_gt_field(gt, 'source')
    if source:
        source['kind'] = 'source'
        checkpoints.append(source)

    root = _location_from_gt_field(gt, 'root_cause')
    if root:
        root_function = dict(root)
        root_function['kind'] = 'root_cause_function'
        root_function['line'] = None
        checkpoints.append(root_function)
        root_line = dict(root)
        root_line['kind'] = 'root_cause_line'
        checkpoints.append(root_line)

    sink = _location_from_gt_field(gt, 'sink')
    if sink:
        sink_function = dict(sink)
        sink_function['kind'] = 'sink_function'
        sink_function['line'] = None
        checkpoints.append(sink_function)
        sink_line = dict(sink)
        sink_line['kind'] = 'sink_line'
        checkpoints.append(sink_line)

    return _dedupe_checkpoints(checkpoints)


def parse_assertion_trace_events(
    trace_text: str, case_name: str = 'original'
) -> list[dict[str, Any]]:
    """Return the ordered ASSERT_EVT sequence for one frozen trace case."""
    active = False
    events: list[dict[str, Any]] = []
    for raw_line in trace_text.splitlines():
        line = raw_line.strip()
        case_match = _TRACE_CASE_RE.match(line)
        if case_match:
            active = case_match.group(1) == case_name
            continue
        if line == 'ENDCASE':
            active = False
            continue
        event_match = _TRACE_EVENT_RE.match(line)
        if not active or not event_match:
            continue
        fields: dict[str, Any] = {}
        for token in event_match.group(1).split():
            if '=' not in token:
                continue
            key, value = token.split('=', 1)
            fields[key] = value
        point = str(fields.pop('point', '')).strip()
        if point:
            events.append({'point': point, 'fields': fields, 'order': len(events)})
    return events


def extract_assertion_event_checkpoints(
    *,
    codebase: Path,
    trace_text: str,
    assertion_spec: dict[str, Any] | None = None,
    verified_invariants: dict[str, Any] | None = None,
    sink_location: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Resolve trace event names to source breakpoints in the instrumented GT tree.

    Event names remain the join key. Their source locations are recovered by scanning
    the exact Stage 04 codebase for the emitted ASSERT_EVT marker, avoiding another
    hand-authored location schema.
    """
    assertion_spec = assertion_spec or {}
    verified_invariants = verified_invariants or {}
    sink_location = sink_location or {}
    case_name = str(assertion_spec.get('original_case') or 'original')
    events = parse_assertion_trace_events(trace_text, case_name)
    locations = _scan_assertion_markers(codebase)
    roles: dict[str, set[str]] = {}
    assertion_ids: dict[str, list[str]] = {}
    sink_invariant_ids = _sink_invariant_ids(
        verified_invariants, sink_location
    )
    for assertion in assertion_spec.get('assertions', []):
        if not isinstance(assertion, dict):
            continue
        point = str(assertion.get('at') or '')
        if point:
            role = 'root' if assertion.get('kind') == 'required' else 'observed'
            roles.setdefault(point, set()).add(role)
            if sink_invariant_ids.intersection(
                str(item) for item in assertion.get('invariants', [])
            ):
                roles[point].add('sink')
            assertion_ids.setdefault(point, []).append(str(assertion.get('id') or ''))
        protected = str(assertion.get('protects') or '')
        if protected:
            # The frozen required assertion binds this event to the unsafe
            # operation it protects, so an observed protected event is also
            # deterministic source-level sink evidence.
            roles.setdefault(protected, set()).update({'protected', 'sink'})

    checkpoints: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        point = event['point']
        if point in seen:
            continue
        seen.add(point)
        location = locations.get(point, {})
        checkpoints.append({
            'kind': 'assertion_event',
            'event_point': point,
            'assertion_role': sorted(roles.get(point, {'observed'})),
            'assertion_ids': [item for item in assertion_ids.get(point, []) if item],
            'expected_order': event['order'],
            'expected_fields': sorted(event['fields']),
            'file': location.get('file', ''),
            'function': location.get('function', ''),
            'line': location.get('line'),
            'code': location.get('code', ''),
            'note': 'Resolved from ASSERT_EVT marker in the exact GT codebase.',
            'resolved': bool(location),
        })
    return checkpoints


def _sink_invariant_ids(
    verified_invariants: dict[str, Any], sink_location: dict[str, Any]
) -> set[str]:
    """Identify selected sink nodes and edges without guessing from ID text."""
    sink_nodes: set[str] = set()
    for node in verified_invariants.get('nodes', []):
        if not isinstance(node, dict):
            continue
        invariant_id = str(node.get('invariant_id') or '')
        if invariant_id and _same_source_location(node, sink_location):
            sink_nodes.add(invariant_id)
    result = set(sink_nodes)
    for edge in verified_invariants.get('edges', []):
        if not isinstance(edge, dict):
            continue
        if str(edge.get('to_node') or '') in sink_nodes:
            invariant_id = str(edge.get('invariant_id') or '')
            if invariant_id:
                result.add(invariant_id)
    return result


def _same_source_location(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not left or not right:
        return False
    left_function = str(left.get('function') or '')
    right_function = str(right.get('function') or '')
    if not left_function or left_function != right_function:
        return False
    left_file = str(left.get('file') or '').replace('\\', '/')
    right_file = str(right.get('file') or '').replace('\\', '/')
    file_match = bool(
        left_file
        and right_file
        and (left_file.endswith(right_file) or right_file.endswith(left_file))
    )
    left_line = _to_int(left.get('line'))
    right_line = _to_int(right.get('line'))
    return file_match and left_line is not None and left_line == right_line


def _scan_assertion_markers(codebase: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not codebase.is_dir():
        return result
    for path in codebase.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if '.git' in path.parts:
            continue
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            match = _ASSERTION_EVENT_RE.search(line)
            if not match:
                continue
            point = match.group(1)
            statement_index = _event_statement_start(lines, index)
            result.setdefault(point, {
                'file': str(path.relative_to(codebase)),
                'function': _nearest_function(lines, statement_index),
                'line': statement_index + 1,
                'code': lines[statement_index].strip(),
            })
    return result


def _event_statement_start(lines: list[str], marker_index: int) -> int:
    """Prefer the executable call line when the marker is a continued C string."""
    for index in range(marker_index, max(-1, marker_index - 4), -1):
        if re.search(r'\b(?:f?printf|puts)\s*\(', lines[index]):
            return index
        if ';' in lines[index] and index != marker_index:
            break
    return marker_index


def _nearest_function(lines: list[str], marker_index: int) -> str:
    function_re = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*\{\s*$')
    for index in range(marker_index, max(-1, marker_index - 120), -1):
        match = function_re.search(lines[index])
        if match and match.group(1) not in {'if', 'for', 'while', 'switch'}:
            return match.group(1)
    return ''


def _resolve_checkpoint(raw: Any, gt: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    same_as = raw.get('same_as')
    if same_as:
        return _location_from_gt_field(gt, str(same_as))
    if raw.get('status') == 'unavailable':
        return {}
    return _normalize_location(raw)


def _location_from_gt_field(gt: dict[str, Any], field: str) -> dict[str, Any]:
    raw = gt.get(field)
    if isinstance(raw, dict):
        return _normalize_location(raw)
    return {}


def _normalize_location(raw: dict[str, Any]) -> dict[str, Any]:
    file = str(raw.get('file') or '').strip()
    function = str(raw.get('function') or '').strip()
    line = _to_int(raw.get('line'))
    code = str(raw.get('code') or raw.get('statement') or '').strip()
    if not file and not function:
        return {}
    return {
        'file': file,
        'function': function,
        'line': line,
        'code': code,
        'note': raw.get('note') or raw.get('description') or raw.get('meaning') or '',
    }


def _dedupe_checkpoints(checkpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        key = (
            checkpoint.get('kind'),
            checkpoint.get('file'),
            checkpoint.get('function'),
            checkpoint.get('line'),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(checkpoint)
    return result


def write_breakpoint_spec(checkpoints: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({'breakpoints': checkpoints}, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )


def run_gdb_reachability(
    *,
    command: str,
    breakpoints_path: Path,
    hits_path: Path,
    gdb_script: Path,
    timeout: int = 120,
) -> CommandResult:
    argv = shlex.split(command)
    if not argv:
        raise ValueError('Empty debug command')
    hits_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['REACHABILITY_BREAKPOINTS'] = str(breakpoints_path)
    env['REACHABILITY_OUTPUT'] = str(hits_path)
    full_command = [
        'gdb',
        '--batch',
        '-q',
        '-x',
        str(gdb_script),
        '--args',
        *argv,
    ]
    proc = subprocess.run(
        full_command,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    return CommandResult(
        command=full_command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def load_hits(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8', errors='replace'))
    if isinstance(data, dict):
        hits = data.get('hits') or []
    else:
        hits = data
    return [item for item in hits if isinstance(item, dict)]


def parse_sanitizer_trace(trace_text: str) -> dict[str, Any]:
    crash_type = ''
    access_type = ''
    sections: dict[str, list[dict[str, Any]]] = {
        'crash_stack': [],
        'free_stack': [],
        'allocation_stack': [],
    }
    first = re.search(r'ERROR: [^:]+: ([A-Za-z0-9_-]+)', trace_text)
    if first:
        crash_type = first.group(1)
    access = re.search(r'\b(READ|WRITE|FREE) of size\b|\b(READ|WRITE) memory access\b', trace_text)
    if access:
        access_type = next(group for group in access.groups() if group)
    frame_re = re.compile(
        r'#(\d+)\s+0x[0-9a-fA-F]+\s+in\s+(.+?)\s+([^\s:]+):(\d+)(?::(\d+))?'
    )
    current: str | None = 'crash_stack'
    for line in trace_text.splitlines():
        if line.startswith('freed by thread'):
            current = 'free_stack'
            continue
        if line.startswith('previously allocated by thread'):
            current = 'allocation_stack'
            continue
        if line.startswith('SUMMARY:'):
            current = None
        if current is None:
            continue
        match = frame_re.search(line)
        if not match:
            continue
        sections[current].append({
            'frame': int(match.group(1)),
            'function': match.group(2).strip(),
            'file': _trim_project_path(match.group(3)),
            'line': _to_int(match.group(4)),
            'column': _to_int(match.group(5)),
        })

    def first_project_frame(frames: list[dict[str, Any]]) -> dict[str, Any]:
        runtime_markers = (
            'llvm/projects/compiler-rt/',
            'libfuzzer/',
            '__libc_start_main',
            'sanitizer_',
            'asan_',
        )
        for frame in frames:
            file = str(frame.get('file') or '')
            function = str(frame.get('function') or '')
            if not any(marker in file or marker in function for marker in runtime_markers):
                return {
                    'function': function,
                    'file': file,
                    'line': frame.get('line'),
                }
        if frames:
            frame = frames[0]
            return {
                'function': str(frame.get('function') or ''),
                'file': str(frame.get('file') or ''),
                'line': frame.get('line'),
            }
        return {}

    crash_location = first_project_frame(sections['crash_stack'])
    return {
        'crash_type': crash_type,
        'access_type': access_type,
        'crash_location': crash_location,
        'free_context': first_project_frame(sections['free_stack']),
        'allocation_context': first_project_frame(sections['allocation_stack']),
        **sections,
    }


def _trim_project_path(path: str) -> str:
    markers = ['/build_sanitizer/', '/build_valgrind/', '/build_debug/', '/src/', '/work/']
    for marker in markers:
        if marker in path:
            return path.split(marker, 1)[1]
    return path


def _to_int(value: Any) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
