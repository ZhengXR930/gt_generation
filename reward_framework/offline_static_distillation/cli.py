from __future__ import annotations

import argparse, json, re, shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_ARTIFACT_ROOTS = [Path("reward_framework/harness_evolution_runs"), Path("reward_framework/harness_evolution/lesson_training")]
DEFAULT_SPLIT = Path("gt_results/train_gt.json")
MODULE_DIR = Path(__file__).resolve().parent
PROMPT_DIR = MODULE_DIR / "prompts"
TEMPLATE_PACKET_DIR = MODULE_DIR / "templates" / "skill_packet"
TEXT_EXTS = {".txt", ".log", ".md", ".json", ".jsonl", ".py", ".sh", ".c", ".cc", ".cpp", ".h", ".hpp"}

@dataclass
class ArtifactBundle:
    sample_id: str
    root: Path
    files: dict[str, list[Path]]

def read_text(path: Path, max_chars: int = 20000) -> str:
    try: data = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc: return f"[unreadable: {exc}]"
    if len(data) > max_chars:
        head = max_chars // 2; tail = max_chars - head
        return data[:head] + f"\n\n[... trimmed {len(data)-max_chars} chars from {path} ...]\n\n" + data[-tail:]
    return data

def load_json(path: Path) -> Any: return json.loads(read_text(path, 10_000_000))

def normalize_sample_list(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        for key in ("samples", "sample_ids", "ids", "train", "data"):
            if key in raw: return normalize_sample_list(raw[key])
        if all(isinstance(k, str) for k in raw.keys()): return list(raw.keys())
    if isinstance(raw, list):
        out=[]
        for item in raw:
            if isinstance(item, str): out.append(item)
            elif isinstance(item, dict):
                for key in ("sample_id", "id", "name"):
                    if key in item: out.append(str(item[key])); break
        return out
    raise ValueError("Unsupported split JSON format")

def safe_name(sample_id: str) -> str: return re.sub(r"[^A-Za-z0-9_.-]+", "_", sample_id)

def looks_relevant(path: Path) -> bool:
    lower=path.name.lower(); keys=["trajectory","submit","submission","candidate","poc","analysis","reasoning","reachability","runtime","report","controller","issue","run_sample","openhands","eval","diagnostic"]
    return path.suffix.lower() in TEXT_EXTS and any(k in lower for k in keys)

def file_kind(path: Path) -> str:
    n=path.name.lower()
    if any(k in n for k in ("trajectory","controller","openhands","run_sample")): return "trajectory_or_controller"
    if any(k in n for k in ("submit","submission")): return "submit_history"
    if any(k in n for k in ("poc","candidate")): return "candidate_or_poc"
    if "analysis" in n: return "analysis_json"
    if "reason" in n: return "reasoning_diagnostic"
    if "reach" in n: return "reachability_diagnostic"
    if any(k in n for k in ("runtime","report","eval")): return "runtime_or_eval_report"
    if "issue" in n: return "issue_description"
    return "other_relevant"

def discover_sample_dirs(artifact_roots: Iterable[Path], sample_ids: list[str]) -> dict[str, list[Path]]:
    wanted=set(sample_ids); by={sid:[] for sid in sample_ids}
    for root in artifact_roots:
        if not root.exists(): continue
        for path in root.rglob("*"):
            if not path.is_dir(): continue
            cands={path.name,path.parent.name}; m=path/"manifest.json"
            if m.exists():
                try:
                    data=load_json(m)
                    if isinstance(data,dict):
                        for k in ("sample_id","sample","id"):
                            if k in data: cands.add(str(data[k]))
                except Exception: pass
            for sid in cands:
                if sid in wanted: by[sid].append(path)
    return by

def score_dir(path: Path) -> tuple[int, float]: return (sum(1 for p in path.rglob("*") if p.is_file() and looks_relevant(p)), path.stat().st_mtime)

def choose_bundle(sample_id: str, dirs: list[Path], prefer: str | None) -> ArtifactBundle | None:
    if prefer:
        pref=[d for d in dirs if prefer in str(d)]
        if pref: dirs=pref
    if not dirs: return None
    chosen=sorted(dirs,key=score_dir,reverse=True)[0]; files={}
    for p in chosen.rglob("*"):
        if p.is_file() and looks_relevant(p): files.setdefault(file_kind(p),[]).append(p)
    for ps in files.values(): ps.sort(key=lambda p:str(p))
    return ArtifactBundle(sample_id,chosen,files)

def extract_issue(raw: Any, sid: str) -> str | None:
    containers=[raw]
    if isinstance(raw,dict): containers=[raw.get("samples"),raw.get("data"),raw]
    for entries in containers:
        if isinstance(entries,dict):
            item=entries.get(sid)
            if isinstance(item,dict):
                for k in ("issue","issue_description","description","prompt"):
                    if k in item: return str(item[k])
        if isinstance(entries,list):
            for item in entries:
                if isinstance(item,dict) and str(item.get("sample_id") or item.get("id") or item.get("name"))==sid:
                    for k in ("issue","issue_description","description","prompt"):
                        if k in item: return str(item[k])
    return None

def render_observation(bundle: ArtifactBundle | None, sid: str, raw: Any, max_file_chars: int, max_files_per_kind: int) -> str:
    lines=[f"# Observation: {sid}",""]; issue=extract_issue(raw,sid)
    if issue: lines += ["## Issue description from split","","```text",issue[:max_file_chars],"```",""]
    if bundle is None:
        lines += ["## Artifact status","","No local artifact bundle was discovered for this sample.",""]; return "\n".join(lines)
    lines += ["## Artifact provenance","",f"- selected_root: `{bundle.root}`",""]
    order=["issue_description","trajectory_or_controller","submit_history","candidate_or_poc","analysis_json","reasoning_diagnostic","reachability_diagnostic","runtime_or_eval_report","other_relevant"]
    for kind in order:
        for path in bundle.files.get(kind,[])[:max_files_per_kind]:
            try: rel=path.relative_to(bundle.root)
            except ValueError: rel=path
            lines += [f"## {kind}: {rel}","","```text",read_text(path,max_file_chars),"```",""]
    return "\n".join(lines)

def package_observations(args):
    split=Path(args.split); raw=load_json(split); ids=normalize_sample_list(raw)
    if args.limit: ids=ids[:args.limit]
    roots=[Path(p) for p in args.artifact_root]; out=Path(args.out); obs=out/"observations"; obs.mkdir(parents=True,exist_ok=True)
    dirs=discover_sample_dirs(roots,ids); manifest={"split":str(split),"sample_count":len(ids),"artifact_roots":[str(p) for p in roots],"samples":[]}
    for sid in ids:
        b=choose_bundle(sid,dirs.get(sid,[]),args.prefer_run_substring); op=obs/f"{safe_name(sid)}.md"
        op.write_text(render_observation(b,sid,raw,args.max_file_chars,args.max_files_per_kind),encoding="utf-8")
        manifest["samples"].append({"sample_id":sid,"observation":str(op),"artifact_root":str(b.root) if b else None,"file_kinds":{k:len(v) for k,v in (b.files if b else {}).items()}})
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding="utf-8")
    print(f"wrote {len(ids)} observations to {obs}"); print("missing_artifacts="+str(sum(1 for s in manifest["samples"] if not s["artifact_root"])))

