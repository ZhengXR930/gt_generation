#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, time
from pathlib import Path
def sha(p):
    if not p or not p.exists(): return None
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1048576),b""): h.update(b)
    return h.hexdigest()
def sf(d):
    p=Path(d); p.mkdir(parents=True,exist_ok=True); return p/"submit_history.jsonl"
def entries(path):
    if not path.exists(): return []
    out=[]
    for line in path.read_text(encoding="utf-8",errors="replace").splitlines():
        try: out.append(json.loads(line))
        except Exception: pass
    return out
def record(a):
    path=sf(a.state_dir); c=Path(a.candidate) if a.candidate else None; an=Path(a.analysis) if a.analysis else None
    item={"time":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"sample_id":a.sample_id,"candidate_path":str(c) if c else None,"candidate_sha256":sha(c),"candidate_size":c.stat().st_size if c and c.exists() else None,"analysis_path":str(an) if an else None,"analysis_sha256":sha(an),"preflight_report":a.preflight_report,"runtime_log":a.runtime_log,"note":a.note}
    with path.open("a",encoding="utf-8") as f: f.write(json.dumps(item,ensure_ascii=False)+"\n")
    print(json.dumps({"recorded":item,"history":str(path)},indent=2,ensure_ascii=False)); return 0
def summarize(a):
    es=entries(sf(a.state_dir))[-a.last:]; lines=["# Submit history summary",""]
    for i,e in enumerate(es,1): lines += [f"## Attempt {i}",f"- time: {e.get('time')}",f"- candidate: `{e.get('candidate_path')}`",f"- candidate_sha256: `{e.get('candidate_sha256')}`",f"- note: {e.get('note') or ''}",""]
    txt="\n".join(lines)
    if a.out: Path(a.out).write_text(txt+"\n",encoding="utf-8")
    print(txt); return 0
def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("record"); p.add_argument("--state-dir",default=".gt_skill_state"); p.add_argument("--sample-id"); p.add_argument("--candidate"); p.add_argument("--analysis"); p.add_argument("--preflight-report"); p.add_argument("--runtime-log"); p.add_argument("--note"); p.set_defaults(func=record)
    p=sub.add_parser("summarize"); p.add_argument("--state-dir",default=".gt_skill_state"); p.add_argument("--last",type=int,default=5); p.add_argument("--out"); p.set_defaults(func=summarize)
    a=ap.parse_args(); return a.func(a)
if __name__=="__main__": raise SystemExit(main())
