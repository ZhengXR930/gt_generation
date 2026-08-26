import ast
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model_router import (
    GLM_OPENAI_API_KEY_ENV,
    GLM_OPENAI_BASE_URL,
    MODELHUB_MESSAGES_BASE_URL,
    MODELHUB_OPENAI_API_VERSION,
    MODELHUB_OPENAI_API_KEY_ENV,
    MODELHUB_OPENAI_BASE_URL,
    MODELHUB_OPENAI_CHAT_COMPLETIONS_URL,
    MODELHUB_OVERSEA_OPENAI_CRAWL_BASE_URL,
    resolve_model_route,
)
from poc_generation import run_harness as poc_run_harness
from reward_framework import run_harness as reward_run_harness
from gt_generation import gt_plugin


def _poc_args(**overrides):
    values = dict(
        harness=None,
        model=None,
        model_route=None,
        namespace=None,
        base_url=None,
        api_key_env=None,
        api_version=None,
        max_iter=None,
        max_attempts=3,
        timeout=None,
        server=None,
        difficulty=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _reward_args(**overrides):
    values = dict(
        harness=None,
        model=None,
        model_route=None,
        base_url=None,
        api_key_env=None,
        api_version=None,
        skill_packet=None,
        max_iter=None,
        max_attempts=3,
        max_effective_submits=None,
        reasoning_effort=None,
        max_output_tokens=None,
        timeout=None,
        server=None,
        difficulty=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _load_cli_bridge_helpers():
    path = ROOT / "harness_runtime" / "cli.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_should_start_codex_bridge", "_bridge_target_url"}
    ]
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {"argparse": argparse}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_should_start_codex_bridge"], namespace["_bridge_target_url"]


def test_poc_codex_gpt54_mini_route_uses_modelhub_bridge_args():
    route = resolve_model_route(
        surface="poc_generation",
        harness="codex",
        model_route="gpt-5.4-mini",
    )

    assert route.model == "gpt-5.4-mini-2026-03-17"
    assert route.base_url == MODELHUB_OPENAI_BASE_URL
    assert route.base_url.startswith(MODELHUB_OVERSEA_OPENAI_CRAWL_BASE_URL)
    assert route.api_key_env == MODELHUB_OPENAI_API_KEY_ENV
    assert route.api_version == MODELHUB_OPENAI_API_VERSION
    assert route.provider_kind == "openai_compatible"
    assert route.payload_format == "chat_completions"
    assert route.extra_args == (
        "--codex-bridge",
        "modelhub_crawl",
        "--bridge-payload-format",
        "chat_completions",
        "--bridge-disable-proxy",
    )


def test_poc_route_can_be_selected_from_launcher_args():
    request = poc_run_harness.build_request(
        _poc_args(harness="codex", model_route="gpt-5.5"),
        {"samples": ["arvo_1"]},
        "arvo_1",
    )

    assert request.model == "gpt-5.4-2026-03-05"
    assert request.base_url == MODELHUB_OPENAI_BASE_URL
    assert request.api_key_env == MODELHUB_OPENAI_API_KEY_ENV
    assert request.api_version == MODELHUB_OPENAI_API_VERSION
    assert request.namespace == "gpt-5.5"
    assert "--bridge-disable-proxy" in request.extra_args


def test_legacy_poc_fields_override_known_route_defaults():
    request = poc_run_harness.build_request(
        _poc_args(),
        {
            "harness": "codex",
            "model": "gpt-5.4-mini-2026-03-17",
            "base_url": "https://example.test/chat/completions",
            "api_key_env": "OPENAI_API_KEY",
            "api_version": "2024-03-01-preview",
            "results_namespace": "legacy",
        },
        "arvo_1",
    )

    assert request.model == "gpt-5.4-mini-2026-03-17"
    assert request.base_url == "https://example.test/chat/completions"
    assert request.api_key_env == "OPENAI_API_KEY"
    assert request.api_version == "2024-03-01-preview"
    assert request.namespace == "legacy"


def test_claude_opus46_compat_route_uses_sonnet5_but_keeps_old_namespaces():
    openhands_route = resolve_model_route(
        surface="poc_generation",
        harness="openhands",
        model_route="claude-opus-4.6",
    )
    claude_route = resolve_model_route(
        surface="poc_generation",
        harness="claude",
        model_route="claude-opus-4.6",
    )

    assert openhands_route.model == "claude-sonnet-5"
    assert openhands_route.base_url == "https://api.lmuai.com"
    assert openhands_route.api_key_env == "ANTHROPIC_AUTH_TOKEN"
    assert openhands_route.results_namespace == "claude-opus-4.6"
    assert openhands_route.extra_args == ("--provider-kind", "anthropic")
    assert claude_route.model == "claude-sonnet-5"
    assert claude_route.base_url == "https://api.lmuai.com"
    assert claude_route.api_key_env == "ANTHROPIC_AUTH_TOKEN"
    assert claude_route.results_namespace == "claudecli-claude-opus-4.6"


