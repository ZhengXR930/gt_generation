# Portable GT Generation

A CLI-agnostic solution for generating fine-grained memory-safety ground truth:
give it one vulnerability's information, get a validated `ground_truth.json`.
Any coding-agent CLI (Codex, Claude Code, or your own shim) can drive it.

All of this lives in `gt_generation/`. Data and the reproduction engine stay at
the repo root (`gt_results/`, `dataset/`, `evaluator/`,
`docker/`); `runner.py` knows the split (code_root = `gt_generation/`,
repo_root = its parent).

## Running a batch (config-driven — start here)

`gt_plugin.py` generates GT for a whole list of samples from one JSON config.
This is the entry point a collaborator should use; it needs only an API key and
a config file.

**1. Put an API key in the repo-root `config.txt`** (one `KEY=value` per line;
`config.txt` is gitignored, so each person keeps their own):

```
DEEPSEEK_API_KEY=<key>
OPENAI_API_KEY=<key>
OPENAI_API_KEY_oversea=<key>
ANTHROPIC_AUTH_TOKEN=<key>
```

Model/provider settings are centralized in `model_router/`. Prefer `model_route` in the JSON config; raw `model` and `codex_provider` remain available for explicit overrides. Provider secrets must stay in the environment or repo-root `config.txt`; configs contain only environment variable names.

**2. Copy the example config and edit it:**

```bash
cp gt_generation/gt_config.example.json gt_generation/gt_config.json
```

| field | meaning |
|-------|---------|
| `cli` | which agent CLI drives generation: `claude` \| `codex` \| `coco` (Trae). Each maps to `adapters/<cli>/`. |
| `model_route` | preferred named route from `model_router/`; for current Codex GT runs use `gt-codex-gpt-5.4`. |
| `model` | optional concrete model override. Leave empty when `model_route` is set. |
| `reasoning_effort` | Codex reasoning effort used for every agent stage: `minimal`, `low`, `medium`, `high`, or `xhigh` (default `high`). |
| `strict_config` | Pass `--strict-config` to Codex so unknown config keys fail immediately (default `true`). |
| `codex_provider` | optional explicit Codex custom provider. Usually leave null and let `model_route` fill it. Current Codex CLI accepts `wire_api: "responses"`; OpenAI-compatible ModelHub routes use the local Responses bridge when needed. |
| `parallel_dockers` | how many samples to run at once (1–6); each holds one Docker workspace. |
| `repo_docker_image` | Image tag used for non-ARVO samples (default `gt-memory-env:latest`). |
| `repo_docker_context` | Build context used for non-ARVO samples (default `docker/gt-memory-env`). |
| `selection` | dataset metadata list (default `dataset/selected_1000.json`). |
| `samples` | sample ids to generate, e.g. `["arvo_1304", "arvo_12595"]`. |

**3. Run:**

```bash
python3 gt_generation/gt_plugin.py --config gt_generation/gt_config.json
```

Before running it refreshes `GT_STATUS.md` and **skips any sample already
complete**, so finished work is never redone. Docker routing is automatic and
printed up front: ARVO samples use the prebuilt `n132/arvo:<id>` images; every
other source builds/clones in the shared `gt-memory-env` image.

### Example — codex with the ModelHub GT route

```jsonc
// config.txt:  OPENAI_API_KEY_oversea=<key>
// gt_generation/gt_config.json:
{
  "cli": "codex",
  "model_route": "gt-codex-gpt-5.4",
  "model": "",
  "codex_provider": null,
  "reasoning_effort": "medium",
  "strict_config": true,
  "parallel_dockers": 2,
  "repo_docker_image": "gt-memory-env:latest",
  "repo_docker_context": "docker/gt-memory-env",
  "selection": "dataset/selected_1000.json",
  "samples": ["arvo_1304", "arvo_12595"]
}
```
```bash
python3 gt_generation/gt_plugin.py --config gt_generation/gt_config.json
```

The `gt-codex-gpt-5.4` route expands to model `gpt-5.4-2026-03-05`, key env `OPENAI_API_KEY_oversea`, and a local Codex Responses bridge that forwards Chat Completions payloads to the oversea ModelHub OpenAI-compatible deployment.

### Coverage status (`GT_STATUS.md`)

`GT_STATUS.md` at the repo root (auto-generated, overwritten each run) lists
which samples are **complete** (recorded — don't re-run), **incomplete** (a
partial dir that should be re-run), and **remaining** (not started). Check it to
see what is already done and what to run next; regenerate any time:

```bash
python3 gt_generation/gt_status.py
```

**Prerequisites:** Docker running, and the chosen CLI installed and on `PATH`
(`claude`, `codex`, or `coco`).

## The three layers

```
gt_generation/
  L3  adapters/                per-CLI projection (thin)
        claude_code/build_skills.py   roles/*.md -> installable Claude Code skills
        codex/README.md               roles/*.md fed as prompts via GT_AGENT_COMMAND
      runner.py                  deterministic stage orchestration (any CLI)
      workflow.json              stage list, timeouts, retries, gates

  L2  gt_toolkit/              portable core — the single source of truth
        schema/ground_truth.schema.json   canonical GT schema (one definition)
        validate.py / state.py / reachability.py / instrument.py
        -> `python3 -m gt_toolkit <cmd>` (zero install with gt_generation on PYTHONPATH)
      roles/01_*.md ... 04_*.md  isolated GT-stage session contracts

<repo root>
  L1  docker/gt-memory-env/    the reproducible build/repro/debug environment
      evaluator/reachability/  engine used by gt_toolkit reachability
```

