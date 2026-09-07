# PoC Evaluation Results

Scope: valid_gt.json 500 samples. This report excludes the OpenHands+Claude namespace (`claude-opus-4.6`) because it is still being run. Metrics use relaxed analysis-quality mode: structurally valid `analysis.json` is scored even when the quality lint flags weak anchors. Historical `fine_trace.role="propagation"` is normalized as `intermediate`.

`semantic` is a report-level merge of reasoning `partial` and `full`: for source it equals full; for propagation/obligation/sink it is the mean of partial and full.

Context denominator: file recall uses samples with recoverable `context_visit.json`; function recall uses recoverable samples with at least one visited function.

## Runtime

| Result set | Samples | Analysis | Context files | Context recoverable | Context functions | Reach | Submitted samples | Any crash samples | GT success samples | Submitted PoCs | Evaluated PoCs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| OpenHands + DeepSeek-V4-Flash (`deepseek-v4-flash`) | 500 | 500 | 500 | 500 | 496 | 132 | 132 | 106 | 69 | 208 | 205 |
| DSH + DeepSeek-V4-Flash (`deepseek-harness-v4-flash`) | 500 | 500 | 500 | 500 | 495 | 407 | 408 | 344 | 227 | 940 | 925 |
| OpenHands + GPT-5.5 (`gpt-5.5`) | 500 | 500 | 500 | 500 | 492 | 400 | 400 | 201 | 116 | 496 | 495 |
| OpenHands + GPT-5.4-mini (`gpt-5.4-mini`) | 500 | 500 | 500 | 500 | 496 | 391 | 391 | 34 | 14 | 1231 | 1230 |
| OpenHands + GLM-5.2 (`glm52`) | 500 | 500 | 500 | 500 | 499 | 429 | 429 | 29 | 18 | 815 | 815 |
| Codex + GPT-5.5 (`codex-gpt55`) | 500 | 500 | 500 | 285 | 171 | 500 | 500 | 404 | 271 | 2015 | 2010 |
| Codex + GPT-5.4-mini (`codex-gpt54-mini`) | 500 | 500 | 500 | 84 | 84 | 476 | 496 | 61 | 30 | 1428 | 1406 |
| ClaudeCLI + Claude Opus 4.6 (`claudecli-opus-4-6`) | 500 | 500 | 500 | 500 | 500 | 500 | 474 | 337 | 210 | 4385 | 4376 |

## Reasoning

| Result set | Scored | Source full | Source semantic | Prop partial | Prop full | Prop semantic | Obl partial | Obl full | Obl semantic | Sink partial | Sink full | Sink semantic | Mean semantic |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| OpenHands + DeepSeek-V4-Flash | 500 | 0.032 | 0.032 | 0.190 | 0.190 | 0.190 | 0.064 | 0.014 | 0.039 | 0.108 | 0.062 | 0.085 | 0.086 |
| DSH + DeepSeek-V4-Flash | 500 | 0.124 | 0.124 | 0.451 | 0.451 | 0.451 | 0.184 | 0.058 | 0.121 | 0.232 | 0.138 | 0.185 | 0.220 |
| OpenHands + GPT-5.5 | 500 | 0.082 | 0.082 | 0.388 | 0.388 | 0.388 | 0.186 | 0.130 | 0.158 | 0.230 | 0.092 | 0.161 | 0.197 |
| OpenHands + GPT-5.4-mini | 500 | 0.012 | 0.012 | 0.087 | 0.087 | 0.087 | 0.020 | 0.000 | 0.010 | 0.048 | 0.030 | 0.039 | 0.037 |
| OpenHands + GLM-5.2 | 500 | 0.016 | 0.016 | 0.161 | 0.161 | 0.161 | 0.060 | 0.038 | 0.049 | 0.080 | 0.048 | 0.064 | 0.072 |
| Codex + GPT-5.5 | 500 | 0.120 | 0.120 | 0.497 | 0.497 | 0.497 | 0.130 | 0.058 | 0.094 | 0.252 | 0.140 | 0.196 | 0.227 |
| Codex + GPT-5.4-mini | 500 | 0.016 | 0.016 | 0.107 | 0.107 | 0.107 | 0.046 | 0.024 | 0.035 | 0.036 | 0.014 | 0.025 | 0.046 |
| ClaudeCLI + Claude Opus 4.6 | 500 | 0.044 | 0.044 | 0.233 | 0.233 | 0.233 | 0.144 | 0.060 | 0.102 | 0.206 | 0.108 | 0.157 | 0.134 |

## Fine-Grained Trace

| Result set | Scored | Node recall | Edge recall | Parser | Source | Root | Sink | Trigger |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| OpenHands + DeepSeek-V4-Flash | 500 | 21.7% | 10.7% | 13.2% | 15.8% | 35.8% | 30.6% | 29.0% |
| DSH + DeepSeek-V4-Flash | 500 | 35.6% | 21.3% | 15.4% | 29.4% | 60.2% | 51.4% | 49.6% |
| OpenHands + GPT-5.5 | 500 | 29.1% | 15.3% | 13.4% | 29.6% | 46.6% | 43.2% | 39.6% |
| OpenHands + GPT-5.4-mini | 500 | 10.8% | 3.6% | 9.0% | 9.8% | 17.4% | 17.2% | 16.0% |
| OpenHands + GLM-5.2 | 500 | 12.2% | 4.0% | 6.8% | 9.8% | 22.4% | 22.6% | 20.8% |
| Codex + GPT-5.5 | 500 | 40.0% | 24.2% | 35.6% | 38.8% | 57.2% | 52.8% | 50.2% |
| Codex + GPT-5.4-mini | 500 | 9.4% | 2.8% | 4.8% | 6.4% | 21.2% | 13.0% | 12.0% |
| ClaudeCLI + Claude Opus 4.6 | 500 | 27.3% | 12.5% | 18.8% | 21.0% | 49.0% | 44.4% | 42.6% |

## Context Recall

| Result set | File denominator | File recall | Function denominator | Function recall |
|---|---:|---:|---:|---:|
| OpenHands + DeepSeek-V4-Flash | 500 | 79.1% | 494 | 31.3% |
| DSH + DeepSeek-V4-Flash | 500 | 83.5% | 493 | 37.1% |
| OpenHands + GPT-5.5 | 500 | 89.9% | 490 | 25.1% |
| OpenHands + GPT-5.4-mini | 500 | 68.5% | 493 | 19.6% |
| OpenHands + GLM-5.2 | 500 | 64.5% | 497 | 20.4% |
| Codex + GPT-5.5 | 290 | 59.4% | 92 | 34.4% |
| Codex + GPT-5.4-mini | 84 | 85.8% | 82 | 31.8% |
| ClaudeCLI + Claude Opus 4.6 | 500 | 87.1% | 498 | 39.1% |

Current context-only summary: `evaluation_results/context_recall_current.json`.
