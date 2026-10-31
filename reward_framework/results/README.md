# Reward-framework result registries

Each supported harness/model family has a small registry under this directory.
The registry points to canonical raw run directories; it does not duplicate
trajectories or submissions. Archived retries remain available for provenance
but are not valid comparison arms unless explicitly selected.

- `dsh_deepseek/RESULTS_INDEX.json` — DSH + DeepSeek initial/evolving TRAIN and
  TEST arms;
- `zcode_glm52/RESULTS_INDEX.json` — canonical ZCode + GLM-5.2 initial TRAIN
  batches currently available.

The harness and model are selected by configuration. The DSH registry's prompt
hash is the one used for the DSH + DeepSeek task prompt; historical ZCode
manifests predate prompt-hash instrumentation and therefore retain a null hash
rather than inventing provenance.
