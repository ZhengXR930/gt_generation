"""Crash signature parsing and matching helpers for reachability scoring."""

from __future__ import annotations

import posixpath
import re
from typing import Any


SOURCE_SUFFIXES = {'.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.rs', '.go'}

_FRAME_PREFIX_RE = re.compile(
    r'^\s*#(?P<frame>\d+)\s+0x[0-9a-fA-F]+\s+in\s+(?P<body>.+?)\s*$'
)
_LOCATION_WITH_LINE_RE = re.compile(
    r'\s+(?P<file>\S+?):(?P<line>\d+)(?::(?P<column>\d+))?$'
)


def parse_sanitizer_stack_frame(line: str) -> dict[str, Any] | None:
    """Parse one sanitizer stack frame into function/file/line fields.

    Sanitizer stack frames are not a single regular language in practice: C++
    template names and argument lists contain spaces, while optimized frames can
    omit the source line.  Parse the stable frame prefix first, then split the
    trailing source location from the right.
    """
    frame_match = _FRAME_PREFIX_RE.match(line)
    if not frame_match:
        return None
    body = frame_match.group('body').strip()
    location = _split_trailing_source_location(body)
    if not location:
        return None
    return {
        'frame': int(frame_match.group('frame')),
        'function': location['function'],
        'file': trim_project_path(location['file']),
        'line': _to_int(location.get('line')),
        'column': _to_int(location.get('column')),
    }


def trim_project_path(path: str) -> str:
    path = str(path or '').strip().replace('\\', '/')
    markers = ('/build_sanitizer/', '/build_valgrind/', '/build_debug/', '/src/', '/work/')
    for marker in markers:
        if marker in path:
            path = path.split(marker, 1)[1]
            break
    return normalize_source_path(path)


def normalize_source_path(path: str) -> str:
    path = str(path or '').strip().replace('\\', '/')
    if '@' in path:
        path = path.split('@', 1)[0]
    if not path:
        return ''
    normalized = posixpath.normpath(path)
    return '' if normalized == '.' else normalized


def source_path_matches(left: str, right: str) -> bool:
    left_path = normalize_source_path(left)
    right_path = normalize_source_path(right)
    if not left_path or not right_path:
        return False
    if left_path == right_path:
        return True
    left_parts = _path_parts(left_path)
    right_parts = _path_parts(right_path)
    if not left_parts or not right_parts:
        return False
    return _parts_endwith(left_parts, right_parts) or _parts_endwith(right_parts, left_parts)


def function_matches(left: str, right: str) -> bool:
    left_aliases = _function_aliases(left)
    right_aliases = _function_aliases(right)
    if not left_aliases or not right_aliases:
        return False
    if left_aliases & right_aliases:
        return True
    return any(
        a.endswith(f'::{b}') or b.endswith(f'::{a}')
        for a in left_aliases
        for b in right_aliases
        if a and b
    )


def location_matches(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    if not expected or not observed:
        return False
    expected_file = normalize_source_path(str(expected.get('file') or ''))
    observed_file = normalize_source_path(str(observed.get('file') or ''))
    file_match = source_path_matches(expected_file, observed_file)
    expected_line = _to_int(expected.get('line'))
    observed_line = _to_int(observed.get('line'))
    expected_function = str(expected.get('function') or '')
    observed_function = str(observed.get('function') or '')
    function_match = function_matches(expected_function, observed_function)
    if expected_file and expected_line is not None:
        if not file_match:
            return False
        if observed_line is not None:
            return expected_line == observed_line
        return function_match
    if expected_function:
        return function_match
    return file_match


def _split_trailing_source_location(body: str) -> dict[str, Any] | None:
    match = _LOCATION_WITH_LINE_RE.search(body)
    if match:
        function = body[:match.start()].strip()
        if function:
            return {
                'function': function,
                'file': match.group('file'),
                'line': match.group('line'),
                'column': match.group('column'),
            }
    function, separator, file = body.rpartition(' ')
    if separator and function.strip() and _looks_like_source_path(file):
        return {'function': function.strip(), 'file': file.strip(), 'line': None, 'column': None}
    return None


def _looks_like_source_path(path: str) -> bool:
    candidate = str(path or '').strip().strip('()').replace('\\', '/')
    if not candidate:
        return False
    suffix = posixpath.splitext(candidate)[1].lower()
    return suffix in SOURCE_SUFFIXES or ('/' in candidate and bool(suffix))


def _path_parts(path: str) -> list[str]:
    return [part for part in normalize_source_path(path).split('/') if part and part != '.']


def _parts_endwith(parts: list[str], suffix: list[str]) -> bool:
    return len(parts) >= len(suffix) and parts[-len(suffix):] == suffix


def _function_aliases(value: str) -> set[str]:
    normalized = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not normalized:
        return set()
    aliases = {normalized}
    without_args = re.sub(r'\([^()]*\)\s*(?:const)?$', '', normalized).strip()
    if without_args:
        aliases.add(without_args)
        aliases.add(without_args.rsplit('::', 1)[-1])
    aliases.add(normalized.rsplit('::', 1)[-1])
    return {alias for alias in aliases if alias}


def _to_int(value: Any) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
