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
)


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
    if field == 'source':
        for step in gt.get('fine_trace') or []:
            if isinstance(step, dict) and step.get('role') in {
                'source',
                'tainted_read',
                'input_materialization',
                'materialization',
            }:
                return _normalize_location(step)
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
