"""Location reachability and target vulnerability trigger scoring.

This is evaluation-side logic: given GT checkpoints, gdb reachability hits, and/or
a sanitizer trace, decide how far a PoC reached in the GT vulnerability chain
(R1 input admitted, R2 source reached, R3 root-cause location reached, R4
sink location reached). The sanitizer oracle is reported separately as
target_vulnerability_triggered because it is a behavioral outcome, not another
reachability stage.

The GDB instrumentation engine, sanitizer parsing, and scoring all live in
this package. GT generation calls the same deterministic implementation through
`gt-toolkit reachability`.
"""

from __future__ import annotations

from typing import Any

# Execution helpers and scoring stay in the same reachability package.
from .engine import (
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
    """Score the exact GT location chain without requiring value capture.

    The four stages are a cumulative prefix, but hit timestamps are deliberately
    not ordered: a root condition can be established before a later source is
    observed, and multiple GT anchors can share one statement.  Sanitizer frames
    and assertion events never promote a location stage.
    """
    checkpoints = checkpoints or extract_reachability_checkpoints(gt)
    ledger_supplied = hits is not None
    hits = hits or []
    run_failed = any(hit.get('run_error') for hit in hits)
    reachability_checked = ledger_supplied and not run_failed
    actual_hits = [
        hit for hit in hits
        if not hit.get('breakpoint_error') and not hit.get('run_error')
    ]
    hit_kinds = {
        str(hit.get('kind')) for hit in actual_hits
        if _hit_matches_expected_location(hit)
    }
    mismatched_hit_kinds = {
        str(hit.get('kind')) for hit in actual_hits
        if not _hit_matches_expected_location(hit)
    }
    checkpoint_kinds = {str(cp.get('kind')) for cp in checkpoints}
    breakpoint_error_kinds = {
        str(hit.get('kind')) for hit in hits if hit.get('breakpoint_error')
    }

    def location_hit(kind: str) -> bool | None:
        if not reachability_checked or run_failed or kind not in checkpoint_kinds:
            return None
        if (
            kind in breakpoint_error_kinds or kind in mismatched_hit_kinds
        ) and kind not in hit_kinds:
            return None
        return kind in hit_kinds

    raw_r1 = location_hit('parser_admitted')
    raw_r2 = location_hit('source')
    raw_r3 = location_hit('root_cause_line')
    raw_r4 = location_hit('sink_line')
    r1 = raw_r1
    r2 = _prefix_stage(raw_r1, raw_r2)
    r3 = _prefix_stage(r2, raw_r3)
    r4 = _prefix_stage(r3, raw_r4)
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
    sanitizer_observed = parse_sanitizer_trace(sanitizer_trace or '') if sanitizer_trace else {}
    target_triggered = (
        _sanitizer_matches_gt(gt, sanitizer_observed) if sanitizer_trace else None
    )
    assertion_sequence_matches = (
        assertion_observed == assertion_expected if assertion_expected and reachability_checked else None
    )
    reachability_depth = _reachability_depth(r1, r2, r3, r4)
    failure_stage = _reachability_failure_stage(r1, r2, r3, r4)
    return {
        'sample_id': gt.get('sample_id') or gt.get('id') or '',
        'reachability_checked': reachability_checked,
        'reachability_depth': reachability_depth,
        'evaluation_protocol': 'location-reachability-v3',
        'R1_input_admitted': r1,
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
            if 'parser_admitted' not in checkpoint_kinds
            else ('checked' if reachability_checked else 'not_checked')
        ),
        'R2_source_reached': r2,
        'R3_root_cause_reached': r3,
        'R4_sink_reached': r4,
        'raw_location_hits': {
            'admission': raw_r1,
            'source': raw_r2,
            'root_cause': raw_r3,
            'sink': raw_r4,
        },
        # Deprecated aliases retained for readers of older result files.
        'R3_vulnerable_function_reached': r3,
        'R3_root_cause_function_reached': raw_r3,
        'R3_sink_function_reached': raw_r4,
        'R4_vulnerable_line_reached': r4,
        'R4_root_cause_line_reached': raw_r3,
        'R4_sink_line_reached': raw_r4,
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
        'sanitizer_derived_reachability': False,
    }


def _prefix_stage(previous: bool | None, current: bool | None) -> bool | None:
    if previous is False:
        return False
    if previous is None or current is None:
        return None
    return current


def _hit_matches_expected_location(hit: dict[str, Any]) -> bool:
    """Reject a function-fallback hit masquerading as an exact line hit."""
    expected_line = _to_int(hit.get('expected_line'))
    observed_line = _to_int(hit.get('line'))
    # GDB may resolve an exact file:line breakpoint to the callee entry or a
    # neighboring statement under optimization/inlining.  The breakpoint was
    # still placed from the exact GT source specification; function fallbacks
    # are never marked this way.
    if expected_line is not None and hit.get('exact_source_breakpoint') is True:
        return True
    if (
        expected_line is not None
        and hit.get('kind') == 'parser_admitted'
        and str(hit.get('expected_function') or '')
        == str(hit.get('function') or '')
    ):
        return True
    if expected_line is not None and observed_line != expected_line:
        return False
    expected_file = str(hit.get('expected_file') or '').replace('\\', '/')
    observed_file = str(hit.get('file') or '').replace('\\', '/')
    if expected_file and observed_file and not (
        expected_file.endswith(observed_file)
        or observed_file.endswith(expected_file)
    ):
        return False
    expected_function = str(hit.get('expected_function') or '')
    observed_function = str(hit.get('function') or '')
    if expected_function and observed_function and not (
        expected_function == observed_function
        or expected_function in observed_function
        or observed_function in expected_function
    ):
        return False
    return True


