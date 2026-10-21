# DSH + DeepSeek failure-balanced 100/100 controls

`sources.json` pins the controls used by Correction's rolling comparison:

- initial skill on the 100-sample TRAIN split at 4096 output tokens;
- the same initial skill snapshot on the disjoint 100-sample TEST split at
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

The retained post-curator history used a different 300/170 split. Its new
rolling artifact is therefore explicitly non-comparable to this 100/100
control, rather than being silently mixed into the trend.
