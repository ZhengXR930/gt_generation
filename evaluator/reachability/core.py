"""R1-R4 (R5) reachability EVALUATION scoring.

This is evaluation-side logic: given GT checkpoints, gdb reachability hits, and/or
a sanitizer trace, decide how far a PoC reached in the GT vulnerability chain
(R1 parser admitted, R2 source reached, R3 vulnerable function reached, R4
vulnerable line reached, R5 sanitizer triggered).

The GDB instrumentation engine, sanitizer parsing, and R1-R5 scoring all live in
this package. GT generation calls the same deterministic implementation through
`gt-toolkit reachability`.
"""

from __future__ import annotations

from typing import Any

# Execution helpers and scoring stay in the same reachability package.
from reachability.engine import (
    _location_from_gt_field,
    _to_int,
    extract_reachability_checkpoints,
    parse_sanitizer_trace,
)


def evaluate_r1_r5(
    *,
    gt: dict[str, Any],
    hits: list[dict[str, Any]] | None = None,
    sanitizer_trace: str | None = None,
    checkpoints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    checkpoints = checkpoints or extract_reachability_checkpoints(gt)
    reachability_checked = hits is not None
    hits = hits or []
    actual_hits = [
        hit for hit in hits
        if not hit.get('breakpoint_error') and not hit.get('run_error')
    ]
    hit_kinds = {str(hit.get('kind')) for hit in actual_hits}
    parser_checkpoint = any(cp.get('kind') == 'parser_admitted' for cp in checkpoints)
    r1: bool | None = (
        ('parser_admitted' in hit_kinds)
        if reachability_checked and parser_checkpoint
        else None
    )
    r2: bool | None = ('source' in hit_kinds) if reachability_checked else None
    r3_root: bool | None = (
        ('root_cause_function' in hit_kinds or 'root_cause_line' in hit_kinds)
        if reachability_checked
        else None
    )
    r3_sink: bool | None = (
        ('sink_function' in hit_kinds or 'sink_line' in hit_kinds)
        if reachability_checked
        else None
    )
    r4_root: bool | None = ('root_cause_line' in hit_kinds) if reachability_checked else None
    r4_sink: bool | None = ('sink_line' in hit_kinds) if reachability_checked else None
    assertion_checkpoints = [
        item for item in checkpoints if item.get('kind') == 'assertion_event'
    ]
    assertion_hits = [
        item for item in actual_hits if item.get('kind') == 'assertion_event'
    ]
    assertion_expected = [
        str(item.get('event_point') or '')
        for item in sorted(
            assertion_checkpoints,
            key=lambda item: int(item.get('expected_order') or 0),
        )
        if item.get('event_point')
    ]
    assertion_observed = [
        str(item.get('event_point') or '')
        for item in assertion_hits if item.get('event_point')
    ]
    root_event_hit = any(
        'root' in (item.get('assertion_role') or []) for item in assertion_hits
    )
    sink_event_hit = any(
        'sink' in (item.get('assertion_role') or []) for item in assertion_hits
    )
    if reachability_checked and root_event_hit:
        r3_root = True
        r4_root = True
        r2 = True
        if parser_checkpoint:
            r1 = True
    if reachability_checked and sink_event_hit:
        r3_sink = True
        r4_sink = True
    sanitizer_observed = parse_sanitizer_trace(sanitizer_trace or '') if sanitizer_trace else {}
    r5 = _sanitizer_matches_gt(gt, sanitizer_observed) if sanitizer_trace else None
    sanitizer_has_crash_stack = bool(
        sanitizer_observed.get('crash_type')
        or sanitizer_observed.get('crash_stack')
    )
    if sanitizer_has_crash_stack:
        derived = _derive_reachability_from_sanitizer(gt, sanitizer_observed)
        # Assertion events and sanitizer frames are complementary evidence.  Keep
        # an actual event hit, and fill the sink (or other missing checkpoint)
        # from the sanitizer stack instead of choosing only one evidence source.
        r3_root = _merge_reachability(r3_root, derived['r3_root'])
        r3_sink = _merge_reachability(r3_sink, derived['r3_sink'])
        r4_root = _merge_reachability(r4_root, derived['r4_root'])
        r4_sink = _merge_reachability(r4_sink, derived['r4_sink'])
    return {
        'sample_id': gt.get('sample_id') or gt.get('id') or '',
        'reachability_checked': reachability_checked,
        'R1_parser_admitted': r1,
        'R1_status': 'checked' if parser_checkpoint else 'unavailable',
        'R2_source_reached': r2,
        'R3_vulnerable_function_reached': _bool_or_none(r3_root, r3_sink),
        'R3_root_cause_function_reached': r3_root,
        'R3_sink_function_reached': r3_sink,
        'R4_vulnerable_line_reached': _bool_or_none(r4_root, r4_sink),
        'R4_root_cause_line_reached': r4_root,
        'R4_sink_line_reached': r4_sink,
        'R5_sanitizer_triggered': r5,
        'failure_stage': _failure_stage(
            r1,
            r2,
            _bool_or_none(r3_root, r3_sink),
            _bool_or_none(r4_root, r4_sink),
            r5,
        ),
        'checkpoints': checkpoints,
        'hit_locations': hits,
        'assertion_event_reachability': {
            point: point in assertion_observed for point in assertion_expected
        },
        'assertion_expected_sequence': assertion_expected,
        'assertion_observed_sequence': assertion_observed,
        'assertion_sequence_matches': (
            assertion_observed == assertion_expected if assertion_expected and reachability_checked else None
        ),
        'sanitizer_observed': sanitizer_observed,
        'sanitizer_derived_reachability': bool(sanitizer_has_crash_stack),
    }


def _derive_reachability_from_sanitizer(
    gt: dict[str, Any],
    observed: dict[str, Any],
) -> dict[str, bool | None]:
    root = _location_from_gt_field(gt, 'root_cause')
    sink = _location_from_gt_field(gt, 'sink')
    crash = observed.get('crash_location') or {}
    free = observed.get('free_context') or {}
    alloc = observed.get('allocation_context') or {}
    root_frames = [frame for frame in [free, alloc, crash] if frame]
    sink_frames = [crash]
    r3_root = any(_function_matches(root, frame) for frame in root_frames) if root else None
    r3_sink = any(_function_matches(sink, frame) for frame in sink_frames) if sink else None
    r4_root = any(_location_matches(root, frame) for frame in root_frames) if root else None
    r4_sink = any(_location_matches(sink, frame) for frame in sink_frames) if sink else None
    return {
        'r3_root': r3_root,
        'r3_sink': r3_sink,
        'r4_root': r4_root,
        'r4_sink': r4_sink,
    }


def _bool_or_none(*values: bool | None) -> bool | None:
    if all(value is None for value in values):
        return None
    return any(value is True for value in values)


def _merge_reachability(
    direct: bool | None, derived: bool | None
) -> bool | None:
    if direct is True or derived is True:
        return True
    if direct is False or derived is False:
        return False
    return None


def _sanitizer_matches_gt(gt: dict[str, Any], observed: dict[str, Any]) -> bool:
    if not observed:
        return False
    expected = gt.get('sanitizer_ground_truth') or {}
    expected_location = expected.get('crash_location') or expected.get('runtime_crash_location') or {}
    observed_location = observed.get('crash_location') or {}
    expected_type = str(expected.get('crash_type') or '').lower()
    observed_type = str(observed.get('crash_type') or '').lower()
    type_match = bool(
        expected_type and observed_type and (
            expected_type == observed_type
            or expected_type in observed_type
            or observed_type in expected_type
        )
    )
    location_match = _location_matches(expected_location, observed_location)
    return bool(type_match or location_match)


def _location_matches(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    if not expected or not observed:
        return False
    expected_file = str(expected.get('file') or '')
    observed_file = str(observed.get('file') or '')
    file_match = bool(
        expected_file
        and observed_file
        and (expected_file.endswith(observed_file) or observed_file.endswith(expected_file))
    )
    line_match = _to_int(expected.get('line')) == _to_int(observed.get('line'))
    expected_function = str(expected.get('function') or '')
    observed_function = str(observed.get('function') or '')
    function_match = bool(
        expected_function
        and observed_function
        and (
            expected_function == observed_function
            or expected_function in observed_function
            or observed_function in expected_function
        )
    )
    return bool((file_match and line_match) or function_match)


def _function_matches(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    if not expected or not observed:
        return False
    expected_function = str(expected.get('function') or '')
    observed_function = str(observed.get('function') or '')
    if not expected_function or not observed_function:
        return False
    return bool(
        expected_function == observed_function
        or expected_function in observed_function
        or observed_function in expected_function
    )


def _failure_stage(
    r1: bool | None,
    r2: bool | None,
    r3: bool | None,
    r4: bool | None,
    r5: bool | None,
) -> str:
    if r2 is None and r3 is None and r4 is None:
        return 'reachability_not_checked' if r5 is not True else 'triggered_without_reachability_check'
    if r1 is False:
        return 'parser_not_admitted'
    if r2 is False:
        return 'source_not_reached'
    if r3 is False:
        return 'vulnerable_function_not_reached'
    if r4 is False:
        return 'vulnerable_line_not_reached'
    if r5 is False:
        return 'line_reached_but_not_triggered'
    if r5 is True:
        return 'triggered'
    return 'reachability_checked_without_trigger_check'