def prompt(name): return read_text(PROMPT_DIR/name,100000)
def build_shard_prompts(args):
    rd=Path(args.run_dir); man=load_json(rd/"manifest.json"); samples=man["samples"] if args.include_missing else [s for s in man["samples"] if s.get("artifact_root")]
    out=rd/"teacher_shard_prompts"; out.mkdir(parents=True,exist_ok=True)
    for idx in range(0,len(samples),args.shard_size):
        parts=[prompt("shard_teacher_prompt.txt"),"\n# Observation shard\n"]
        for item in samples[idx:idx+args.shard_size]: parts += ["\n---\n",read_text(Path(item["observation"]),args.max_observation_chars)]
        (out/f"shard_{idx//args.shard_size:04d}.prompt.md").write_text("\n".join(parts),encoding="utf-8")
    print(f"wrote shard prompts to {out}")
def build_global_prompt(args):
    rd=Path(args.run_dir); reports=Path(args.shard_reports_dir) if args.shard_reports_dir else rd/"teacher_shard_reports"; parts=[prompt("global_teacher_prompt.txt"),"\n# Shard reports\n"]
    if reports.exists():
        for p in sorted(reports.glob("*.md")): parts += [f"\n---\n## {p.name}\n",read_text(p,args.max_report_chars)]
    else: parts += [f"\n[missing shard reports dir: {reports}]\n"]
    (rd/"global_teacher.prompt.md").write_text("\n".join(parts),encoding="utf-8"); print(f"wrote {rd/'global_teacher.prompt.md'}")
def append_tree(parts,title,root,max_chars):
    parts += [f"\n# {title}: {root}\n"]
    if not root.exists(): parts += ["\n[missing]\n"]; return
    for p in sorted(x for x in root.rglob("*") if x.is_file() and x.suffix.lower() in TEXT_EXTS): parts += [f"\n## {p.relative_to(root)}\n","```text",read_text(p,max_chars),"```\n"]
def build_curator_prompt(args):
    rd=Path(args.run_dir); parts=[prompt("evolver_curator_prompt.txt")]; append_tree(parts,"Current skill packet",Path(args.skill_packet),args.max_skill_file_chars)
    proposal=Path(args.proposal) if args.proposal else rd/"global_teacher_report.md"; parts += ["\n# Teacher proposal\n",read_text(proposal,args.max_report_chars)]
    (rd/"evolver_curator.prompt.md").write_text("\n".join(parts),encoding="utf-8"); print(f"wrote {rd/'evolver_curator.prompt.md'}")

