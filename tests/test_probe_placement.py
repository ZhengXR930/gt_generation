"""A probe hoisted above the guard must be reported, not silently failed.

Stage 04 lets the agent hand-write the C instrumentation. When it emits a
block's probes together at the top, the "the dangerous operation ran" event
fires even on a fixed build whose new guard returns first, so `guarded` becomes
unreachable and a correct patch looks like a broken one.
"""
from gt_generation.gt_toolkit.assertions import (
    assertion_content_hash,
    parse_trace_matrix,
    validate_assertions,
)

SPEC = {
    "schema_version": "assertion-spec-v3",
    "sample_id": "probe_case",
    "original_case": "original",
    "assertions": [{
        "id": "REQ_bytes_within_section",
        "invariants": ["root_missing_bounds_check"],
        "kind": "required",
        "at": "branch",
        "protects": "copy",
        "check": ["le", "$branch.needed", "$branch.available"],
    }],
}
SPEC["content_hash"] = assertion_content_hash(SPEC)

VULNERABLE = (
    "CASE name=original rc=1 result=crash\n"
    "ASSERT_EVT point=branch needed=8 available=2\n"
    "ASSERT_EVT point=copy src=0x1\n"
    "ENDCASE\n"
)


def _run(fixed_trace):
    return validate_assertions(
        SPEC,
        parse_trace_matrix(VULNERABLE),
        parse_trace_matrix(fixed_trace),
    )


def test_probe_before_guard_is_reported_as_misplaced():
    # The fix returns before the copy, but the probe was hoisted above the
    # guard, so the copy event still fires on a run that exits cleanly.
    out = _run(
        "CASE name=original rc=0 result=clean\n"
        "ASSERT_EVT point=branch needed=8 available=2\n"
        "ASSERT_EVT point=copy src=0x1\n"
        "ENDCASE\n"
    )
    assert out["differential_status"] == "probe_misplaced"
    item = out["assertions"][0]
    assert not item["verified"]
    assert "protected operation" in item["probe_placement_error"]


def test_correctly_placed_probe_is_not_flagged():
    # Probe sits after the guard, so the copy event is absent once the fix
    # skips it: predicate violated with the operation not performed.
    out = _run(
        "CASE name=original rc=0 result=clean\n"
        "ASSERT_EVT point=branch needed=8 available=2\n"
        "ENDCASE\n"
    )
    item = out["assertions"][0]
    assert "probe_placement_error" not in item
    assert out["differential_status"] != "probe_misplaced"


def test_fixed_run_that_still_crashes_stays_a_sample_problem():
    # The patch genuinely fails to fix the bug. That must remain
    # distinguishable from instrumentation that observed the wrong statement.
    out = _run(
        "CASE name=original rc=1 result=crash\n"
        "ASSERT_EVT point=branch needed=8 available=2\n"
        "ASSERT_EVT point=copy src=0x1\n"
        "ENDCASE\n"
    )
    assert out["differential_status"] == "vulnerable_side_only"
    assert "probe_placement_error" not in out["assertions"][0]
