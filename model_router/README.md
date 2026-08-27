# Model Router

`model_router` is the single source of truth for model/provider settings shared by `poc_generation`, `reward_framework`, `gt_generation`, and `harness_runtime`.

Prefer `model_route` in configs and CLI flags. Direct `model`, `base_url`, `api_key_env`, and `api_version` fields still work as manual overrides for compatibility. Store only environment variable names in configs; keep secret values in the environment or repo-root `config.txt`.

| route | model passed downstream | provider/key | intended use |
|---|---|---|---|
| `deepseek-v4-flash` | `deepseek/deepseek-chat` for OpenHands, `deepseek-v4-flash` for DeepSeek Harness | DeepSeek official, `DEEPSEEK_API_KEY` | OpenHands and DeepSeek Harness |
| `gpt-5.5` | `gpt-5.5-2026-04-24` | CN ModelHub OpenAI-compatible deployment, `OPENAI_API_KEY` | OpenHands and PoC/reward Codex; writes results under `gpt-5.5` |
| `gpt-5.4-mini` | `gpt-5.4-mini-2026-03-17` | Oversea ModelHub OpenAI-compatible deployment, `OPENAI_API_KEY_oversea` | OpenHands and PoC/reward Codex |
| `glm-5.2` | `glm-5.2` | CN ModelHub OpenAI-compatible deployment, `OPENAI_API_KEY` | OpenHands |
| `claude-opus-4.6` | `claude-sonnet-5` | LMUAI, `ANTHROPIC_AUTH_TOKEN` | Compatibility route: OpenHands writes `claude-opus-4.6`; Claude CLI writes `claudecli-claude-opus-4.6` |
| `claude-opus-4.8` | `claude-sonnet-5` | LMUAI, `ANTHROPIC_AUTH_TOKEN` | Compatibility route: OpenHands writes `claude-opus-4.8`; Claude CLI writes `claudecli-claude-opus-4.8` |
| `gt-codex-gpt-5.4` | `gpt-5.4-2026-03-05` | Oversea ModelHub OpenAI-compatible deployment via Codex Responses bridge, `OPENAI_API_KEY_oversea` | `gt_generation` Codex |
