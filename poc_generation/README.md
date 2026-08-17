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

Both `run_sample.py` and `run_local_sample.py` accept
`--openhands-repo /path/to/OpenHands`. No runner depends on a machine-local
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