def test_claude_opus48_compat_route_uses_sonnet5_but_keeps_old_namespaces():
    openhands_route = resolve_model_route(
        surface="poc_generation",
        harness="openhands",
        model_route="claude-opus-4.8",
    )
    claude_route = resolve_model_route(
        surface="poc_generation",
        harness="claude",
        model_route="claude-opus-4.8",
    )

    assert openhands_route.route_id == "claude-opus-4.8"
    assert openhands_route.model == "claude-sonnet-5"
    assert openhands_route.base_url == "https://api.lmuai.com"
    assert openhands_route.api_key_env == "ANTHROPIC_AUTH_TOKEN"
    assert openhands_route.results_namespace == "claude-opus-4.8"
    assert openhands_route.extra_args == ("--provider-kind", "anthropic")
    assert claude_route.model == "claude-sonnet-5"
    assert claude_route.results_namespace == "claudecli-claude-opus-4.8"


def test_poc_sangfor_glm52_route_uses_openai_compatible_provider():
    request = poc_run_harness.build_request(
        _poc_args(
            harness="sangfor_ai",
            model_route="glm-5.2",
            namespace="sangfor-glm-5.2",
        ),
        {"samples": ["arvo_1"]},
        "arvo_1",
    )

    assert request.model == "glm-5.2"
    assert request.api_key_env == GLM_OPENAI_API_KEY_ENV
    assert request.api_version == MODELHUB_OPENAI_API_VERSION
    assert request.base_url == GLM_OPENAI_BASE_URL
    assert request.namespace == "sangfor-glm-5.2"
    assert request.extra_args == ("--provider-kind", "openai_compatible")


def test_reward_framework_uses_same_model_route_table(tmp_path):
    request = reward_run_harness.build_request(
        _reward_args(harness="openhands", model_route="glm-5.2"),
        {},
        run_id="route-test",
        run_dir=tmp_path,
        sample_id="arvo_1",
    )

    assert request.model == "glm-5.2"
    assert request.api_key_env == GLM_OPENAI_API_KEY_ENV
    assert request.api_version == MODELHUB_OPENAI_API_VERSION
    assert request.base_url == GLM_OPENAI_BASE_URL
    assert request.extra_args == ("--provider-kind", "openai_compatible")


def test_gt_generation_route_expands_codex_provider(tmp_path):
    config = {
        "cli": "codex",
        "model_route": "gt-codex-gpt-5.4",
        "model": "",
        "reasoning_effort": "medium",
        "strict_config": True,
        "codex_provider": None,
        "repo_docker_context": str(ROOT / "docker" / "gt-memory-env"),
        "samples": ["arvo_1"],
    }
    config_path = tmp_path / "gt_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    loaded = gt_plugin.load_config(config_path)

    assert loaded["model"] == "gpt-5.4-2026-03-05"
    assert loaded["model_route"] == "gt-codex-gpt-5.4"
    provider = loaded["codex_provider"]
    assert provider["env_key"] == MODELHUB_OPENAI_API_KEY_ENV
    assert provider["bridge"]["target_url"] == MODELHUB_OPENAI_CHAT_COMPLETIONS_URL
    assert provider["bridge"]["payload_format"] == "chat_completions"
    assert provider["bridge"]["disable_proxy"] is True
    assert "caller" not in provider["bridge"] or provider["bridge"]["caller"] == ""


def test_codex_bridge_auto_only_for_modelhub_like_urls():
    should_start, bridge_target_url = _load_cli_bridge_helpers()

    assert should_start(SimpleNamespace(base_url=MODELHUB_MESSAGES_BASE_URL, codex_bridge="auto"))
    assert should_start(SimpleNamespace(base_url=MODELHUB_OPENAI_BASE_URL, codex_bridge="auto"))
    assert not should_start(SimpleNamespace(base_url="https://api.zhizengzeng.com/v1", codex_bridge="auto"))
    assert should_start(SimpleNamespace(base_url="https://api.zhizengzeng.com/v1", codex_bridge="modelhub_crawl"))
    assert not should_start(SimpleNamespace(base_url=MODELHUB_MESSAGES_BASE_URL, codex_bridge="none"))
    assert bridge_target_url(MODELHUB_OPENAI_BASE_URL, MODELHUB_OPENAI_API_VERSION) == (
        MODELHUB_OPENAI_BASE_URL
        + "/chat/completions?api-version="
        + MODELHUB_OPENAI_API_VERSION
    )
