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

def text(path, limit):
    return path.read_bytes()[:limit].decode("utf-8",errors="replace")

def similarity(a, b):
    if not a and not b: return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--current",required=True)
    ap.add_argument("--previous")
    ap.add_argument("--history-jsonl")
    ap.add_argument("--out")
    ap.add_argument("--max-bytes",type=int,default=20000)
    a=ap.parse_args()
    cur=Path(a.current); prev=Path(a.previous) if a.previous else None
    if prev is None:
        lc=last_candidate(a.history_jsonl); prev=Path(lc) if lc else None
    rep={"current_path":str(cur),"current_exists":cur.exists(),"previous_path":str(prev) if prev else None,"previous_exists":prev.exists() if prev else False,"exact_duplicate":None,"similarity_ratio":None,"near_duplicate":None,"current_sha256":None,"previous_sha256":None,"current_size":None,"previous_size":None,"diff_preview":[]}
    if cur.exists():
        rep["current_sha256"]=sha(cur); rep["current_size"]=cur.stat().st_size
    if prev and prev.exists() and cur.exists():
        rep["previous_sha256"]=sha(prev); rep["previous_size"]=prev.stat().st_size
        rep["exact_duplicate"]=rep["current_sha256"]==rep["previous_sha256"]
        old=text(prev,a.max_bytes); new=text(cur,a.max_bytes)
        rep["similarity_ratio"]=similarity(old,new)
        rep["near_duplicate"]=bool(rep["similarity_ratio"] is not None and rep["similarity_ratio"]>=0.985)
        if not rep["exact_duplicate"]:
            rep["diff_preview"]=list(difflib.unified_diff(old.splitlines(),new.splitlines(),fromfile=str(prev),tofile=str(cur),lineterm=""))[:200]
    txt=json.dumps(rep,indent=2,ensure_ascii=False)
    if a.out: Path(a.out).write_text(txt+"\n",encoding="utf-8")
    print(txt); return 0
if __name__=="__main__": raise SystemExit(main())
