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
def boolish(v):
    if v is None: return None
    if isinstance(v,bool): return v
    s=str(v).strip().lower()
    if s in {"1","true","yes","y","valid","ok","crash","crashed"}: return True
    if s in {"0","false","no","n","invalid","none","unknown"}: return False
    return v
def file_info(path):
    p=Path(path) if path else None
    return {"path":str(p) if p else None,"sha256":sha(p),"size":p.stat().st_size if p and p.exists() else None}
def record(a):
    path=sf(a.state_dir); c=Path(a.candidate) if a.candidate else None; an=Path(a.analysis) if a.analysis else None; tr=Path(a.trace_file) if a.trace_file else None
    item={"time":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"candidate_path":str(c) if c else None,"candidate_sha256":sha(c),"candidate_size":c.stat().st_size if c and c.exists() else None,"candidate_kind":a.candidate_kind,"analysis_path":str(an) if an else None,"analysis_sha256":sha(an),"trace_path":str(tr) if tr else None,"trace_sha256":sha(tr),"trace_size":tr.stat().st_size if tr and tr.exists() else None,"artifact_valid":boolish(a.artifact_valid),"artifact_error":a.artifact_error,"target_exit_code":a.target_exit_code,"trace_valid":boolish(a.trace_valid),"crash_observed":boolish(a.crash_observed),"submission_status":a.submission_status,"repair_class":a.repair_class,"result_summary":a.result_summary,"preflight_report":a.preflight_report,"runtime_log":a.runtime_log,"note":a.note}
    with path.open("a",encoding="utf-8") as f: f.write(json.dumps(item,ensure_ascii=False)+"\n")
    print(json.dumps({"recorded":item,"history":str(path)},indent=2,ensure_ascii=False)); return 0
def evaluated(e):
    status=str(e.get("submission_status") or "").lower()
    return status in {"evaluated","valid_noncrash","noncrash","non_crash","crash","crashed","success","target_executed","normal_exit","accepted"} or e.get("target_exit_code") is not None
def crash(e):
    return boolish(e.get("crash_observed")) is True or str(e.get("submission_status") or "").lower() in {"crash","crashed","success","task_success"}
def best_candidate(es):
    for e in reversed(es):
        if crash(e):
            return e
    for e in reversed(es):
        if evaluated(e) and boolish(e.get("artifact_valid")) is not False:
            return e
    return es[-1] if es else None
def grouped(es):
    groups={}
    for i,e in enumerate(es,1):
        h=e.get("candidate_sha256") or "<missing>"
        g=groups.setdefault(h,{"candidate_sha256":h,"candidate_path":e.get("candidate_path"),"duplicate_count":0,"attempt_indexes":[],"evaluated":False,"crash_observed":False,"repair_classes":[],"last_status":None,"last_result_summary":None})
        g["duplicate_count"]+=1; g["attempt_indexes"].append(i)
        g["candidate_path"]=e.get("candidate_path") or g["candidate_path"]
        g["evaluated"]=g["evaluated"] or evaluated(e)
        g["crash_observed"]=g["crash_observed"] or crash(e)
        if e.get("repair_class") and e.get("repair_class") not in g["repair_classes"]: g["repair_classes"].append(e.get("repair_class"))
        g["last_status"]=e.get("submission_status") or g["last_status"]
        g["last_result_summary"]=e.get("result_summary") or g["last_result_summary"]
    return list(groups.values())
def summarize(a):
    all_entries=entries(sf(a.state_dir)); es=all_entries[-a.last:]; best=best_candidate(all_entries); lines=["# Submit history summary",""]
    if best:
        lines += ["## Current best candidate",f"- candidate: `{best.get('candidate_path')}`",f"- candidate_sha256: `{best.get('candidate_sha256')}`",f"- status: {best.get('submission_status') or ''}",f"- crash_observed: {best.get('crash_observed')}",f"- result: {best.get('result_summary') or best.get('note') or ''}",""]
    lines += ["## Candidate groups",""]
    for g in grouped(all_entries):
        lines += [f"- `{g.get('candidate_sha256')}` attempts={g.get('attempt_indexes')} duplicates={g.get('duplicate_count')} evaluated={g.get('evaluated')} crash_observed={g.get('crash_observed')} repair_classes={g.get('repair_classes')} status={g.get('last_status') or ''}"]
    if all_entries: lines.append("")
    lines += ["## Recent attempts",""]
    start=max(0,len(all_entries)-len(es))
    for i,e in enumerate(es,start+1): lines += [f"## Attempt {i}",f"- time: {e.get('time')}",f"- candidate: `{e.get('candidate_path')}`",f"- candidate_sha256: `{e.get('candidate_sha256')}`",f"- candidate_kind: {e.get('candidate_kind') or ''}",f"- trace_sha256: `{e.get('trace_sha256')}`",f"- artifact_valid: {e.get('artifact_valid')}",f"- target_exit_code: {e.get('target_exit_code')}",f"- trace_valid: {e.get('trace_valid')}",f"- crash_observed: {e.get('crash_observed')}",f"- submission_status: {e.get('submission_status') or ''}",f"- repair_class: {e.get('repair_class') or ''}",f"- result_summary: {e.get('result_summary') or ''}",f"- note: {e.get('note') or ''}",""]
    txt="\n".join(lines)
    if a.out: Path(a.out).write_text(txt+"\n",encoding="utf-8")
    print(txt); return 0
def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("record"); p.add_argument("--state-dir",default=".poc_skill_state"); p.add_argument("--candidate"); p.add_argument("--candidate-kind"); p.add_argument("--analysis"); p.add_argument("--trace-file"); p.add_argument("--artifact-valid"); p.add_argument("--artifact-error"); p.add_argument("--target-exit-code"); p.add_argument("--trace-valid"); p.add_argument("--crash-observed"); p.add_argument("--submission-status"); p.add_argument("--repair-class"); p.add_argument("--result-summary"); p.add_argument("--preflight-report"); p.add_argument("--runtime-log"); p.add_argument("--note"); p.set_defaults(func=record)
    p=sub.add_parser("summarize"); p.add_argument("--state-dir",default=".poc_skill_state"); p.add_argument("--last",type=int,default=5); p.add_argument("--out"); p.set_defaults(func=summarize)
    a=ap.parse_args(); return a.func(a)
if __name__=="__main__": raise SystemExit(main())
