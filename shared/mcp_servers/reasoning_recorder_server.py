from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from recorder_core import append_reasoning_record, reduce_records
from recorder_core.core import load_reasoning_events, role_group, VALID_EDGE_TYPES


def _default_events_path() -> Path:
    return Path(os.environ.get('RECORDER_EVENTS_PATH', 'reasoning_events.jsonl'))


def _default_state_path() -> Path:
    return Path(os.environ.get('RECORDER_STATE_PATH', 'reasoning_state.json'))


def _enhance_mode() -> bool:
    return os.environ.get('OPENHANDS_HARNESS_MODE', 'evaluation') == 'enhance'


def create_server(events_path: Path, state_path: Path) -> FastMCP:
    server = FastMCP(
        'vulnerability-reasoning-recorder',
        instructions=(
            'Records explicit vulnerability reasoning commitments for source, '
            'sink, propagation edges, and root cause. This server validates and '
            'persists structured records; it does not judge correctness.'
        ),
    )

    @server.tool(
        name='record_vulnerability_state',
        description=(
            'Record one structured snapshot of the current vulnerability understanding as '
            'a REASONING TRACE: `nodes` is the ordered list of trace points, each with a '
            '`role` (source, tainted_read, tainted_value_materialization, dispatch, alloc, '
            'free, root_cause, sink, ...) plus file/function/line/var/code/text; `edges` '
            'connect them. Record the intermediate points (allocation, free, size/index '
            'computation, the missing check), not only source/sink/root_cause — the more '
            'specific the role, the better it matches. (`sources`/`root_causes`/`sinks` are '
            'still accepted for back-compat.) '
            'Do not call with everything empty. Do not use LLVMFuzzerTestOneInput/Data '
            'as the source; use the project parser/load statement where attacker bytes '
            'become vulnerability-relevant state. '
            'EDGES — every edge MUST be {"from","to","type",...} where `type` is EXACTLY one '
            'of "data" | "control" | "order". An edge with NO `type` (only a text/relation '
            'description) is DISCARDED and scores ZERO — a prose description is NOT enough, '
            'the type field is mandatory on every single edge. '
            '"data" = value provenance (e.g. {"from":"len","to":"buf","type":"data"}); '
            '"control" = the guard/branch/dispatch or the MISSING check that makes the sink '
            'reachable (e.g. {"from":"count","to":"out","type":"control","relation":"missing bounds check"}); '
            '"order" = temporal happens-before for lifetime bugs '
            '(e.g. {"from":"ptr","to":"ptr","type":"order","relation":"free_before_use"}). '
            'Set from/to to the variables the edge is about (for control, the variables in '
            'the guard/missing check e.g. out,count,end; for order, the freed/used pointer) '
            'so the edge is matchable. A UAF/uninit needs at least one control OR order edge — '
            'pure data edges are not enough; and NEVER omit `type`. After a complete snapshot '
            'exists, do not restate the same understanding; continue with candidate '
            'construction or use stage=revision for a complete updated snapshot.'
        ),
    )
    def record_vulnerability_state(
        stage: str = 'partial',
        confidence: str = 'low',
        note: str = '',
        nodes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
        open_questions: list[str] | None = None,
    ) -> dict[str, Any]:
        # `nodes` is the reasoning trace (each with a `role`); route each node to its
        # source / root_cause / sink family so the snapshot machinery stores it.
        src: list[dict[str, Any]] = []
        rc: list[dict[str, Any]] = []
        snk: list[dict[str, Any]] = []
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            grp = role_group(node.get('role'))
            (snk if grp == 'sinks' else rc if grp == 'root_causes' else src).append(node)
        raw_record = {
            'kind': 'vulnerability_state',
            'status': 'confirmed',
            'stage': stage,
            'confidence': confidence,
            'text': note,
            'sources': src,
            'root_causes': rc,
            'edges': edges or [],
            'sinks': snk,
            'open_questions': open_questions or [],
        }
        result = append_reasoning_record(
            raw_record,
            events_path=events_path,
            state_path=state_path,
            accepted_only=_enhance_mode(),
            strict=_enhance_mode(),
        )
        # LOUD, actionable feedback: any edge without a valid type is dropped and scores 0.
        untyped = [e for e in (edges or []) if isinstance(e, dict)
                   and str(e.get('type') or '').strip().lower() not in VALID_EDGE_TYPES]
        warnings = list(result['warnings'])
        if untyped:
            warnings.insert(0, (
                f'{len(untyped)} edge(s) DROPPED (score 0): missing a valid `type` '
                f'(must be data/control/order). Re-record them with the `type` field set — '
                f'e.g. {{"from":"{untyped[0].get("from","x")}","to":"{untyped[0].get("to","y")}","type":"data|control|order"}}.'
            ))
        return {
            'accepted': result['accepted'],
            'content': result['content'],
            'event_id': result['record'].get('event_id'),
            'errors': result['errors'],
            'warnings': warnings,
            'missing_fields': result['missing_fields'],
            'next_missing': result['next_missing'],
            'next_tools': result['next_tools'],
            'state': result['state'],
        }

    @server.tool(
        name='read_reasoning_state',
        description='Read the current reduced reasoning state.',
    )
    def read_reasoning_state() -> dict[str, Any]:
        if state_path.exists():
            try:
                state = json.loads(
                    state_path.read_text(encoding='utf-8', errors='replace')
                )
            except json.JSONDecodeError:
                state = {}
        else:
            state = reduce_records(load_reasoning_events(events_path))
        return {'state': state}

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description='Run the reasoning recorder MCP server.')
    parser.add_argument('--host', default=os.environ.get('RECORDER_MCP_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('RECORDER_MCP_PORT', '9001')))
    parser.add_argument('--events-path', type=Path, default=_default_events_path())
    parser.add_argument('--state-path', type=Path, default=_default_state_path())
    args = parser.parse_args()

    args.events_path.parent.mkdir(parents=True, exist_ok=True)
    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    server = create_server(args.events_path, args.state_path)
    server.settings.host = args.host
    server.settings.port = args.port
    server.run('sse')


if __name__ == '__main__':
    main()
