from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import reachability_core  # shared engine package (for gdb_reachability.py location)
from reachability_core.core import (
    extract_reachability_checkpoints,
    load_hits,
    load_json,
    run_gdb_reachability,
    write_breakpoint_spec,
)

from reachability_eval.core import evaluate_r1_r5


def main() -> None:
    parser = argparse.ArgumentParser(description='Evaluate PoC reachability R1-R5.')
    parser.add_argument('--gt', required=True, type=Path)
    parser.add_argument('--poc', type=Path)
    parser.add_argument('--debug-command', help='Debug command with optional {poc} placeholder.')
    parser.add_argument('--sanitizer-command', help='Sanitizer command with optional {poc} placeholder.')
    parser.add_argument('--sanitizer-trace', type=Path, help='Existing sanitizer trace to parse.')
    parser.add_argument('--out-dir', type=Path, default=Path('reachability_out'))
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args()

    gt = load_json(args.gt)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    breakpoints_path = out_dir / 'reachability_breakpoints.json'
    hits_path = out_dir / 'reachability_hits.json'
    report_path = out_dir / 'reachability_report.json'
    sanitizer_trace_path = out_dir / 'sanitizer_trace.txt'

    checkpoints = extract_reachability_checkpoints(gt)
    write_breakpoint_spec(checkpoints, breakpoints_path)

    gdb_result = None
    if args.debug_command:
        debug_command = _format_command(args.debug_command, args.poc)
        gdb_result = run_gdb_reachability(
            command=debug_command,
            breakpoints_path=breakpoints_path,
            hits_path=hits_path,
            gdb_script=Path(reachability_core.__file__).resolve().parent / 'gdb_reachability.py',
            timeout=args.timeout,
        )
        (out_dir / 'gdb_stdout.txt').write_text(gdb_result.stdout, encoding='utf-8')
        (out_dir / 'gdb_stderr.txt').write_text(gdb_result.stderr, encoding='utf-8')

    sanitizer_text = ''
    if args.sanitizer_command:
        sanitizer_command = _format_command(args.sanitizer_command, args.poc)
        proc = subprocess.run(
            sanitizer_command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=args.timeout,
        )
        sanitizer_text = proc.stdout + '\n' + proc.stderr
        sanitizer_trace_path.write_text(sanitizer_text, encoding='utf-8')
    elif args.sanitizer_trace:
        sanitizer_text = args.sanitizer_trace.read_text(
            encoding='utf-8', errors='replace'
        )

    hits = load_hits(hits_path) if args.debug_command else None
    report = evaluate_r1_r5(gt=gt, hits=hits, sanitizer_trace=sanitizer_text)
    report['artifacts'] = {
        'breakpoints': str(breakpoints_path),
        'hits': str(hits_path),
        'sanitizer_trace': str(sanitizer_trace_path if sanitizer_text else ''),
    }
    if gdb_result is not None:
        report['debug_command'] = {
            'command': gdb_result.command,
            'returncode': gdb_result.returncode,
        }
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


def _format_command(template: str, poc: Path | None) -> str:
    if poc is None:
        return template
    return template.replace('{poc}', str(poc))


if __name__ == '__main__':
    main()