# Canonical crash classes, longest patterns first so "heap-buffer-overflow"
# is not swallowed by "buffer-overflow".
_CRASH_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("use-after-free", ("use-after-free", "use after free", "heap-use-after-free")),
    ("double-free", ("double-free", "double free")),
    ("bad-free", ("bad-free", "bad free", "invalid free", "attempting free")),
    ("heap-buffer-overflow", ("heap-buffer-overflow", "heap buffer overflow")),
    ("stack-buffer-overflow", ("stack-buffer-overflow", "stack buffer overflow")),
    ("global-buffer-overflow", ("global-buffer-overflow", "global buffer overflow")),
    ("stack-overflow", ("stack-overflow", "stack exhaustion")),
    ("uninitialized-value", (
        "use-of-uninitialized-value", "uninitialised", "uninitialized",
    )),
    ("memory-leak", ("memory leak", "detected memory leaks")),
    ("bad-cast", ("bad-cast", "bad cast", "downcast")),
    ("null-dereference", (
        "null-dereference", "null pointer", "null-pointer", "segv on unknown address 0x000000000000",
    )),
    ("integer-overflow", (
        "integer-overflow", "signed integer overflow", "unsigned integer overflow",
    )),
    ("shift", ("shift exponent", "left shift of negative")),
    ("misaligned", ("misaligned address", "load of misaligned")),
    ("segv", ("segv", "segmentation fault")),
    # Deliberately last: UBSan prints this as the umbrella label for findings the
    # entries above name precisely.
    ("undefined-behavior", ("undefined-behavior", "undefined behaviour", "runtime error")),
)


def _crash_class(text: Any) -> str | None:
    """Reduce a crash description to a canonical class, or None if it has none."""
    value = str(text or "").lower()
    if not value.strip():
        return None
    for name, patterns in _CRASH_CLASSES:
        if any(pattern in value for pattern in patterns):
            return name
    return None


def _detectors_agree(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    """Whether both sides name the same sanitizer."""
    def tokens(source: dict[str, Any]) -> set[str]:
        found = set()
        for key in ("detector", "sanitizer"):
            value = str(source.get(key) or "").lower()
            for name, aliases in (
                ("address", ("address", "asan")),
                ("undefined", ("undefined", "ubsan")),
                ("memory", ("memorysanitizer", "msan")),
                ("thread", ("thread", "tsan")),
                ("leak", ("leak", "lsan")),
            ):
                if any(alias in value for alias in aliases):
                    found.add(name)
        return found

    expected_tokens, observed_tokens = tokens(expected), tokens(observed)
    return bool(expected_tokens and observed_tokens and expected_tokens & observed_tokens)


def _sanitizer_matches_gt(gt: dict[str, Any], observed: dict[str, Any]) -> bool:
    if not observed:
        return False
    expected = gt.get('sanitizer_ground_truth') or {}
    expected_locations = [
        item
        for item in (
            expected.get('crash_location'),
            expected.get('runtime_crash_location'),
        )
        if isinstance(item, dict) and item
    ]
    observed_locations = [
        item
        for item in [
            observed.get('crash_location'),
            *(observed.get('crash_stack') or []),
        ]
        if isinstance(item, dict) and item
    ]
    expected_type = str(expected.get('crash_type') or '').lower()
    observed_type = str(observed.get('crash_type') or '').lower()
    expected_class = _crash_class(expected_type)
    observed_class = _crash_class(observed_type) or _crash_class(observed.get('sanitizer'))
    if expected_class and observed_class:
        # Both sides carry a class: a different finding at the same line is
        # still a different finding.
        type_match = expected_class == observed_class
    else:
        # GT's crash_type is free prose and may name no class at all. Keep the
        # old substring test where it can decide, otherwise fall back to the
        # sanitizer actually agreeing rather than failing on wording.
        type_match = bool(
            expected_type and observed_type and (
                expected_type == observed_type
                or expected_type in observed_type
                or observed_type in expected_type
            )
        ) or _detectors_agree(expected, observed)
    location_match = any(
        _location_matches(expected_location, observed_location)
        for expected_location in expected_locations
        for observed_location in observed_locations
    )
    # A same-class sanitizer finding elsewhere in a large parser is not this
    # benchmark vulnerability. When GT has a source location, require it.
    if expected_locations:
        return bool(
            location_match
            and (type_match or not expected_type or not observed_type)
        )
    return type_match


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
        return 'input_not_admitted'
    if r2 is False:
        return 'source_not_reached'
    if r3 is False:
        return 'root_cause_not_reached'
    if r4 is False:
        return 'sink_not_reached'
    return 'R4_reached'
