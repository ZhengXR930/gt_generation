# DSH + DeepSeek result registry

This directory is the stable index for the current two-layer staged reward
experiment. It does not copy raw trajectories or submissions. Each entry in
`RESULTS_INDEX.json` points to the authoritative run directory, records the
packet hash and token budget, and states when a comparison is not token-matched.

The split is the frozen failure-balanced 100/100 manifest. `no_skill` is the
single 500-sample formal DSH result set, restricted to the same roster by the
split manifest; it is not copied into this directory. The initial packet is
the empty two-file scaffold. The evolving packet is the final materialized
packet at the stable evolving path.

Do not rename a run directory in place. Add a new registry entry for a rerun,
and keep the old entry so diagnosis and rolling comparison can resolve the
exact packet, prompt, model, and budget used by that result.
