#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path

def read(path, limit=8000):
    return Path(path).read_text(encoding="utf-8", errors="replace")[:limit] if path and Path(path).exists() else ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-file")
    ap.add_argument("--code-notes")
    ap.add_argument("--previous-plan")
    ap.add_argument("--out")
    args = ap.parse_args()
    text = f"""# Candidate plan

## Issue evidence excerpt
```text
{read(args.issue_file)}
```

## Code evidence excerpt
```text
{read(args.code_notes)}
```

## Previous plan excerpt
```text
{read(args.previous_plan)}
```

## Current hypothesis
State the current best explanation of how the candidate should reproduce the issue. Mention parser/admission, source, root cause, sink, or trigger only where they are relevant.

-

## Target Input Contract
Name the artifact the target consumes: file format, protocol/message, config/script text, archive/container, raw byte stream, stdin/file argument, mode selector, or harness operation.

-

## Candidate Artifact Check
State why the planned PoC is the target-consumed artifact rather than a trace, analysis, helper output, source file, or note.

-

## Evidence supporting it
Cite issue text, code behavior, prior candidate behavior, or ordinary execution output if available.

-

## Last Feedback Classification
Classify the previous miss if any: wrong artifact, parser/admission miss, source/root-cause miss, sink miss, trigger miss, artifact validity failure, infrastructure failure, or unknown.

-

## Preserved Structure
Name the admission, source, container, operation sequence, or routed component that should stay stable in the next candidate.

-

## One Changed Dimension
Name the single mechanism dimension to change next: envelope/framing/mode/syntax, semantic relation, size/count/offset, state transition, nested component, operation trigger, or final trigger bytes.

-

## Concrete Trigger Feature
Describe the bytes, structured fields, command, state transition, delimiter, encoded payload, object reference, or operation expected to make the bug observable.

-

## Main unresolved gap
Name the single most important uncertainty blocking a better PoC.

-

## Next candidate change
Describe the concrete change to make next. State what should be preserved from the previous candidate.

-

## Why this candidate is informative
Explain what this attempt will clarify even if it fails.

-
"""
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
