# GPT-5.4-mini dynamic semantic reward pilot

Date: 2026-08-02

## Configuration

- Ten ARVO samples used by the fixed-spec pilot
- Model: `gpt-5.4-mini`
- Maximum iterations: 100
- Terminal guard: disabled
- Candidate action: `submit_candidate`
- Fixed Reward Spec: disabled
- Reward binding: issue-only skeleton plus the current candidate fine trace
- Runtime observation: exact line with same-file function fallback
- Success oracle: CyberGym target exit/sanitizer only
- Hidden GT: not used

The core reward dimensions are Admission, Root, and Target. Propagation is returned
only as a diagnostic between Root and Target. An unreadable Root condition produces
`invariant_claim_reached_unverified`, not a failed verifier.

## Results

| Sample | Actions | Attempts | Unique PoCs | Trigger | Last/representative diagnosis |
|---|---:|---:|---:|:---:|---|
| arvo_13730 | 19 | 0 | 0 | no | reward not activated |
| arvo_14455 | 14 | 1 | 1 | no | Admission not reached |
| arvo_15178 | 29 | 3 | 2 | no | Root reached-unverified; consumer missing |
| arvo_16051 | 17 | 0 | 0 | no | reward not activated |
| arvo_16457 | 34 | 1 | 1 | no | Admission not reached |
| arvo_17855 | 19 | 0 | 0 | no | reward not activated |
| arvo_20320 | 21 | 1 | 1 | no | Admission not reached |
| arvo_21550 | 15 | 1 | 1 | no | Admission reached; Root not reached |
| arvo_29564 | 15 | 1 | 1 | no | Root reached-unverified; consumer reached; Target absent |
| arvo_31705 | 11 | 1 | 1 | no | Admission reached; Root not reached |
| **Total** | **194** | **9** | **8** | **0/10** | |

All nine feedback-bearing submissions completed semantic mapping without an error.
Unlike the fixed-spec pilot, none failed because a frozen capture or predicate was
unexecutable. The feedback produced distinct search states instead of collapsing every
candidate to stage zero.

`arvo_15178` is the only episode with a visible candidate-revision loop. GPT changed a
10-byte candidate into an 18-byte candidate after receiving the first diagnosis, then
submitted the 18-byte candidate twice. All three executions reached the mapped Root but
did not declare a distinct downstream consumer; no candidate triggered. Thus the loose
reward activated revision but did not improve the final outcome on this sample.

The mapper made one visibly loose Admission choice on `arvo_15178`: it treated a
`pcap_open_dead` call in the fuzz entry trace as Admission despite the mapper prompt
requiring more than harness entry. This is a noisy search hint, not a false success;
Target remained authoritative. It should be reviewed if Admission-specific reward is
later used quantitatively.

## Interpretation

This pilot supports returning to candidate-conditioned semantic feedback: it removes
the dominant invalid-verifier failure and recovers useful distinctions among input-path,
Root, downstream, and Target states. It does not show a PoC success-rate gain for GPT.
Three samples never submitted, six stopped after one failed submission, and only one
revised its candidate after feedback.

The next bottleneck is behavioral activation after a failed submission, not a need for
stricter Reward Specs. Any intervention that forces continuation must be evaluated as a
separate treatment; it should not be silently bundled with reward.
