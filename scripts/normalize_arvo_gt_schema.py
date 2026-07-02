#!/usr/bin/env python3
from __future__ import annotations

import argparse, datetime as dt, json, os, subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# gt_toolkit lives under gt_generation/; put it on PYTHONPATH so `-m gt_toolkit` resolves.
os.environ["PYTHONPATH"] = str(ROOT / "gt_generation") + os.pathsep + os.environ.get("PYTHONPATH", "")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def find_record(selection: list[dict[str, Any]], sid: str) -> dict[str, Any]:
    for r in selection:
        if r.get('local_sample_id') == sid or r.get('sample_id') == sid:
            return r
    return {}


def description_from(src: dict[str, Any], record: dict[str, Any], sid: str) -> dict[str, str]:
    existing = src.get('bug_description') if isinstance(src.get('bug_description'), dict) else {}
    original = (
        existing.get('original')
        or src.get('original_bug_description')
        or record.get('original_bug_description')
        or src.get('evidence')
        or record.get('evidence')
        or src.get('normalized_bug_description')
        or record.get('normalized_bug_description')
        or f'OSS-Fuzz / ARVO sample {sid} memory-safety issue.'
    )
    normalized = (
        existing.get('normalized')
        or src.get('normalized_bug_description')
        or record.get('normalized_bug_description')
        or str(original)
    )
    original_source = (
        existing.get('original_source')
        or src.get('original_source')
        or record.get('original_source')
        or 'ARVO-Meta / OSS-Fuzz issue metadata'
    )
    return {
        'original': str(original).strip(),
        'original_source': str(original_source).strip(),
        'normalized': str(normalized).strip(),
    }


def infer_poc_format(
    src: dict[str, Any],
    trigger: dict[str, Any],
    poc: dict[str, Any],
) -> dict[str, Any]:
    target_binary = str(trigger.get('target_binary') or '')
    command = str(
        poc.get('trigger')
        or poc.get('command')
        or trigger.get('target_command')
        or trigger.get('command')
        or ''
    )
    text = ' '.join(
        str(value)
        for value in [
            src.get('normalized_bug_description'),
            src.get('original_bug_description'),
            command,
            target_binary,
        ]
        if value
    ).lower()
    hints = [
        ('pkcs12', 'PKCS#12 DER'),
        ('pdf', 'PDF'),
        ('tga', 'TGA image'),
        ('png', 'PNG image'),
        ('jpeg', 'JPEG image'),
        ('jpg', 'JPEG image'),
        ('bmp', 'BMP image'),
        ('zip', 'ZIP archive'),
        ('xml', 'XML document'),
        ('json', 'JSON document'),
        ('html', 'HTML document'),
        ('javascript', 'JavaScript source'),
        ('js', 'JavaScript source'),
        ('mp4', 'MP4 media container'),
        ('pcap', 'pcap capture/filter input'),
    ]
    format_name = 'project-specific fuzzer input'
    for needle, label in hints:
        if needle in text:
            format_name = label
            break
    component = Path(target_binary).name or 'target parser input'
    return {
        'name': format_name,
        'contract': (
            f"Single PoC file at {trigger.get('container_poc_path') or '/tmp/poc'} "
            f'is consumed by {component}. A candidate should be accepted far '
            'enough by the target harness to reach the annotated '
            'source/root-cause/sink chain.'
        ),
    }


def normalize_poc(src: dict[str, Any], trigger: dict[str, Any], gt: dict[str, Any]) -> bool:
    poc = gt.get('poc')
    if not isinstance(poc, dict):
        gt['poc'] = {}
        poc = gt['poc']
    changed = False
    if not str(poc.get('path') or '').strip():
        poc['path'] = (
            trigger.get('local_poc_path')
            or poc.get('artifact')
            or 'poc'
        )
        changed = True
    if not str(poc.get('trigger') or '').strip():
        poc['trigger'] = (
            poc.get('command')
            or trigger.get('target_command')
            or trigger.get('command')
            or ''
        )
        changed = True
    inferred_format = infer_poc_format(src, trigger, poc)
    if not isinstance(poc.get('format'), dict):
        poc['format'] = {}
        changed = True
    for key, value in inferred_format.items():
        if key not in poc['format'] or poc['format'].get(key) in ('', None, []):
            poc['format'][key] = value
            changed = True
    return changed


