from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from recorder_core import append_reasoning_record, reduce_records
from recorder_core.core import load_reasoning_events


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
            'Record one structured snapshot of the current vulnerability understanding. '
            'Do not call with all arrays empty. Do not use LLVMFuzzerTestOneInput/Data '
            'as the source; use the project parser/load statement where attacker bytes '
            'become vulnerability-relevant state. After a complete snapshot exists, '
            'do not restate the same understanding; continue with candidate construction '
            'or use stage=revision for a complete updated snapshot.'
        ),
    )
    def record_vulnerability_state(
        stage: str = 'partial',
        confidence: str = 'low',
        note: str = '',
        sources: list[dict[str, Any]] | None = None,
        root_causes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
        sinks: list[dict[str, Any]] | None = None,
        open_questions: list[str] | None = None,
    ) -> dict[str, Any]:
        raw_record = {
            'kind': 'vulnerability_state',
            'status': 'confirmed',
            'stage': stage,
            'confidence': confidence,
            'text': note,
            'sources': sources or [],
            'root_causes': root_causes or [],
            'edges': edges or [],
            'sinks': sinks or [],
            'open_questions': open_questions or [],
        }
        result = append_reasoning_record(
            raw_record,
            events_path=events_path,
            state_path=state_path,
            accepted_only=_enhance_mode(),
            strict=_enhance_mode(),
        )
        return {
            'accepted': result['accepted'],
            'content': result['content'],
            'event_id': result['record'].get('event_id'),
            'errors': result['errors'],
            'warnings': result['warnings'],
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
