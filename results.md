# PoC Evaluation Results

Scope: `valid_gt.json` 500 samples. This report covers the complete formal result sets and excludes the partial OpenHands+Claude namespace (`claude-opus-4.6`). Metrics use relaxed analysis-quality mode so structurally valid `analysis.json` artifacts are scored even when quality lint flags weak anchors. Historical `fine_trace.role="propagation"` is normalized as `intermediate`.

Codex context recall is scored only on recoverable `context_visit.json` files; the remaining unrecoverable Codex GPT-5.5 checkpoints are kept as missing context rather than filled from `analysis.json`.

## Runtime / Reachability

| Result set | Samples | Analysis | Reach files | Submitted samples | Any crash samples | GT success samples | Submitted PoCs | Evaluated PoCs | GT-triggered PoCs | FP PoCs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| OpenHands + DeepSeek-V4-Flash (`deepseek-v4-flash`) | 500 | 500 | 132 | 132 | 106 | 69 | 208 | 205 | 70 | 36 |
| DSH + DeepSeek-V4-Flash (`deepseek-harness-v4-flash`) | 500 | 500 | 408 | 408 | 343 | 227 | 940 | 926 | 233 | 124 |
| OpenHands + GPT-5.5 (`gpt-5.5`) | 500 | 500 | 400 | 396 | 201 | 116 | 496 | 495 | 116 | 88 |
| OpenHands + GPT-5.4-mini (`gpt-5.4-mini`) | 500 | 500 | 391 | 391 | 33 | 14 | 1231 | 1230 | 16 | 22 |
| OpenHands + GLM-5.2 (`glm52`) | 500 | 500 | 429 | 426 | 29 | 18 | 815 | 815 | 35 | 11 |
| Codex + GPT-5.5 (`codex-gpt55`) | 500 | 500 | 500 | 500 | 404 | 271 | 2015 | 2010 | 295 | 209 |
| Codex + GPT-5.4-mini (`codex-gpt54-mini`) | 500 | 500 | 496 | 496 | 61 | 30 | 1428 | 1428 | 35 | 47 |
| ClaudeCLI + Claude Opus 4.6 (`claudecli-opus-4-6`) | 500 | 500 | 500 | 474 | 333 | 210 | 4385 | 4376 | 231 | 262 |

## Reasoning

| Result set | Scored | Source full | Prop semantic | Obligation semantic | Sink semantic |
|---|---:|---:|---:|---:|---:|
| OpenHands + DeepSeek-V4-Flash | 500 | 0.032 | 0.190 | 0.064 | 0.108 |
| DSH + DeepSeek-V4-Flash | 500 | 0.124 | 0.451 | 0.184 | 0.232 |
| OpenHands + GPT-5.5 | 500 | 0.082 | 0.388 | 0.186 | 0.230 |
| OpenHands + GPT-5.4-mini | 500 | 0.012 | 0.087 | 0.020 | 0.048 |
| OpenHands + GLM-5.2 | 500 | 0.016 | 0.161 | 0.060 | 0.080 |
| Codex + GPT-5.5 | 500 | 0.120 | 0.497 | 0.130 | 0.252 |
| Codex + GPT-5.4-mini | 500 | 0.016 | 0.107 | 0.046 | 0.036 |
| ClaudeCLI + Claude Opus 4.6 | 500 | 0.044 | 0.233 | 0.144 | 0.206 |

## Fine-Grained Trace

| Result set | Scored | Node recall | Edge recall |
|---|---:|---:|---:|
| OpenHands + DeepSeek-V4-Flash | 500 | 21.7% | 10.7% |
| DSH + DeepSeek-V4-Flash | 500 | 35.6% | 21.3% |
| OpenHands + GPT-5.5 | 500 | 29.1% | 15.3% |
| OpenHands + GPT-5.4-mini | 500 | 10.8% | 3.6% |
| OpenHands + GLM-5.2 | 500 | 12.2% | 4.0% |
| Codex + GPT-5.5 | 500 | 40.0% | 24.2% |
| Codex + GPT-5.4-mini | 500 | 9.4% | 2.8% |
| ClaudeCLI + Claude Opus 4.6 | 500 | 27.3% | 12.5% |

## Context Recall

| Result set | File denominator | File recall | Function denominator | Function recall |
|---|---:|---:|---:|---:|
| OpenHands + DeepSeek-V4-Flash | 500 | 79.1% | 496 | 31.3% |
| DSH + DeepSeek-V4-Flash | 500 | 83.5% | 495 | 37.1% |
| OpenHands + GPT-5.5 | 500 | 89.9% | 492 | 25.1% |
| OpenHands + GPT-5.4-mini | 500 | 68.5% | 495 | 19.6% |
| OpenHands + GLM-5.2 | 500 | 64.5% | 499 | 20.4% |
| Codex + GPT-5.5 | 319 | 57.0% | 235 | 34.3% |
| Codex + GPT-5.4-mini | 500 | 52.1% | 499 | 14.2% |
| ClaudeCLI + Claude Opus 4.6 | 500 | 87.1% | 500 | 39.1% |

Raw summary: `evaluation_results/formal_results_fast_summary.json`. Context-only summary: `evaluation_results/context_recall_current.json`.
