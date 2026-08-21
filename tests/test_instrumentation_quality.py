from gt_generation.gt_toolkit.instrumentation_quality import (
    required_runtime_fields,
    validate_instrumentation_runtime_fields,
)


def _spec():
    return {
        "schema_version": "assertion-spec-v3",
        "sample_id": "sample",
        "assertions": [
            {
                "id": "root.assertion",
                "kind": "required",
                "at": "root",
                "mechanism": "lifetime",
                "check": ["eq", "$root.free_before_use", "$root.false_literal"],
                "invariants": ["root.invariant"],
            }
        ],
    }


def _bindings():
    return {
        "bindings": {
            "root.free_before_use": {"expr": "free_before_use"},
            "root.false_literal": {"expr": "0"},
        }
    }


def test_required_runtime_fields_excludes_literal_operands():
    assert required_runtime_fields(_spec(), _bindings()) == {
        ("root", "free_before_use")
    }


def test_runtime_field_quality_rejects_required_field_hardcoded_as_printf_arg():
    patch = """diff --git a/src/parser.c b/src/parser.c
--- a/src/parser.c
+++ b/src/parser.c
@@ -1 +1,2 @@
 old
++fprintf(stderr, "ASSERT_EVT point=root free_before_use=%d false_literal=%d\\n", 0, 0);
"""

    report = validate_instrumentation_runtime_fields(
        spec=_spec(),
        field_bindings=_bindings(),
        patch_text=patch,
        patch_name="vulnerable-instrumentation.patch",
    )

    assert report["valid"] is False
    assert any("$root.free_before_use" in error for error in report["errors"])
    assert not any("$root.false_literal" in error for error in report["errors"])


def test_runtime_field_quality_accepts_required_field_from_program_expression():
    patch = """diff --git a/src/parser.c b/src/parser.c
--- a/src/parser.c
+++ b/src/parser.c
@@ -1 +1,2 @@
 old
++fprintf(stderr, "ASSERT_EVT point=root free_before_use=%d false_literal=%d\\n", is_freed_before_use(state), 0);
"""

    report = validate_instrumentation_runtime_fields(
        spec=_spec(),
        field_bindings=_bindings(),
        patch_text=patch,
    )

    assert report["valid"] is True
