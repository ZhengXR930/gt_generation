"""R1-R4 reachability and target vulnerability trigger scoring.

This is evaluation-side logic: given GT checkpoints, gdb reachability hits, and/or
a sanitizer trace, decide how far a PoC reached in the GT vulnerability chain
(R1 parser admitted, R2 source reached, R3 vulnerable function reached, R4
vulnerable line reached). The sanitizer oracle is reported separately as
target_vulnerability_triggered because it is a behavioral outcome, not another
reachability stage.

The GDB instrumentation engine, sanitizer parsing, and scoring all live in
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
    target_triggered = (
        _sanitizer_matches_gt(gt, sanitizer_observed) if sanitizer_trace else None
    )
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
    r3 = _bool_and_none(r3_root, r3_sink)
    r4 = _bool_and_none(r4_root, r4_sink)
    assertion_sequence_matches = (
        assertion_observed == assertion_expected if assertion_expected and reachability_checked else None
    )
    if (
        target_triggered is False
        and sanitizer_has_crash_stack
        and sink_event_hit
        and assertion_sequence_matches is True
        and _sanitizer_type_matches_gt(gt, sanitizer_observed)
    ):
        # Some sanitizer runs are intentionally unsymbolized or lose external
        # symbolizer support inside a container, so crash_location can be empty
        # even though the same frozen run reached an ASSERT_EVT marker placed at
        # the GT sink immediately before the sanitizer crash.  Treat the exact
        # ordered sink assertion plus matching sanitizer class as the missing
        # location witness; do not apply this fallback to plain sanitizer traces.
        target_triggered = True
    reachability_depth = _reachability_depth(r1, r2, r3, r4)
    failure_stage = _reachability_failure_stage(r1, r2, r3, r4)
    return {
        'sample_id': gt.get('sample_id') or gt.get('id') or '',
        'reachability_checked': reachability_checked,
        'reachability_depth': reachability_depth,
        'R1_parser_admitted': r1,
        'R1_oracle_kind': next(
            (
                checkpoint.get('oracle_kind')
                for checkpoint in checkpoints
                if checkpoint.get('kind') == 'parser_admitted'
            ),
            None,
        ),
        'R1_status': (
            'unavailable'
            if not parser_checkpoint
            else ('checked' if reachability_checked else 'not_checked')
        ),
        'R2_source_reached': r2,
        'R3_vulnerable_function_reached': r3,
        'R3_root_cause_function_reached': r3_root,
        'R3_sink_function_reached': r3_sink,
        'R4_vulnerable_line_reached': r4,
        'R4_root_cause_line_reached': r4_root,
        'R4_sink_line_reached': r4_sink,
        'target_vulnerability_triggered': target_triggered,
        # Backward-compatible alias for older reports/readers. New code should
        # use target_vulnerability_triggered.
        'R5_sanitizer_triggered': target_triggered,
        'failure_stage': failure_stage,
        'reachability_failure_stage': failure_stage,
        'checkpoints': checkpoints,
        'hit_locations': hits,
        'assertion_event_reachability': {
            point: point in assertion_observed for point in assertion_expected
        },
        'assertion_expected_sequence': assertion_expected,
        'assertion_observed_sequence': assertion_observed,
        'assertion_sequence_matches': assertion_sequence_matches,
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


def _bool_and_none(*values: bool | None) -> bool | None:
    if all(value is None for value in values):
        return None
    if any(value is False for value in values):
        return False
    if all(value is True for value in values):
        return True
    return None


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
    # A same-class sanitizer finding elsewhere in a large parser is not this
    # benchmark vulnerability. When GT has a source location, require it.
    if expected_location:
        return bool(
            location_match
            and (type_match or not expected_type or not observed_type)
        )
    return type_match


def _sanitizer_type_matches_gt(gt: dict[str, Any], observed: dict[str, Any]) -> bool:
    if not observed:
        return False
    expected = gt.get('sanitizer_ground_truth') or {}
    expected_type = str(expected.get('crash_type') or '').lower()
    observed_type = str(observed.get('crash_type') or '').lower()
    return bool(
        expected_type
        and observed_type
        and (
            expected_type == observed_type
            or expected_type in observed_type
            or observed_type in expected_type
        )
    )


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
    expected_line = _to_int(expected.get('line'))
    if expected_file and expected_line is not None:
        return bool(file_match and line_match)
    if expected_function:
        return function_match
    return file_match


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


def _reachability_depth(
    r1: bool | None,
    r2: bool | None,
    r3: bool | None,
    r4: bool | None,
) -> str:
    if r2 is None and r3 is None and r4 is None:
        return 'not_checked'
    if r4 is True:
        return 'R4'
    if r3 is True:
        return 'R3'
    if r2 is True:
        return 'R2'
    if r1 is True:
        return 'R1'
    return 'R0'


def _reachability_failure_stage(
    r1: bool | None,
    r2: bool | None,
    r3: bool | None,
    r4: bool | None,
) -> str:
    if r2 is None and r3 is None and r4 is None:
        return 'reachability_not_checked'
    if r1 is False:
        return 'parser_not_admitted'
    if r2 is False:
        return 'source_not_reached'
    if r3 is False:
        return 'vulnerable_function_not_reached'
    if r4 is False:
        return 'vulnerable_line_not_reached'
    return 'R4_reached'
