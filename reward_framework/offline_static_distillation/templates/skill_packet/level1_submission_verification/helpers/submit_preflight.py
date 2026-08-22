#!/usr/bin/env python3
from __future__ import annotations
import argparse, difflib, hashlib, json, re
from pathlib import Path
TERMS=["parser","admission","source","root","cause","sink","trigger"]
EVIDENCE_WORDS=["because","evidence","code","issue","function","branch","condition","size","length","state","parse","accepted","rejected","crash","error","stack","changed","preserve","hypothesis"]

def sha(p):
    if not p.exists(): return None
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1048576),b""):
            h.update(b)
    return h.hexdigest()

def read(p, limit=20000):
    return Path(p).read_text(encoding="utf-8",errors="replace")[:limit] if p and Path(p).exists() else ""

def history(d):
    p=Path(d)/"submit_history.jsonl"; out=[]
    if p.exists():
        for line in p.read_text(encoding="utf-8",errors="replace").splitlines():
            try: out.append(json.loads(line))
            except Exception: pass
    return out

def last_candidate(entries):
    last=None
    for e in entries:
        if e.get("candidate_path"): last=e.get("candidate_path")
    return Path(last) if last else None

def similarity(a,b):
    if not a and not b: return 1.0
    return difflib.SequenceMatcher(None,a,b).ratio()

def evidence_score(text):
    low=text.lower()
    terms=[t for t in TERMS if t in low]
    words=[w for w in EVIDENCE_WORDS if w in low]
    code_refs=re.findall(r"[A-Za-z0-9_./-]+\.(?:c|cc|cpp|h|hpp|py)(?::\d+)?|\b[A-Za-z_][A-Za-z0-9_]+\(\)",text)
    return {"chars":len(text.strip()),"terms_found":terms,"evidence_words_found":words,"code_refs":code_refs[:30],"has_new_evidence":len(text.strip())>=120 and (len(terms)>=3 or len(words)>=4 or len(code_refs)>=1)}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--candidate",required=True)
    ap.add_argument("--analysis")
    ap.add_argument("--state-dir",default=".gt_skill_state")
    ap.add_argument("--note-file")
    ap.add_argument("--evidence-file", action="append", default=[])
    ap.add_argument("--out")
    ap.add_argument("--strict",action="store_true")
    ap.add_argument("--near-duplicate-threshold",type=float,default=0.985)
    a=ap.parse_args()
    c=Path(a.candidate); errs=[]; warns=[]
    if not c.exists(): errs.append("candidate_missing")
    entries=history(a.state_dir); prior_hashes={e.get("candidate_sha256") for e in entries if e.get("candidate_sha256")}
    ch=sha(c) if c.exists() else None
    prev=last_candidate(entries)
    sim=None; exact=False; near=False
    if ch and ch in prior_hashes: exact=True
    if c.exists() and prev and prev.exists():
        cur=c.read_bytes()[:20000].decode("utf-8",errors="replace"); old=prev.read_bytes()[:20000].decode("utf-8",errors="replace")
        sim=similarity(old,cur); near=sim>=a.near_duplicate_threshold
    note=read(a.note_file)
    evidence_text=note + "\n" + "\n".join(read(p) for p in a.evidence_file)
    ev=evidence_score(evidence_text)
    if not a.analysis or not Path(a.analysis).exists(): warns.append("analysis_missing_or_not_provided")
    if a.note_file and len(note.strip())<80: warns.append("pre_submit_note_too_short")
    if exact: warns.append("candidate_exact_duplicate")
    if near: warns.append("candidate_near_duplicate")
    block_recommended=False; block_reasons=[]
    if exact:
        block_recommended=True; block_reasons.append("exact_duplicate_candidate")
    if near and not ev["has_new_evidence"]:
        block_recommended=True; block_reasons.append("near_duplicate_without_new_evidence")
    rep={"ok":not errs,"errors":errs,"warnings":warns,"block_recommended":block_recommended,"block_reasons":block_reasons,"candidate":{"path":str(c),"sha256":ch,"size":c.stat().st_size if c.exists() else None},"previous_candidate":str(prev) if prev else None,"similarity_to_previous":sim,"history_count":len(entries),"evidence_score":ev,"advice":"Block exact duplicates and near-duplicates without new evidence; otherwise submit if this is the best evidence-bearing attempt."}
    txt=json.dumps(rep,indent=2,ensure_ascii=False)
    if a.out: Path(a.out).write_text(txt+"\n",encoding="utf-8")
    print(txt)
    return 1 if a.strict and (errs or block_recommended) else 0
if __name__=="__main__": raise SystemExit(main())
