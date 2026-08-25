import ast
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = (
    ROOT
    / "harness_runtime"
    / "openhands"
    / "runtime.py"
)
SAMPLE_RUNNER = (
    ROOT
    / "harness_runtime"
    / "openhands"
    / "arvo.py"
)


def _load_model_map():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "model_map"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {}
    exec(compile(module, str(RUNNER), "exec"), namespace)
    return namespace["model_map"]


def _load_native_tool_calling_for_model():
    tree = ast.parse(SAMPLE_RUNNER.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "native_tool_calling_for_model"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {"os": os}
    exec(compile(module, str(SAMPLE_RUNNER), "exec"), namespace)
    return namespace["native_tool_calling_for_model"]


def test_openai_compatible_proxy_forces_openai_wire_adapter():
    model_map = _load_model_map()

    assert model_map("gpt-5.4", openai_compatible=True) == "openai/gpt-5.4"
    assert (
        model_map("claude-opus-4-7", openai_compatible=True)
        == "openai/claude-opus-4-7"
    )


def test_native_provider_mapping_remains_unchanged_without_proxy():
    model_map = _load_model_map()

    assert model_map("claude-opus-4-7") == "claude-opus-4-7"
    assert model_map("claude-sonnet-5") == "claude-sonnet-5"
    assert model_map("deepseek/deepseek-chat") == "deepseek/deepseek-chat"


def test_gpt54_uses_native_tools_with_old_openhands_capability_tables():
    native_tool_calling_for_model = _load_native_tool_calling_for_model()

    assert native_tool_calling_for_model("gpt-5.4-mini") is True
    assert native_tool_calling_for_model("openai/gpt-5.4") is True
    assert native_tool_calling_for_model("deepseek/deepseek-chat") is None


def test_anthropic_messages_provider_mapping_overrides_base_url_openai_default():
    model_map = _load_model_map()

    assert (
        model_map(
            "gpt-5.4-mini-2026-03-17",
            openai_compatible=True,
            provider_kind="anthropic_messages",
        )
        == "anthropic/gpt-5.4-mini-2026-03-17"
    )
    assert (
        model_map(
            "anthropic/claude-opus-4.8",
            openai_compatible=True,
            provider_kind="anthropic_messages",
        )
        == "anthropic/claude-opus-4.8"
    )