Principle: **content lives once in L2** (roles + schema + tools). L3 projects it
per-CLI; it never forks the content. Portability comes from L2 + thin adapters,
not from any one CLI's skill mechanism.

## Pipeline

`00_prepare -> 01_reproducer -> 02_fine_trace -> 03_trace_review -> 04_assertion_validator -> 05_validate`

`01_reproducer` is a hard screening gate. A config with
`"stop_after": "01_reproducer"` is a first-class pre-screening batch: success is
judged from `reproduction_report.json` plus the fixed-oracle gate when a fixed commit
exists, and does not require `audit-package`. Only samples accepted by that gate should
be queued for Stage 02 and later.

Stages 01–04 are fresh external coding-agent CLI sessions. They share only files in the
sample result directory. When Stage 03 rejects completeness, the runner launches a new
Stage 02 session with `trace_feedback.json`, then a new Stage 03 session, for at most the
configured feedback rounds. `05_validate` is deterministic. Evaluation later uses the
separate `evaluation-prober` under `evaluator/reasoning/skill/`.

Every stage runs with the single model from the config (`GT_AGENT_MODEL`); there is
no per-stage model switching. Adapters read the model from `GT_AGENT_MODEL`
(the Claude adapter falls back to the older `GT_CLAUDE_MODEL`, then `sonnet`).
Codex also receives the configured reasoning effort and strict-config check. Each
sample records the effective CLI, model, custom Codex provider when used, adapter
hash, authentication method, and non-ARVO Docker settings in
`generation_provenance.json`.

For ARVO, Stage 01 creates one sample workspace and performs the default vulnerable
build and reproduction. Stage 04 is split at its failure boundaries: assertion planning
first freezes invariants from the accepted fine trace, sanitizer trace, and vulnerable
source; separate vulnerable and fixed instrumentation stages then generate and compile
one observation patch each against the corresponding published image; execution finally
runs the frozen observations. A failed fixed-side patch retries only fixed
instrumentation and cannot rewrite the invariant plan or vulnerable patch. The official
`patch.diff` is not an invariant input or an execution oracle. Cleanup removes only that
sample's workspace/images.

Every result directory permanently retains the three reproducibility assets
`sample_info.json`, `build.sh`, and `poc`. Runtime worktrees, containers,
instrumentation patches, and role logs may be cleaned after validation; these three files
must not be cleaned. `patch.diff` (the official fix commit) is **not** kept: for ARVO it
is frequently an unrelated build/docs/version commit, so it is not a reliable fix oracle
— the sanitizer trace is authoritative for the root cause and the prebuilt `-fix` image
for "the crash disappears". It may still be produced transiently during generation but is
stripped from the stored package.

After Stage 05 succeeds, the runner compacts the result to those three assets plus the
default/reproduced crash traces, `ground_truth.json`, verified invariants/assertions,
field/event bindings, assertion/perturbation/reachability results, and generation timing.
Candidate specs, instrumentation patches, raw assertion traces, reviewer state, role logs,
`patch.diff`, and debugger scratch artifacts are retained only for failed runs.

Stage 00 also snapshots `default_crash_trace.txt`, the exact crash context initially
visible to the evaluated agent. Stage 04 emits `assertion-spec-v3`: node states use
`observed`, the missing safety obligation uses `required`, and every verified invariant
edge has one cross-event `transition` assertion. Stage 05 runs `audit-package`, which
rejects legacy edge bindings, missing public context, unverified perturbations, and
dangling evidence paths before a result can be used to generate probes.

## Low-level / single-sample use

The config-driven launcher above is the normal path. For one sample, the
deterministic tools, or a custom CLI shim, drive `runner.py` directly. Run from
the repo root; put `gt_generation/` on `PYTHONPATH` (or `pip install -e
gt_generation` once, which gives you a global `gt-toolkit` command).

```bash
# 1. deterministic tools, zero install:
export PYTHONPATH=gt_generation
python3 -m gt_toolkit validate gt_results/<sample>/ground_truth.json
python3 -m gt_toolkit schema-path

# 2. pick any agent CLI and run the pipeline (runner sets PYTHONPATH for stages):
export GT_AGENT_COMMAND='<your agent CLI> --prompt-file'
python3 gt_generation/runner.py --sample gt_generation/sample.example.json --resume

# 3. (optional) project roles into installable Claude Code skills:
python3 gt_generation/adapters/claude_code/build_skills.py --out ~/.claude/skills
```

## Validation tiers

`gt-toolkit validate` separates:

- **errors** — structural violations of the canonical schema (always fail);
- **warnings** — quality expectations (`value_from`, `description`,
  `reachability_checkpoints`, bootstrap wording, disposable poc.path). Promote
  them to errors with `--strict`.

Optional: `--source-root <checkout>` checks that scored lines exist;
`--jsonschema` runs an advisory jsonschema cross-check (non-fatal).

## Migration note

The historical `gt_results/` corpus contains two GT generations. Under the new
canonical schema the older **arvo** samples mostly fail (missing `poc.format`,
legacy `watchpoint_trace`, older `bug_description` shape), while newer
**secbench** samples are closer. This is real drift the single schema now makes
visible; those samples need migration or regeneration, not a looser schema.

The previous `gt_generator/` directory and the old `skills/00-08` pipeline have
been removed; their runner, roles, schemas, and contract now live in this layout
(`gt_toolkit/` + `roles/` + `runner.py` + `workflow.json` + `adapters/`). The L1
Docker environment moved to `docker/gt-memory-env/`. Historical batch,
migration, grounding, and audit scripts were removed; regeneration now goes
through this one pipeline.
