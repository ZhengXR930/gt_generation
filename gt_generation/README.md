# Portable GT Generation

A CLI-agnostic solution for generating fine-grained memory-safety ground truth:
give it one vulnerability's information, get a validated `ground_truth.json`.
Any coding-agent CLI (Codex, Claude Code, or your own shim) can drive it.

All of this lives in `gt_generation/`. Data and the reproduction engine stay at
the repo root (`gt_results/`, `final_dataset/`, `evaluation_mode/`, `shared/`,
`docker/`); `runner.py` knows the split (code_root = `gt_generation/`,
repo_root = its parent).

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
      roles/*.md                 role prompts (one definition, projected by L3)

<repo root>
  L1  docker/gt-memory-env/    the reproducible build/repro/debug environment
      evaluation_mode/, shared/  reachability engine (used by gt_toolkit reachability)
```

Principle: **content lives once in L2** (roles + schema + tools). L3 projects it
per-CLI; it never forks the content. Portability comes from L2 + thin adapters,
not from any one CLI's skill mechanism.

## Pipeline stages

`00_materialize -> 01_reproducer -> 02_gt_generator -> 03_source_auditor ->
04_semantic_reviewer -> 05_runtime_validator -> 06_validate`

- `06_validate` is a deterministic gate: `gt-toolkit validate` against the
  canonical schema. No agent judgment.

## Quick start

Run from the repo root. Put `gt_generation/` on `PYTHONPATH` (or `pip install -e
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
Docker environment moved to `docker/gt-memory-env/`, and the still-used arvo
grounding helper to `scripts/compute_grounding.py`.
