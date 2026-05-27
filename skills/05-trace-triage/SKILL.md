---
name: trace-triage
description: Analyze sanitizer and Valgrind traces for a reproduced memory-safety crash. Use this to identify sink candidates, stack frames, allocation/free context, and missing source-to-sink edges before generating ground truth.
---

# Trace Triage

## Purpose

Convert raw crash traces into actionable reasoning notes for fine-grained GT generation.

This skill does not produce final GT. It identifies evidence, uncertainty, and candidate source/sink/call-chain locations.

## Inputs

- `sanitizer_trace.txt`
- `valgrind_trace.txt`
- Vulnerable source checkout.
- Issue descriptions.
- PoC/PoV.
- Patch diff as oracle context.

## Required Analysis

Identify:

- Primary crash location.
- Memory error class.
- Sink candidate file/function/line.
- Allocation, free, or initialization context when present.
- User-controlled input entry candidates.
- Direct stack frames.
- Missing edges not visible in the crash stack, such as callbacks, virtual dispatch, parser dispatch, function pointers, or generated tables.

## Important Rule

The crash stack is not necessarily the full source-to-sink path. Use it as evidence, then inspect source code to recover the complete vulnerability logic chain.

## Logging

Write triage observations into `generation.log`, including:

- Why a sink candidate was chosen.
- Which frames are library/runtime noise.
- Which frames require source inspection.
- Whether indirect calls or callbacks may exist.

## Failure Conditions

Use these failure types:

- `trace_too_shallow`
- `sink_uncertain`
- `source_uncertain`
- `needs_human_review`

