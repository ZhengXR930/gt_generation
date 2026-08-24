#!/usr/bin/env python3
from __future__ import annotations
import argparse, difflib, hashlib, json
from pathlib import Path

def sha(p):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1048576),b""):
            h.update(b)
    return h.hexdigest()

def last_candidate(hist):
    if not hist or not Path(hist).exists(): return None
    last=None
    for line in Path(hist).read_text(encoding="utf-8",errors="replace").splitlines():
        try:
            item=json.loads(line); last=item.get("candidate_path") or last
        except Exception:
            pass
    return last

def history_entries(hist):
    if not hist or not Path(hist).exists(): return []
    out=[]
    for line in Path(hist).read_text(encoding="utf-8",errors="replace").splitlines():
        try: out.append(json.loads(line))
        except Exception: pass
    return out

def text(path, limit):
    return path.read_bytes()[:limit].decode("utf-8",errors="replace")

def preview(path, limit=160):
    data=path.read_bytes()[:limit]
    is_text=all((b in b"\t\r\n" or 32<=b<127) for b in data)
    body=data.decode("utf-8",errors="replace") if is_text else data.hex()
    return {"kind":"text" if is_text else "binary","bytes_sampled":len(data),"preview":body}

def similarity(a, b):
    if not a and not b: return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()

def common_prefix(a,b):
    n=0
    for x,y in zip(a,b):
        if x!=y: break
        n+=1
    return n

def outcome_context(e):
    return {k:e.get(k) for k in ["submission_status","target_exit_code","artifact_valid","trace_valid","crash_observed","repair_class","result_summary","note"] if e.get(k) is not None}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--current",required=True)
    ap.add_argument("--previous")
    ap.add_argument("--history-jsonl")
    ap.add_argument("--out")
    ap.add_argument("--max-bytes",type=int,default=20000)
    a=ap.parse_args()
    cur=Path(a.current); prev=Path(a.previous) if a.previous else None
    hist=history_entries(a.history_jsonl)
    if prev is None:
        lc=last_candidate(a.history_jsonl); prev=Path(lc) if lc else None
    rep={"current_path":str(cur),"current_exists":cur.exists(),"previous_path":str(prev) if prev else None,"previous_exists":prev.exists() if prev else False,"exact_duplicate":None,"similarity_ratio":None,"near_duplicate":None,"current_sha256":None,"previous_sha256":None,"current_size":None,"previous_size":None,"size_delta":None,"common_prefix_length":None,"current_preview":None,"previous_preview":None,"diff_preview":[],"prior_exact_matches":[],"duplicate_attempt_indexes":[],"nearest_prior_candidate":None}
    if cur.exists():
        rep["current_sha256"]=sha(cur); rep["current_size"]=cur.stat().st_size; rep["current_preview"]=preview(cur)
    if prev and prev.exists() and cur.exists():
        rep["previous_sha256"]=sha(prev); rep["previous_size"]=prev.stat().st_size
        rep["exact_duplicate"]=rep["current_sha256"]==rep["previous_sha256"]
        old=text(prev,a.max_bytes); new=text(cur,a.max_bytes)
        rep["similarity_ratio"]=similarity(old,new)
        rep["near_duplicate"]=bool(rep["similarity_ratio"] is not None and rep["similarity_ratio"]>=0.985)
        rep["size_delta"]=rep["current_size"]-rep["previous_size"]
        rep["common_prefix_length"]=common_prefix(prev.read_bytes(),cur.read_bytes())
        rep["previous_preview"]=preview(prev)
        if not rep["exact_duplicate"]:
            rep["diff_preview"]=list(difflib.unified_diff(old.splitlines(),new.splitlines(),fromfile=str(prev),tofile=str(cur),lineterm=""))[:200]
    if cur.exists() and hist:
        nearest=None
        for i,e in enumerate(hist,1):
            raw_path=e.get("candidate_path")
            p=Path(raw_path) if raw_path else None
            h=e.get("candidate_sha256") or (sha(p) if p and p.exists() else None)
            if h and h==rep["current_sha256"]:
                rep["duplicate_attempt_indexes"].append(i)
                rep["prior_exact_matches"].append({"attempt_index":i,"candidate_path":e.get("candidate_path"),"candidate_sha256":h,"outcome":outcome_context(e)})
            if p and p.exists():
                score=similarity(text(p,a.max_bytes),text(cur,a.max_bytes))
                cand={"attempt_index":i,"candidate_path":str(p),"candidate_sha256":h,"similarity_ratio":score,"size_delta":rep["current_size"]-p.stat().st_size,"common_prefix_length":common_prefix(p.read_bytes(),cur.read_bytes()),"prior_preview":preview(p),"outcome":outcome_context(e)}
                if nearest is None or score>nearest["similarity_ratio"]:
                    nearest=cand
        rep["nearest_prior_candidate"]=nearest
    txt=json.dumps(rep,indent=2,ensure_ascii=False)
    if a.out: Path(a.out).write_text(txt+"\n",encoding="utf-8")
    print(txt); return 0
if __name__=="__main__": raise SystemExit(main())
