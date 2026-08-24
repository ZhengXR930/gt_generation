#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re
from pathlib import Path
PARTS=["parser","source","root","sink","trigger"]; STOP={"the","and","for","with","that","this","from","into","when","where","then","have","has","are","was","were","can","not","issue","vulnerability"}
def read(p): return Path(p).read_text(encoding="utf-8",errors="replace") if p and Path(p).exists() else ""
def toks(s): return {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}",s) if t.lower() not in STOP}
def has_any(s, words): return any(w in s for w in words)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--issue-file"); ap.add_argument("--code-notes"); ap.add_argument("--plan",required=True); ap.add_argument("--out"); a=ap.parse_args()
    issue=read(a.issue_file); code=read(a.code_notes); plan=read(a.plan); pl=plan.lower(); present=[p for p in PARTS if p in pl]
    issue_ol=sorted(toks(issue)&toks(plan))[:50]; code_ol=sorted(toks(code)&toks(plan))[:50]; refs=re.findall(r"[A-Za-z0-9_./-]+\.(?:c|cc|cpp|h|hpp|py)(?::\d+)?|\b[A-Za-z_][A-Za-z0-9_]+\(\)",plan)
    warn=[]
    if not has_any(pl,["target input","input contract","file format","protocol","stdin","argument","mode selector","harness operation","archive","container","raw byte"]): warn.append("missing_target_input_contract")
    if not has_any(pl,["artifact check","target-consumed","target consumed","not a trace","not analysis","candidate artifact"]): warn.append("missing_candidate_artifact_check")
    if not has_any(pl,["preserve","preserved","keep stable","unchanged","admission structure","source structure"]): warn.append("missing_preserved_structure")
    if not has_any(pl,["one changed","single mechanism","changed dimension","mutate only","next candidate change"]): warn.append("missing_one_changed_dimension")
    if "sink" in pl and not has_any(pl,["source","candidate feature","bytes","field","record","payload","state"]): warn.append("sink_without_candidate_source_feature")
    if has_any(pl,["harness bug","driver bug","setup bug","wrapper bug"]) and not has_any(pl,["project logic","vulnerable project","target logic"]): warn.append("root_cause_may_be_harness_or_setup")
    if re.search(r"\b(trace|analysis|note|helper output|log)\b.{0,40}\b(poc|candidate|submit)",pl): warn.append("candidate_plan_mentions_trace_as_poc")
    if has_any(pl,["random","sweep","try many","brute force","corpus"]) and not has_any(pl,["repair class","wrong artifact","admission miss","trigger miss","root-cause miss","root cause miss","artifact validity","infrastructure"]): warn.append("broad_mutation_without_repair_class")
    if has_any(pl,["valid input","fixture","corpus"]) and not has_any(pl,["mutate","changed field","changed state","preserve","repair"]): warn.append("ordinary_valid_input_drift")
    if issue and len(issue_ol)<3: warn.append("low_issue_plan_token_overlap")
    if code and len(code_ol)<3 and not refs: warn.append("low_code_plan_evidence")
    rep={"present_five_part_terms":present,"issue_overlap_sample":issue_ol,"code_overlap_sample":code_ol,"code_reference_hits":refs[:50],"warnings":warn,"note":"alignment check only, not correctness evaluator"}
    txt=json.dumps(rep,indent=2,ensure_ascii=False)
    if a.out: Path(a.out).write_text(txt+"\n",encoding="utf-8")
    print(txt); return 0
if __name__=="__main__": raise SystemExit(main())
