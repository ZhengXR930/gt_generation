from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import reachability_core  # shared engine package (for gdb_reachability.py location)
from reachability_core.core import (
    extract_reachability_checkpoints,
    load_hits,
    load_json,
    run_gdb_reachability,
    write_breakpoint_spec,
)

from reachability_eval.core import evaluate_r1_r5


def create_server(default_out_dir: Path) -> FastMCP:
    server = FastMCP(
        'vulnerability-reachability-recorder',
        instructions=(
            'Evaluator-side tool for measuring PoC reachability R1-R5. '
            'Do not expose this server to the tested agent in baseline runs.'
        ),
    )

    @server.tool(
        name='evaluate_poc_reachability',
        description=(
            'Run R1-R5 reachability checks for a submitted PoC using GT checkpoints, '
            'GDB line/function breakpoints, and optional sanitizer trace matching.'
        ),
    )
    def evaluate_poc_reachability(
        gt_path: str,
        poc_path: str = '',
        debug_command: str = '',
        sanitizer_command: str = '',
        sanitizer_trace_path: str = '',
        out_dir: str = '',
        timeout: int = 120,
    ) -> dict:
        gt = load_json(Path(gt_path))
        output_dir = Path(out_dir) if out_dir else default_out_dir / str(gt.get('sample_id') or 'sample')
        output_dir.mkdir(parents=True, exist_ok=True)
        breakpoints_path = output_dir / 'reachability_breakpoints.json'
        hits_path = output_dir / 'reachability_hits.json'
        sanitizer_out = output_dir / 'sanitizer_trace.txt'

        checkpoints = extract_reachability_checkpoints(gt)
        write_breakpoint_spec(checkpoints, breakpoints_path)

        gdb_result = None
        if debug_command:
            command = _format_command(debug_command, poc_path)
            gdb_result = run_gdb_reachability(
                command=command,
                breakpoints_path=breakpoints_path,
                hits_path=hits_path,
                gdb_script=Path(reachability_core.__file__).resolve().parent / 'gdb_reachability.py',
                timeout=timeout,
            )
            (output_dir / 'gdb_stdout.txt').write_text(gdb_result.stdout, encoding='utf-8')
            (output_dir / 'gdb_stderr.txt').write_text(gdb_result.stderr, encoding='utf-8')

        sanitizer_text = ''
        if sanitizer_command:
            command = _format_command(sanitizer_command, poc_path)
            proc = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            sanitizer_text = proc.stdout + '\n' + proc.stderr
            sanitizer_out.write_text(sanitizer_text, encoding='utf-8')
        elif sanitizer_trace_path:
            sanitizer_text = Path(sanitizer_trace_path).read_text(
                encoding='utf-8', errors='replace'
            )

        report = evaluate_r1_r5(
            gt=gt,
            hits=load_hits(hits_path) if debug_command else None,
            sanitizer_trace=sanitizer_text,
        )
        report['artifacts'] = {
            'breakpoints': str(breakpoints_path),
            'hits': str(hits_path),
            'sanitizer_trace': str(sanitizer_out if sanitizer_text else ''),
            'out_dir': str(output_dir),
        }
        if gdb_result is not None:
            report['debug_command'] = {
                'command': gdb_result.command,
                'returncode': gdb_result.returncode,
            }
        (output_dir / 'reachability_report.json').write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
        return report

    return server


def _format_command(template: str, poc_path: str) -> str:
    return template.replace('{poc}', poc_path) if poc_path else template


def main() -> None:
    parser = argparse.ArgumentParser(description='Run reachability recorder MCP server.')
    parser.add_argument('--host', default=os.environ.get('REACHABILITY_MCP_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('REACHABILITY_MCP_PORT', '9012')))
    parser.add_argument('--out-dir', type=Path, default=Path(os.environ.get('REACHABILITY_OUT_DIR', 'reachability_runs')))
    args = parser.parse_args()
    server = create_server(args.out_dir)
    server.settings.host = args.host
    server.settings.port = args.port
    server.run('sse')


if __name__ == '__main__':
    main()