EXPECTED_CHUNKS = {
    "submission_skill/SKILL.md": [
        "S.A-submit-loop",
        "S.B-evidence-gain-gate",
        "S.C-analysis-history-state",
        "S.D-helper-safety",
    ],
    "reproduction_skill/SKILL.md": [
        "R.A-reproduction-loop",
        "R.B-five-part-working-representation",
        "R.C-candidate-feedback-repair",
        "R.D-learned-reproduction-lessons",
        "R.E-helper-safety",
    ],
}
EXPECTED_HELPERS = [
    "submission_skill/helpers/candidate_diff.py",
    "submission_skill/helpers/submit_command_lint.py",
    "submission_skill/helpers/submit_history.py",
    "submission_skill/helpers/submit_preflight.py",
    "reproduction_skill/helpers/candidate_plan.py",
    "reproduction_skill/helpers/issue_code_alignment.py",
]

def validate_skill_packet(args):
    root = Path(args.skill_packet)
    errors = []
    warnings = []
    found_chunks = {}
    for rel, expected in EXPECTED_CHUNKS.items():
        path = root / rel
        if not path.exists():
            errors.append(f"missing_skill_md:{rel}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        present = []
        for cid in expected:
            marker = f"block-id: {cid}"
            if marker in text:
                present.append(cid)
            else:
                errors.append(f"missing_chunk:{rel}:{cid}")
        found_chunks[rel] = present
        extra = re.findall(r"block-id:\s*([^\s>]+)", text)
        unexpected = [x for x in extra if x not in expected]
        if unexpected:
            warnings.append(f"unexpected_chunks:{rel}:{','.join(unexpected)}")
    helper_status = {}
    for rel in EXPECTED_HELPERS:
        path = root / rel
        helper_status[rel] = path.exists()
        if not path.exists():
            errors.append(f"missing_helper:{rel}")
    report = {
        "ok": not errors,
        "skill_packet": str(root),
        "errors": errors,
        "warnings": warnings,
        "chunks": found_chunks,
        "helpers": helper_status,
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if errors else 0

def copy_tree(src,dst,overwrite):
    if dst.exists() and not overwrite: raise SystemExit(f"output exists: {dst}; pass --overwrite to replace files")
    dst.mkdir(parents=True,exist_ok=True)
    for p in src.rglob("*"):
        rel=p.relative_to(src); target=dst/rel
        if p.is_dir(): target.mkdir(parents=True,exist_ok=True)
        else: target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(str(p),str(target))
def scaffold_skill_packet(args): copy_tree(TEMPLATE_PACKET_DIR,Path(args.out),args.overwrite); print(f"scaffolded full skill packet at {args.out}")
def main():
    ap=argparse.ArgumentParser(description="Offline static skill distillation utilities"); sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("package-observations"); p.add_argument("--split",default=str(DEFAULT_SPLIT)); p.add_argument("--artifact-root",action="append",default=[str(p) for p in DEFAULT_ARTIFACT_ROOTS]); p.add_argument("--out",required=True); p.add_argument("--limit",type=int); p.add_argument("--prefer-run-substring"); p.add_argument("--max-file-chars",type=int,default=20000); p.add_argument("--max-files-per-kind",type=int,default=8); p.set_defaults(func=package_observations)
    p=sub.add_parser("build-shard-prompts"); p.add_argument("--run-dir",required=True); p.add_argument("--shard-size",type=int,default=10); p.add_argument("--include-missing",action="store_true"); p.add_argument("--max-observation-chars",type=int,default=60000); p.set_defaults(func=build_shard_prompts)
    p=sub.add_parser("build-global-prompt"); p.add_argument("--run-dir",required=True); p.add_argument("--shard-reports-dir"); p.add_argument("--max-report-chars",type=int,default=120000); p.set_defaults(func=build_global_prompt)
    p=sub.add_parser("build-curator-prompt"); p.add_argument("--run-dir",required=True); p.add_argument("--skill-packet",required=True); p.add_argument("--proposal"); p.add_argument("--max-skill-file-chars",type=int,default=50000); p.add_argument("--max-report-chars",type=int,default=120000); p.set_defaults(func=build_curator_prompt)
    p=sub.add_parser("scaffold-skill-packet"); p.add_argument("--out",required=True); p.add_argument("--overwrite",action="store_true"); p.set_defaults(func=scaffold_skill_packet)
    p=sub.add_parser("validate-skill-packet"); p.add_argument("--skill-packet",required=True); p.add_argument("--out"); p.set_defaults(func=validate_skill_packet)
    a=ap.parse_args(); a.func(a); return 0
if __name__=="__main__": raise SystemExit(main())