def run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout


def normalize_sample(sid: str, record: dict[str, Any], results: Path) -> dict[str, Any]:
    d = results / sid
    gt_path = d / 'ground_truth.json'
    if not gt_path.exists():
        return {'sample_id': sid, 'status': 'missing_gt'}
    gt = load_json(gt_path, {})
    src = load_json(d / 'source_sample.json', {}) or {}
    trigger = load_json(d / 'trigger.json', {}) or {}

    changed = False
    if 'watchpoint_trace' in gt:
        gt.pop('watchpoint_trace', None)
        changed = True
    if not isinstance(gt.get('bug_description'), dict) or not all(str(gt.get('bug_description', {}).get(k, '')).strip() for k in ['original','original_source','normalized']):
        # Keep bug_description near project/classification for readability.
        bug = description_from(src, record, sid)
        rebuilt = {}
        inserted = False
        for k, v in gt.items():
            rebuilt[k] = v
            if k == 'project':
                rebuilt['bug_description'] = bug
                inserted = True
        if not inserted:
            rebuilt['bug_description'] = bug
        gt = rebuilt
        changed = True
    if normalize_poc(src, trigger, gt):
        changed = True
    if changed:
        write_json(gt_path, gt)

    # Recompute deterministic grounding after any schema migration.
    patch = d / 'patch.diff'
    if patch.exists():
        code, out = run(['python3', 'scripts/compute_grounding.py', str(gt_path), '--patch', str(patch), '--in-place'])
        if code != 0:
            return {'sample_id': sid, 'status': 'grounding_failed', 'output': out[-1000:]}

    code, out = run(['python3', '-m', 'gt_toolkit', 'validate', str(gt_path)])
    schema = json.loads(out) if out.strip().startswith('{') else {'ok': False, 'errors': [out]}

    state_path = d / 'sample_state.json'
    state = load_json(state_path, {}) or {}
    state.setdefault('sample_id', sid)
    state.setdefault('artifacts', {})
    state['artifacts'].update({
        'ground_truth': 'ground_truth.json',
        'build_script': 'build.sh',
        'sanitizer_trace': 'sanitizer_trace.txt',
        'patch_diff': 'patch.diff',
        'generation_log': 'generation.log',
    })
    state.setdefault('validation', {})
    state['validation']['schema_valid'] = bool(schema.get('ok'))
    state['validation']['schema_errors'] = schema.get('errors', [])
    if schema.get('ok'):
        state['status'] = 'gt_completed_schema_passed'
        state['current_stage'] = 'completed'
        state['failure'] = None
        state.setdefault('cleanup', {})
        state['cleanup'].update({'source_deleted': True, 'build_deleted': True})
    state['updated_at'] = now()
    write_json(state_path, state)
    with (d / 'generation.log').open('a', encoding='utf-8') as fh:
        fh.write(f"{now()} schema_migration status={'ok' if schema.get('ok') else 'failed'}\n")
    return {'sample_id': sid, 'status': 'ok' if schema.get('ok') else 'schema_failed', 'errors': schema.get('errors', [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--selection', type=Path, default=ROOT / 'selected_samples_json/cybergym_overlap_50_gt_validated.json')
    ap.add_argument('--results', type=Path, default=ROOT / 'gt_results')
    ap.add_argument('--output', type=Path, default=ROOT / 'gt_results/schema_migration_report.json')
    args = ap.parse_args()
    records = load_json(args.selection, [])
    report = []
    for r in records:
        sid = r.get('local_sample_id') or r.get('sample_id')
        if not sid:
            continue
        item = normalize_sample(sid, r, args.results)
        print(item)
        report.append(item)
    write_json(args.output, {'selection': str(args.selection), 'count': len(report), 'items': report})

if __name__ == '__main__':
    main()
