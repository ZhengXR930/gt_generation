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

def read_bytes(p, limit=20000):
    return Path(p).read_bytes()[:limit] if p and Path(p).exists() else b""

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

def as_bool(v):
    if isinstance(v,bool): return v
    if v is None: return None
    return str(v).strip().lower() in {"1","true","yes","y","valid","ok","crash","crashed"}

def valid_noncrash(e):
    status=str(e.get("submission_status") or e.get("wrapper_result") or "").lower()
    if status in {"crash","crashed","success","task_success"}:
        return False
    crash=as_bool(e.get("crash_observed"))
    if crash is True:
        return False
    artifact_valid=as_bool(e.get("artifact_valid"))
    evaluated=status in {"evaluated","valid_noncrash","noncrash","non_crash","target_executed","normal_exit","accepted"} or e.get("target_exit_code") is not None
    return bool(evaluated and artifact_valid is not False and crash is not True)

def companion_paths(a):
    items=[]
    for label,path in [("analysis",a.analysis),("trace",a.trace_file),("note",a.note_file)]:
        if path:
            items.append((label,Path(path)))
    for path in a.evidence_file:
        items.append(("evidence",Path(path)))
    return items

def same_file(a,b):
    try:
        return a.exists() and b.exists() and a.resolve()==b.resolve()
    except Exception:
        return False

def shape_warnings(data):
    if not data:
        return []
    warns=[]
    text=data.decode("utf-8",errors="replace")
    stripped=text.lstrip()
    low=text.lower()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj=json.loads(text)
            keys=set(obj.keys()) if isinstance(obj,dict) else set()
            if keys & {"trace","events","frames","stack","target_exit","target_exit_code","crash_observed","coverage"}:
                warns.append("candidate_looks_like_trace_json")
            if keys & {"analysis","root_cause","sink","trigger","hypothesis","evidence","candidate_goal"}:
                warns.append("candidate_looks_like_analysis_json")
        except Exception:
            pass
    if re.search(r"(?m)^\s*(#include|import |from [A-Za-z0-9_.]+ import |def |class |int main\s*\(|void main\s*\(|LLVMFuzzerTestOneInput)",text):
        warns.append("candidate_looks_like_source_or_harness_text")
    if re.search(r"(?im)^\s*#\s*(readme|prompt|analysis|instructions)\b|you are an? |system prompt|task:",text):
        warns.append("candidate_looks_like_prompt_or_readme_text")
    if re.search(r"\\x[0-9a-fA-F]{2}|\\u00[0-9a-fA-F]{2}|b['\"][^'\"]*\\x",text):
        warns.append("candidate_contains_literal_escaped_binary_text")
    return warns

def textual_signal_summary(text):
    low=text.lower()
    terms=[t for t in TERMS if t in low]
    words=[w for w in EVIDENCE_WORDS if w in low]
    code_refs=re.findall(r"[A-Za-z0-9_./-]+\.(?:c|cc|cpp|h|hpp|py)(?::\d+)?|\b[A-Za-z_][A-Za-z0-9_]+\(\)",text)
    return {
        "chars":len(text.strip()),
        "terms_found":terms,
        "evidence_words_found":words,
        "code_refs":code_refs[:30],
        "semantic_quality_judged":False,
        "note":"Textual signals are logged for Teacher review only; this helper does not decide semantic evidence gain."
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--candidate",required=True)
    ap.add_argument("--artifact-kind")
    ap.add_argument("--analysis")
    ap.add_argument("--trace-file")
    ap.add_argument("--state-dir",default=".poc_skill_state")
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
    duplicate_after_valid_noncrash=False; duplicate_attempts=[]
    if ch:
        for i,e in enumerate(entries,1):
            if e.get("candidate_sha256")==ch:
                duplicate_attempts.append(i)
                if valid_noncrash(e):
                    duplicate_after_valid_noncrash=True
    if c.exists() and prev and prev.exists():
        cur=c.read_bytes()[:20000].decode("utf-8",errors="replace"); old=prev.read_bytes()[:20000].decode("utf-8",errors="replace")
        sim=similarity(old,cur); near=sim>=a.near_duplicate_threshold
    note=read(a.note_file)
    evidence_text=note + "\n" + "\n".join(read(p) for p in a.evidence_file)
    textual=textual_signal_summary(evidence_text)
    candidate_same_as_companion_artifact=[]
    if c.exists():
        for label,path in companion_paths(a):
            ph=sha(path) if path.exists() else None
            same=bool((ch and ph and ch==ph) or same_file(c,path))
            if same:
                candidate_same_as_companion_artifact.append({"kind":label,"path":str(path),"sha256":ph})
        warns.extend(shape_warnings(read_bytes(c)))
    if not a.analysis or not Path(a.analysis).exists(): warns.append("analysis_missing_or_not_provided")
    if a.note_file and len(note.strip())<80: warns.append("pre_submit_note_too_short")
    if exact: warns.append("candidate_exact_duplicate")
    if duplicate_after_valid_noncrash: warns.append("duplicate_after_valid_noncrash")
    if candidate_same_as_companion_artifact: warns.append("candidate_same_as_companion_artifact")
    if near: warns.append("candidate_near_duplicate_structural_warning")
    block_recommended=False; block_reasons=[]
    if "candidate_missing" in errs:
        block_recommended=True; block_reasons.append("candidate_missing")
    if candidate_same_as_companion_artifact:
        block_recommended=True; block_reasons.append("candidate_same_as_companion_artifact")
    if duplicate_after_valid_noncrash:
        block_recommended=True; block_reasons.append("duplicate_after_valid_noncrash")
    rep={
        "ok":not errs,
        "errors":errs,
        "warnings":warns,
        "block_recommended":block_recommended,
        "block_reasons":block_reasons,
        "candidate":{"path":str(c),"artifact_kind":a.artifact_kind,"sha256":ch,"size":c.stat().st_size if c.exists() else None},
        "candidate_same_as_companion_artifact":candidate_same_as_companion_artifact,
        "previous_candidate":str(prev) if prev else None,
        "similarity_to_previous":sim,
        "near_duplicate_threshold":a.near_duplicate_threshold,
        "history_count":len(entries),
        "duplicate_attempt_indexes":duplicate_attempts,
        "duplicate_after_valid_noncrash":duplicate_after_valid_noncrash,
        "textual_signal_summary":textual,
        "semantic_evidence_gain_decision":"not_computed_by_helper",
        "advice":"Block missing candidates, exact companion-artifact identity, and duplicates after valid non-crashing evaluated attempts. Warn on ambiguous shape or near-duplicates and leave semantic evidence gain to Teacher review."
    }
    txt=json.dumps(rep,indent=2,ensure_ascii=False)
    if a.out: Path(a.out).write_text(txt+"\n",encoding="utf-8")
    print(txt)
    return 1 if a.strict and (errs or block_recommended) else 0
if __name__=="__main__": raise SystemExit(main())
