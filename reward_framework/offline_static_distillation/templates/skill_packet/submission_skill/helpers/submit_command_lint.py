#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re, shlex
from pathlib import Path

SHELL_NAMES={"sh","bash","dash","zsh","ksh","fish"}
PYTHON_NAMES={"python","python2","python3","pypy","pypy3"}

def lint(command):
    warnings=[]
    if re.search(r"\|\|\s*true\b",command):
        warnings.append("command_masks_failure_with_or_true")
    if re.search(r";\s*true\b",command):
        warnings.append("command_masks_failure_with_semicolon_true")
    if re.search(r"\bset\s+\+e\b",command):
        warnings.append("command_disables_exit_on_error")
    if re.search(r"submit\.sh\b(?:(?!\|\||[;&\n]).)*\|(?!\|)",command):
        warnings.append("command_pipes_after_submit_sh")
    if re.search(r"submit\.sh\b[^;&\n]*(?:;|&&|\|\|)",command):
        warnings.append("command_runs_after_submit_sh")
    try:
        parts=shlex.split(command)
    except ValueError as exc:
        warnings.append("command_parse_error")
        return warnings, {"parse_error":str(exc)}
    interpreter=Path(parts[0]).name if parts else ""
    target=Path(parts[1]).name if len(parts)>1 else ""
    if interpreter in PYTHON_NAMES and target.endswith(".sh"):
        warnings.append("shell_script_invoked_with_python")
    if interpreter in SHELL_NAMES and re.search(r"\.py$",target):
        warnings.append("python_script_invoked_with_shell")
    return warnings, {"argv":parts}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--command",required=True)
    ap.add_argument("--out")
    args=ap.parse_args()
    warnings,parsed=lint(args.command)
    rep={
        "command":args.command,
        "warnings":warnings,
        "parsed":parsed,
        "executed":False,
        "semantic_candidate_quality_judged":False,
        "note":"warning-only submit command lint; this helper does not execute commands or judge candidate quality"
    }
    txt=json.dumps(rep,indent=2,ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(txt+"\n",encoding="utf-8")
    print(txt)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
