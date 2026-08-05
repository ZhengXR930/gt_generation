from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from reachability.core import evaluate_r1_r5
from reachability.engine import (
    extract_assertion_event_checkpoints,
    extract_reachability_checkpoints,
    load_hits,
    load_json,
    run_gdb_reachability,
    write_breakpoint_spec,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Evaluate PoC reachability R1-R4 plus target crash trigger.'
    )
    parser.add_argument('--gt', required=True, type=Path)
    parser.add_argument('--poc', type=Path)
    parser.add_argument('--debug-command', help='Debug command with optional {poc} placeholder.')
    parser.add_argument(
        '--debug-wrapper',
        help=(
            'Run the gdb invocation through this command, passed as one shell '
            'word. Use the sample build.sh for repo-track targets, whose binaries '
            'link against the image glibc and sanitizer runtime and cannot execute '
            'on the host.'
        ),
    )
    parser.add_argument(
        '--debug-path-map',
        help='HOST=CONTAINER prefix rewrite applied to paths handed to the wrapper.',
    )
    parser.add_argument('--sanitizer-command', help='Sanitizer command with optional {poc} placeholder.')
    parser.add_argument('--sanitizer-trace', type=Path, help='Existing sanitizer trace to parse.')
    parser.add_argument('--codebase', type=Path, help='Exact Stage 04 GT source tree containing ASSERT_EVT markers.')
    parser.add_argument('--assertion-spec', type=Path, help='Frozen candidate_assertions.json.')
    parser.add_argument('--assertion-trace', type=Path, help='Frozen vulnerable or fixed ASSERT_EVT trace.')
    parser.add_argument('--verified-invariants', type=Path, help='Selected verified invariant graph used to classify assertion sink events.')
    parser.add_argument('--out-dir', type=Path, default=Path('reachability_out'))
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args()
    if args.assertion_trace and not args.codebase:
        parser.error('--assertion-trace requires --codebase')

    gt = load_json(args.gt)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    breakpoints_path = out_dir / 'reachability_breakpoints.json'
    hits_path = out_dir / 'reachability_hits.json'
    report_path = out_dir / 'reachability_report.json'
    sanitizer_trace_path = out_dir / 'sanitizer_trace.txt'

    checkpoints = extract_reachability_checkpoints(gt)
    assertion_checkpoints = []
    assertion_hits = []
    if args.assertion_trace:
        assertion_spec = load_json(args.assertion_spec) if args.assertion_spec else {}
        verified_invariants = (
            load_json(args.verified_invariants) if args.verified_invariants else {}
        )
        assertion_checkpoints = extract_assertion_event_checkpoints(
            codebase=args.codebase,
            trace_text=args.assertion_trace.read_text(encoding='utf-8', errors='replace'),
            assertion_spec=assertion_spec,
            verified_invariants=verified_invariants,
            sink_location=gt.get('sink') if isinstance(gt.get('sink'), dict) else {},
        )
        checkpoints.extend(assertion_checkpoints)
        # The frozen trace is itself runtime evidence.  Each checkpoint above is
        # created only for an ASSERT_EVT that occurred in the selected case, so a
        # second debugger run is not required to rediscover the same event.
        assertion_hits = [
            {
                'kind': 'assertion_event',
                'event_point': item['event_point'],
                'assertion_role': item['assertion_role'],
            }
            for item in assertion_checkpoints
        ]
    write_breakpoint_spec(checkpoints, breakpoints_path)

    gdb_result = None
    if args.debug_command:
        debug_command = _format_command(args.debug_command, args.poc)
        path_map = None
        if getattr(args, 'debug_path_map', None):
            host, _, guest = str(args.debug_path_map).partition('=')
            if not guest:
                raise SystemExit('--debug-path-map must look like HOST=CONTAINER')
            path_map = (host, guest)
        gdb_result = run_gdb_reachability(
            command=debug_command,
            breakpoints_path=breakpoints_path,
            hits_path=hits_path,
            gdb_script=_stage_gdb_script(out_dir),
            timeout=args.timeout,
            wrapper=getattr(args, 'debug_wrapper', None),
            path_map=path_map,
        )
        (out_dir / 'gdb_stdout.txt').write_text(gdb_result.stdout, encoding='utf-8')
        (out_dir / 'gdb_stderr.txt').write_text(gdb_result.stderr, encoding='utf-8')

    sanitizer_text = ''
    if args.sanitizer_command:
        sanitizer_command = _format_command(args.sanitizer_command, args.poc)
        proc = subprocess.run(
            sanitizer_command,
            shell=True,
            check=False,
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
        sanitizer_trace_path.write_text(sanitizer_text, encoding='utf-8')

    debugger_hits = load_hits(hits_path) if args.debug_command else []
    hits = debugger_hits + assertion_hits if debugger_hits or assertion_hits else None
    if hits is not None:
        hits_path.write_text(
            json.dumps({'hits': hits}, indent=2, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
    report = evaluate_r1_r5(
        gt=gt,
        hits=hits,
        sanitizer_trace=sanitizer_text,
        checkpoints=checkpoints,
    )
    package_root = out_dir.resolve().parent
    artifacts = {
        'breakpoints': _package_path(breakpoints_path, package_root),
    }
    if hits is not None:
        artifacts['hits'] = _package_path(hits_path, package_root)
    if sanitizer_text:
        artifacts['sanitizer_trace'] = _package_path(
            sanitizer_trace_path, package_root
        )
    if args.assertion_spec:
        artifacts['assertion_spec'] = _package_path(
            args.assertion_spec, package_root
        )
    if args.assertion_trace:
        artifacts['assertion_trace'] = _package_path(
            args.assertion_trace, package_root
        )
    report['artifacts'] = artifacts
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


def _stage_gdb_script(out_dir: Path) -> Path:
    """Place the gdb driver beside the outputs.

    A wrapper only sees the result directory, so the script has to live under it
    to be readable from inside the container. Copying keeps the direct path
    unchanged rather than adding a second lookup rule.
    """
    source = Path(__file__).resolve().parent / 'gdb_reachability.py'
    out_dir.mkdir(parents=True, exist_ok=True)
    staged = out_dir / 'gdb_reachability.py'
    staged.write_bytes(source.read_bytes())
    return staged


def _format_command(template: str, poc: Path | None) -> str:
    if poc is None:
        return template
    return template.replace('{poc}', str(poc))


def _package_path(path: Path, package_root: Path) -> str:
    """Return a stable path inside the self-contained sample package."""
    return str(path.resolve().relative_to(package_root))


if __name__ == '__main__':
    main()
