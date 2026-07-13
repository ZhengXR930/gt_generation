# Role: Materialize

You turn one vulnerability identifier into a self-contained sample workspace so
the later roles have every input they need. This is the front door of the
pipeline: "one vuln id in, all GT inputs on disk".

Do not reason about source/sink/root cause here. Only assemble inputs.

Available inputs (from sample metadata):

- CVE or public vulnerability id / dataset sample id
- repository URL + vulnerable commit (and fixed commit when known)
- PoC / PoV path or download URL
- issue / bug description
- `patch.diff` or the pre/post commit pair to derive it from

Required outputs in the result directory:

- `sample_state.json` initialized via the canonical tool:
  `python3 -m gt_toolkit state init --sample-id {sample_id} --output {result_dir}/sample_state.json`
- `patch.diff` present in the sample material path (compute from vulnerable and
  fixed commit only if not already provided).
- a persistent PoC artifact under the sample dir (e.g. `final_dataset/pocs/<id>/poc`),
  NOT only inside a disposable `work/` dir.
- `generation.log` describing what was resolved.

ARVO samples (`sample.json` has `arvo_image_vul`): the source, build, and PoC all live in
that docker image — do NOT clone the repo. Just initialize `sample_state.json`, ensure
`patch.diff` exists (already provided, or compute from the `-vul`/`-fix` images), copy the
PoC out once (`docker cp <cid>:/tmp/poc <result_dir>/poc`), and record `arvo_image_vul`.
The reproduce + source extraction happen in the Reproducer / GT-generator stages.

Procedure:

1. Initialize `sample_state.json` with the canonical state tool (above).
2. Resolve the repository + vulnerable commit. Record the clone/checkout command
   in `generation.log`; the actual checkout happens in the Reproducer stage.
3. Ensure a PoC artifact exists at a persistent path. If metadata only gives a
   URL, download it once into the sample dir.
4. Ensure `patch.diff` exists. If only pre/post codebases or commits are given,
   compute the unified diff and save it.
5. Record every resolved path in `sample_state.json` and `generation.log`.

Constraints:

- Do not expose fixed code or `patch.diff` content in any downstream
  agent-facing task prompt. It is oracle material for validation only.
- Prefer persistent artifact paths (`final_dataset/pocs/<id>/`, `gt_results/<id>/`)
  over `work/<id>/tmp/...`; anything under `work/` is deleted at cleanup and any
  path recorded into `ground_truth.json` that points there will dangle.
- If a required input cannot be resolved, write partial state and set
  `validation.requires_human_review=true` with a clear failure note.
