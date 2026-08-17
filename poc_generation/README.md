# PoC generation environment

The subject-agent runner uses upstream OpenHands 0.33.0. The checkout is a
reproducible external dependency and is intentionally not stored in Git.

After cloning this repository, prepare it with:

```bash
./scripts/setup_openhands.sh
```

This clones the pinned OpenHands revision into `external/OpenHands` and runs
its Poetry installation. To use an already prepared environment, clone only
the source and select its interpreter explicitly:

```bash
./scripts/setup_openhands.sh --checkout-only
export OPENHANDS_PYTHON=/path/to/openhands-venv/bin/python
```

Both `poc_generator/run_sample.py` and `poc_generator/run_local_sample.py`
remain supported compatibility entrypoints and accept
`--openhands-repo /path/to/OpenHands`. Their implementations live under
`poc_generator/openhands_backend/`. No runner depends on a machine-local
`/tmp/openhands-poc-smoke` directory.

Do not edit `external/OpenHands` directly. It is the pinned pristine baseline
used by PoC-generation evaluation. For experiments that require OpenHands source
changes, first create a separate editable copy:

```bash
./scripts/create_openhands_copy.sh my-experiment
```

Then pass that copy to an independent experiment or reward-framework launcher
with `--openhands-repo external/OpenHands-experiments/my-experiment`.
Baseline and remote-equivalent PoC evaluation should continue to use the
pristine checkout.

## DeepSeek Harness backend

The DeepSeek Harness evaluation glue lives under `poc_generator/dsh/`:

- `run_deepseek_harness_arvo_sample.py` for ARVO/CyberGym samples
- `run_deepseek_harness_local_sample.py` for non-ARVO local samples
- `run_dsh_arvo_batch.py` and `run_dsh_local_batch.py` for direct backend
  batches
- `finalize_dsh_analysis_*.py` and `recover_dsh_analysis_from_checkpoint.py`
  for DSH-specific artifact recovery

The top-level scripts with the same names are compatibility shims. Configured
batch runs should use the shared launcher plus the stable DSH config:

The DeepSeek Harness source checkout itself is a third-party dependency and is
not tracked here. Install or clone it at `external/deepseek-harness`, build its
CLI, and keep the local Node runtime path in the config's `dsh_node_root`.

```bash
python poc_generation/poc_generator/run_config_batch.py \
  poc_config.deepseek_harness_strict_gt_all.json
```
