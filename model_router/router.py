"""Centralized model routing for generation harnesses.

The launcher layers historically accepted raw ``model/base_url/api_key_env``
fields. That remains supported, but production runs should prefer a named
``model_route`` so the endpoint, key environment variable, protocol bridge and
stable result namespace are configured in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

ZHIZENGZENG_BASE_URL = "https://api.zhizengzeng.com/v1"
MODELHUB_OPENROUTER_MESSAGES_BASE_URL = (
    "https://aidp-i18ntt-sg.tiktok-row.net/api/modelhub/online/"
    "multimodal/crawl/openrouter/api/v1/messages"
)
MODELHUB_CN_OPENAI_BASE_URL = (
    "https://aidp.bytedance.net/api/modelhub/online/v2/crawl/openai/"
    "deployments/gpt_openapi"
)
MODELHUB_OVERSEA_OPENAI_CRAWL_BASE_URL = (
    "https://aidp-i18ntt-sg.tiktok-row.net/api/modelhub/online/v2/crawl"
)
MODELHUB_OPENAI_BASE_URL = (
    f"{MODELHUB_OVERSEA_OPENAI_CRAWL_BASE_URL}/openai/deployments/gpt_openapi"
)
MODELHUB_OPENAI_API_KEY_ENV = "OPENAI_API_KEY_oversea"
MODELHUB_OPENAI_API_VERSION = "2024-03-01-preview"
MODELHUB_OPENAI_CHAT_COMPLETIONS_URL = (
    f"{MODELHUB_OPENAI_BASE_URL}/chat/completions"
    f"?api-version={MODELHUB_OPENAI_API_VERSION}"
)
MODELHUB_MESSAGES_BASE_URL = MODELHUB_OPENROUTER_MESSAGES_BASE_URL
GLM_OPENAI_BASE_URL = MODELHUB_OPENAI_BASE_URL
LMUAI_BASE_URL = "https://api.lmuai.com"

# DeepSeek's official endpoint is kept as metadata for auditability. The
# OpenHands DeepSeek route intentionally does not force base_url, because the
# existing LiteLLM native provider path uses model ``deepseek/deepseek-chat`` and
# selecting OpenAI-compatible mode would change behavior.
DEEPSEEK_OFFICIAL_BASE_URL = "https://api.deepseek.com"


@dataclass(frozen=True)
class ModelRoute:
    """Resolved model/provider settings for one launcher/harness combination."""

    route_id: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    api_version: str = ""
    results_namespace: str = ""
    provider_kind: str = ""
    payload_format: str = ""
    codex_provider: dict[str, Any] | None = None
    extra_args: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    known: bool = True

    def public_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "route_id": self.route_id,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_version": self.api_version,
            "results_namespace": self.results_namespace,
            "provider_kind": self.provider_kind,
            "payload_format": self.payload_format,
            "extra_args": list(self.extra_args),
            "known": self.known,
        }
        if self.codex_provider:
            data["codex_provider"] = dict(self.codex_provider)
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return {key: value for key, value in data.items() if value not in (None, "", [], {})}


def _norm(value: str | None) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _normalize_harness(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _normalize_surface(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _modelhub_codex_provider(
    *,
    env_key: str,
    model_name: str,
    target_url: str = MODELHUB_OPENAI_CHAT_COMPLETIONS_URL,
    payload_format: str = "chat_completions",
    disable_proxy: bool = True,
    max_tokens: int = 16384,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    return {
        "id": "gt-modelhub-crawl",
        "name": "ModelHub crawl",
        "base_url": "http://127.0.0.1:0",
        "wire_api": "responses",
        "env_key": env_key,
        "bridge": {
            "enabled": True,
            "target_url": target_url,
            "payload_format": payload_format,
            "disable_proxy": disable_proxy,
            "max_tokens": max_tokens,
            "timeout_seconds": timeout_seconds,
        },
        "metadata": {"upstream_model": model_name},
    }


def _canonical_route(route_key: str, *, surface: str) -> str:
    key = _norm(route_key)
    aliases = {
        "deepseek": "deepseek-v4-flash",
        "deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek/deepseek-chat": "deepseek-v4-flash",
        "deepseek-chat": "deepseek-v4-flash",
        "gpt-5.5": "gpt-5.5",
        "gpt-5.5-2026-04-24": "gpt-5.5",
        "gpt5.5": "gpt-5.5",
        "gpt-5.4-mini": "gpt-5.4-mini",
        "gpt-5.4-mini-2026-03-17": "gpt-5.4-mini",
        "gpt54-mini": "gpt-5.4-mini",
        "glm-5.2": "glm-5.2",
        "glm5.2": "glm-5.2",
        "claude-opus-4.6": "claude-opus-4.6",
        "claude-opus-4-6": "claude-opus-4.6",
        "anthropic/claude-opus-4.6": "claude-opus-4.6",
        "claude-opus-4.6-lmuai": "claude-opus-4.6",
        "claude-opus-4-6-lmuai": "claude-opus-4.6",
        "claude-opus-4.8": "claude-opus-4.8",
        "claude-opus-4-8": "claude-opus-4.8",
        "anthropic/claude-opus-4.8": "claude-opus-4.8",
        "gt-codex-gpt-5.4": "gt-codex-gpt-5.4",
        "gt-gpt-5.4": "gt-codex-gpt-5.4",
        "gpt-5.4-2026-03-05": "gt-codex-gpt-5.4" if surface == "gt_generation" else "",
        "gpt-5.4": "gt-codex-gpt-5.4" if surface == "gt_generation" else "",
    }
    return aliases.get(key, "")


def _route_for(canonical: str, *, surface: str, harness: str) -> ModelRoute:
    if canonical == "deepseek-v4-flash":
        if harness == "deepseek_harness":
            return ModelRoute(
                route_id=canonical,
                model="deepseek-v4-flash",
                base_url=DEEPSEEK_OFFICIAL_BASE_URL,
                api_key_env="DEEPSEEK_API_KEY",
                results_namespace="deepseek-v4-flash",
                provider_kind="deepseek_official",
                metadata={"official_base_url": DEEPSEEK_OFFICIAL_BASE_URL},
            )
        return ModelRoute(
            route_id=canonical,
            model="deepseek/deepseek-chat",
            api_key_env="DEEPSEEK_API_KEY",
            results_namespace="deepseek-v4-flash",
            provider_kind="deepseek_litellm_native",
            metadata={"official_base_url": DEEPSEEK_OFFICIAL_BASE_URL},
        )

    if canonical == "gpt-5.5":
        model_name = "gpt-5.4-2026-03-05"
        extra_args: tuple[str, ...] = ()
        if harness == "codex":
            extra_args = (
                "--codex-bridge",
                "modelhub_crawl",
                "--bridge-payload-format",
                "chat_completions",
                "--bridge-disable-proxy",
            )
        elif harness in {"openhands", "sangfor_ai"}:
            extra_args = ("--provider-kind", "openai_compatible")
        return ModelRoute(
            route_id=canonical,
            model=model_name,
            base_url=MODELHUB_OPENAI_BASE_URL,
            api_key_env=MODELHUB_OPENAI_API_KEY_ENV,
            api_version=MODELHUB_OPENAI_API_VERSION,
            results_namespace="gpt-5.5",
            provider_kind="openai_compatible",
            payload_format="chat_completions",
            extra_args=extra_args,
            metadata={
                "requested_eval_model": "gpt-5.5-2026-04-24",
                "effective_model": model_name,
                "crawl_base_url": MODELHUB_OVERSEA_OPENAI_CRAWL_BASE_URL,
            },
        )

    if canonical == "gpt-5.4-mini":
        extra_args: tuple[str, ...] = ()
        if harness == "codex":
            extra_args = (
                "--codex-bridge",
                "modelhub_crawl",
                "--bridge-payload-format",
                "chat_completions",
                "--bridge-disable-proxy",
            )
        elif harness in {"openhands", "sangfor_ai"}:
            extra_args = ("--provider-kind", "openai_compatible")
        return ModelRoute(
            route_id=canonical,
            model="gpt-5.4-mini-2026-03-17",
            base_url=MODELHUB_OPENAI_BASE_URL,
            api_key_env=MODELHUB_OPENAI_API_KEY_ENV,
            api_version=MODELHUB_OPENAI_API_VERSION,
            results_namespace="gpt-5.4-mini",
            provider_kind="openai_compatible",
            payload_format="chat_completions",
            extra_args=extra_args,
            metadata={"crawl_base_url": MODELHUB_OVERSEA_OPENAI_CRAWL_BASE_URL},
        )

    if canonical == "glm-5.2":
        extra_args: tuple[str, ...] = ()
        if harness in {"openhands", "sangfor_ai"}:
            extra_args = ("--provider-kind", "openai_compatible")
        return ModelRoute(
            route_id=canonical,
            model="glm_5.2",
            base_url=GLM_OPENAI_BASE_URL,
            api_key_env=MODELHUB_OPENAI_API_KEY_ENV,
            api_version=MODELHUB_OPENAI_API_VERSION,
            results_namespace="glm-5.2",
            provider_kind="openai_compatible",
            payload_format="chat_completions",
            extra_args=extra_args,
            metadata={"crawl_base_url": MODELHUB_OVERSEA_OPENAI_CRAWL_BASE_URL},
        )

    if canonical == "claude-opus-4.6":
        extra_args = (
            ("--provider-kind", "anthropic")
            if harness in {"openhands", "sangfor_ai"}
            else ()
        )
        namespace = "claudecli-claude-opus-4.6" if harness == "claude" else "claude-opus-4.6"
        return ModelRoute(
            route_id=canonical,
            model="claude-sonnet-5",
            base_url=LMUAI_BASE_URL,
            api_key_env="ANTHROPIC_AUTH_TOKEN",
            results_namespace=namespace,
            provider_kind="anthropic_compatible",
            payload_format="anthropic_messages",
            extra_args=extra_args,
            metadata={
                "requested_eval_model": "claude-opus-4.6",
                "effective_model": "claude-sonnet-5",
            },
        )

    if canonical == "claude-opus-4.8":
        extra_args = (
            ("--provider-kind", "anthropic")
            if harness in {"openhands", "sangfor_ai"}
            else ()
        )
        namespace = "claudecli-claude-opus-4.8" if harness == "claude" else "claude-opus-4.8"
        return ModelRoute(
            route_id=canonical,
            model="claude-sonnet-5",
            base_url=LMUAI_BASE_URL,
            api_key_env="ANTHROPIC_AUTH_TOKEN",
            results_namespace=namespace,
            provider_kind="anthropic_compatible",
            payload_format="anthropic_messages",
            extra_args=extra_args,
            metadata={
                "requested_eval_model": "claude-opus-4.8",
                "effective_model": "claude-sonnet-5",
            },
        )

    if canonical == "gt-codex-gpt-5.4":
        model_name = "gpt-5.4-2026-03-05"
        return ModelRoute(
            route_id=canonical,
            model=model_name,
            base_url=MODELHUB_OPENAI_BASE_URL,
            api_key_env=MODELHUB_OPENAI_API_KEY_ENV,
            api_version=MODELHUB_OPENAI_API_VERSION,
            results_namespace="gt-gpt-5.4",
            provider_kind="openai_compatible",
            payload_format="chat_completions",
            codex_provider=_modelhub_codex_provider(
                env_key=MODELHUB_OPENAI_API_KEY_ENV,
                model_name=model_name,
            ),
            metadata={"crawl_base_url": MODELHUB_OVERSEA_OPENAI_CRAWL_BASE_URL},
        )

    raise ValueError(f"unknown model route: {canonical!r}")


def _manual_route(model: str, *, base_url: str = "", api_key_env: str = "", api_version: str = "") -> ModelRoute:
    return ModelRoute(
        route_id="manual",
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        api_version=api_version,
        results_namespace=model,
        known=False,
    )


def resolve_model_route(
    *,
    surface: str,
    harness: str,
    model_route: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    api_version: str | None = None,
    namespace: str | None = None,
) -> ModelRoute:
    """Resolve route settings and apply explicit caller overrides.

    ``model_route`` selects a named route. If it is absent and ``model`` is a
    known alias, the alias also selects a route for backwards compatibility.
    Explicit ``base_url``, ``api_key_env`` and ``api_version`` values override
    route defaults. Explicit ``namespace`` overrides the route namespace.
    """

    normalized_surface = _normalize_surface(surface)
    normalized_harness = _normalize_harness(harness)
    requested_route = str(model_route or "").strip()
    requested_model = str(model or "").strip()
    route_key = requested_route or requested_model
    canonical = _canonical_route(route_key, surface=normalized_surface) if route_key else ""

    if canonical:
        route = _route_for(canonical, surface=normalized_surface, harness=normalized_harness)
    elif requested_route:
        raise ValueError(f"unknown model_route {requested_route!r}")
    else:
        route = _manual_route(requested_model)

    if requested_route and requested_model:
        model_alias = _canonical_route(requested_model, surface=normalized_surface)
        if model_alias not in {"", route.route_id}:
            route = replace(route, model=requested_model)
    elif not route.known and requested_model:
        route = replace(route, model=requested_model)

    explicit_base_url = str(base_url or "").strip()
    explicit_api_key_env = str(api_key_env or "").strip()
    explicit_api_version = str(api_version or "").strip()
    explicit_namespace = str(namespace or "").strip()
    if explicit_base_url:
        route = replace(route, base_url=explicit_base_url)
    if explicit_api_key_env:
        route = replace(route, api_key_env=explicit_api_key_env)
    if explicit_api_version:
        route = replace(route, api_version=explicit_api_version)
    if explicit_namespace:
        route = replace(route, results_namespace=explicit_namespace)
    if not route.results_namespace:
        route = replace(route, results_namespace=route.model or route.route_id)
    return route


def available_model_routes() -> dict[str, str]:
    """Return the supported public route ids and their intended use."""

    return {
        "deepseek-v4-flash": "OpenHands and deepseek_harness DeepSeek route; official key env DEEPSEEK_API_KEY.",
        "gpt-5.5": "Oversea ModelHub OpenAI-compatible GPT route; currently sends model gpt-5.4-2026-03-05 while writing gpt-5.5 results; key env OPENAI_API_KEY_oversea.",
        "gpt-5.4-mini": "Oversea ModelHub OpenAI-compatible GPT route; key env OPENAI_API_KEY_oversea.",
        "glm-5.2": "OpenAI-compatible GLM route using the same oversea GPT base URL and OPENAI_API_KEY_oversea.",
        "claude-opus-4.6": "Compatibility route: run claude-sonnet-5 through LMUAI while writing opus-4.6 result namespaces.",
        "claude-opus-4.8": "Compatibility route: run claude-sonnet-5 through LMUAI while writing opus-4.8 result namespaces.",
        "gt-codex-gpt-5.4": "gt_generation Codex bridge route to oversea ModelHub OpenAI-compatible GPT; model gpt-5.4-2026-03-05.",
    }
