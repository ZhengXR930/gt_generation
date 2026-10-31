# DSH + DeepSeek failure-balanced 100/150 controls

`sources.json` pins the controls used by Correction's rolling comparison:

- initial skill on the 100-sample TRAIN split at 4096 output tokens;
- the same initial skill snapshot on the disjoint 150-sample TEST split at
  32768 output tokens;
- the no-skill formal DSH + DeepSeek result set from `poc_generation`, covering
  the 500-sample valid corpus.

The TEST control uses the immutable harness snapshot named in `sources.json`.
The complete TRAIN control's historical manifest records the mutable
`reward_framework/skill_packets/initial` path and has an empty effective-config
file; its original packet contents cannot be reconstructed byte-for-byte from
the run alone. This provenance limitation is recorded in `sources.json` and
must not be silently erased when replaying the baseline.

Correction pairs each evolving TRAIN batch with the initial-skill and no-skill
rows for the same sample IDs. Missing controls make a batch non-comparable;
they never support a trend claim.

The canonical `split_manifest.json` is maintained in place from the latest
unique `poc_generation` reachability results. The active roster is 100 TRAIN
and 150 TEST samples, with batches 000-014 (ten samples each); the original
100-sample test control is historical. Batch000 and batch001 remain fixed; the
remaining batches use three minimal swaps so the Codex+GPT-5.5 no-skill
GT-trigger counts are `9, 8, 7, 7, 7, 7, 7, 7, 6, 6`. The manifest records
the selected Codex outcome labels, the duplicate-resolution rule for
`arvo_13467`, and the actual DSH baseline success/failure count of every batch;
it is the only active split for future runs.

The retained post-curator history used a different 300/170 split. Its new
rolling artifact is therefore explicitly non-comparable to this 100/150
control, rather than being silently mixed into the trend.

The historical packet paths recorded in `sources.json` now resolve under
`skill_packets/_archive/historical_packets/`; the active current packet paths
are maintained separately in `reward_framework/results/dsh_deepseek/`.
