# PoC Generation

`poc_generation` is the baseline evaluation frontend for harness plus model PoC
generation. It owns the baseline prompt, the result namespace under
`poc_results/`, and the four harness adapters:

- `poc_generation.adapters.openhands`
- `poc_generation.adapters.codex`
- `poc_generation.adapters.claude`
- `poc_generation.adapters.deepseek_harness`

All shared execution code lives in `harness_runtime/`. The baseline frontend
does not import `reward_framework` and does not install reward skill packets.

Run samples through the unified entrypoint:

```bash
python -m poc_generation.run_harness \
  --harness openhands \
  --model deepseek/deepseek-chat \
  --namespace deepseek-v4-flash \
  --sample-selector valid_gt_arvo \
  --parallel 3
```

Use `--sample` or `--samples-file` for explicit sample lists. Use
`--sample-selector valid_gt`, `valid_gt_arvo`, or `valid_gt_non_arvo` to read
the authoritative denominator from `gt_results/valid_gt.json`. Each sample writes
to `poc_results/<namespace>/<sample_id>/` with `manifest.json`,
`analysis.json`, checkpoint data, and `submissions/` when the harness submitted
candidate inputs.

## Harness Runtime

`harness_runtime/` is the neutral runtime used by both baseline and reward
frontends. It owns only shared mechanics: task workspace creation, OpenHands
launching, Codex/Claude CLI launching, DeepSeek Harness launching,
submission-ledger persistence, deduplication, and reachability evaluation.

The local CyberGym submission server is shared by all ARVO harnesses. Start it
from the neutral runtime wrapper:

```bash
./harness_runtime/start_server.sh
```

The physical server state is stored under `harness_runtime/server/`; database
and log files there are local runtime artifacts and are ignored by Git.

## OpenHands Checkout

The subject-agent runner uses upstream OpenHands 0.33.0. The checkout is a
reproducible external dependency and is intentionally not stored in Git.

After cloning this repository, prepare it with:

```bash
./scripts/setup_openhands.sh
```

To use an already prepared environment, clone only the source and select its
interpreter explicitly:

```bash
./scripts/setup_openhands.sh --checkout-only
export OPENHANDS_PYTHON=/path/to/openhands-venv/bin/python
```

Do not edit `external/OpenHands` directly. It is the pinned pristine baseline
used by PoC-generation evaluation. For experiments that require OpenHands source
changes, first create a separate editable copy and pass it with
`--openhands-repo`.
