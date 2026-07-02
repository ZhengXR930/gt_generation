# Vulnerability Reasoning Harness Proposal

## Literature Retrieval

- Machine-collected seed table: `evaluation_runs/harness_literature/PAPER_TABLE.md` (60 papers).
- The broad seed intentionally covers software/security LLM papers, vulnerability repair/detection, proof-of-vulnerability generation, repository-level agent benchmarks, and agent evaluation/trajectory work.
- The table is a traceable seed, not a final related-work section; some broad agent-evaluation papers are only background.

Representative anchors from the seed table:

- R003 (2026) [LLM-based Vulnerability Detection at Project Scale: An Empirical Study](https://www.semanticscholar.org/paper/6dcc9127919c7a55f02a36aef442dd90bcdda566)
- R006 (2026) [SEC-bench Pro: Can Language Models Solve Long-Horizon Software Security Tasks?](https://www.semanticscholar.org/paper/372a3b02da59e7e3cb1bbc7a34516d057585d11d)
- R021 (2025) [SV-TrustEval-C: Evaluating Structure and Semantic Reasoning in Large Language Models for Source Code Vulnerability Analysis](https://www.semanticscholar.org/paper/80fd0f930b242bdaedcb371b5ac47d2bc1bb9261)
- R022 (2025) [LLM4CVE: Enabling Iterative Automated Vulnerability Repair with Large Language Models](https://www.semanticscholar.org/paper/5dd784817c9284142c0152eba13e41da6d282af5)
- R023 (2025) [FaultLine: Automated Proof-of-Vulnerability Generation Using LLM Agents](https://www.semanticscholar.org/paper/38a351a6f8b523ee02a95ac18b4f6382a19a9a1d)
- R026 (2025) [VulnRepairEval: An Exploit-Based Evaluation Framework for Assessing Large Language Model Vulnerability Repair Capabilities](https://www.semanticscholar.org/paper/41c0abfe56b6c82e43a6b04ec0c63d7f31698248)
- R042 (2024) [Large Language Model for Vulnerability Detection and Repair: Literature Review and the Road Ahead](https://www.semanticscholar.org/paper/5a440c1a3d9e955450a73938181a8321db0ee060)

Additional primary local/source anchors:
- CyberGym README and citation: `external/cybergym/README.md`; arXiv 2506.02548; OpenReview `2YvbLQEdYt`.
- CyberGym level1 task generation shows only `repo-vul.tar.gz`, `description.txt`, `README.md`, and `submit.sh`, which matches the information boundary used in the probe.

## Feasibility Claim

A vulnerability-reasoning agent harness is feasible and useful if it is framed as a diagnostic layer over executable vulnerability tasks, not as a replacement for CyberGym pass/fail. The core novelty is to preserve trajectories and evaluate source/sink identification, propagation trace recovery, root-cause understanding, PoC/patch success, and rationale with deterministic matchers against runtime-grounded GT.

## Harness Logic

1. Preserve the CyberGym level1 oracle: success is still vulnerable-crashes/fixed-passes for submitted PoCs.
2. Add a public-information prompt wrapper that asks the agent to record source, sink, root cause, and step-by-step propagation hypotheses before/while constructing the PoC.
3. Never expose GT, patch.diff, fix code, sanitizer trace, or private PoC during level1.
4. Preserve full OpenHands trajectory, workspace, submitted PoCs, server output, and copied GT artifacts in a diagnostic bundle.
5. Score T1-T5 deterministically; do not use LLM-as-a-judge for T2.
6. Report both final success and diagnostic scores. Treat improved reasoning without success and success without good reasoning as different failure/success modes.

## Evidence From Current Probe

`arvo:14245` is a useful pilot: baseline failed PoC and had zero T2 edge recall, while the reasoning-harness prompt succeeded and substantially improved trace recovery. This is not a statistical result; it is a mechanism check showing that harnessing can change both final success and diagnostic observability.

## Paper-Framing Recommendation

Package the work as an agent harness for memory-safety vulnerability reasoning, with three contributions: a runtime-grounded GT construction workflow, deterministic trajectory evaluators for T1-T5, and a harness/prompt protocol that improves diagnostic observability while preserving benchmark oracles. The strongest near-term claim should be qualitative/mechanistic on 15 samples plus a pilot improvement case; a publishable quantitative claim needs more completed trajectories and at least 30-50 validated GTs.

## Risks and Controls

- Risk: trajectory matching may credit shallow keyword mentions. Control: keep strict edge recall as the main T2 metric and audit excerpts for high-impact claims.
- Risk: prompt wrapper changes agent behavior, so it is not the original CyberGym baseline. Control: report as a harness intervention, not as direct leaderboard comparison.
- Risk: T1 strict may be over-conservative for fuzz harness cases. Control: separate parser/materialized source from harness boundary and inspect false negatives like `arvo:14245`.
- Risk: T4/T5 patch generation/rationale cannot be evaluated in level1. Control: define a separate patch-generation task level that exposes patch-relevant context without leaking developer patch unless scoring patch rationale.

