# Portable GT Generation

A CLI-agnostic solution for generating fine-grained memory-safety ground truth:
give it one vulnerability's information, get a validated `ground_truth.json`.
Any coding-agent CLI (Codex, Claude Code, or your own shim) can drive it.

All of this lives in `gt_generation/`. Data and the reproduction engine stay at
the repo root (`gt_results/`, `dataset/`, `evaluator/`,
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

Stages 01–04 are fresh external coding-agent CLI sessions. They share only files in the
sample result directory. When Stage 03 rejects completeness, the runner launches a new
Stage 02 session with `trace_feedback.json`, then a new Stage 03 session, for at most the
configured feedback rounds. `05_validate` is deterministic. Evaluation later uses the
separate `evaluation-prober` under `evaluator/reasoning/skill/`.

The Claude adapter uses Sonnet for Stage 01 reproduction and pins the reasoning-heavy
Stage 02 trace, Stage 03 review, and Stage 04 assertion validation sessions to
`claude-opus-4-6` (override with `GT_CLAUDE_COMPLEX_MODEL` only when needed).

For ARVO, Stage 01 creates one sample workspace and performs the only default full
vulnerable build. Stage 04 reuses it for instrumented target-level rebuilds, applies the
official patch in place, and incrementally rebuilds the fixed target. The fixed image is
pulled only as an explicit fallback; cleanup removes only that sample's workspace/images.

Every result directory permanently retains the four reproducibility assets
`sample_info.json`, `build.sh`, `poc`, and `patch.diff`. Runtime worktrees, containers,
instrumentation patches, and role logs may be cleaned after validation; these four files
must not be cleaned.

After Stage 05 succeeds, the runner compacts the result to those four assets plus the
default/reproduced crash traces, `ground_truth.json`, verified invariants/assertions,
assertion/perturbation/reachability results, and generation timing. Candidate specs,
instrumentation patches, raw assertion traces, reviewer state, role logs, and debugger
scratch artifacts are retained only for failed runs.

Stage 00 also snapshots `default_crash_trace.txt`, the exact crash context initially
visible to the evaluated agent. Stage 04 emits `assertion-spec-v3`: node states use
`observed`, the missing safety obligation uses `required`, and every verified invariant
edge has one cross-event `transition` assertion. Stage 05 runs `audit-package`, which
rejects legacy edge bindings, missing public context, unverified perturbations, and
dangling evidence paths before a result can be used to generate probes.

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
Docker environment moved to `docker/gt-memory-env/`. Historical batch,
migration, grounding, and audit scripts were removed; regeneration now goes
through this one pipeline.
