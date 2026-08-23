"""DeepSeek Harness telemetry helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def summarize_session_log(session_log: Path) -> dict[str, Any]:
    """Summarize a DSH session/event log if exported as JSONL."""
    events: list[dict] = []
    if session_log.is_file():
        for line in session_log.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    submit_like = [
        e for e in events
        if "submit" in json.dumps(e, ensure_ascii=False).lower()
        or "analysis.json" in json.dumps(e, ensure_ascii=False).lower()
    ]
    return {
        "adapter": "deepseek_harness",
        "session_log": str(session_log),
        "events": len(events),
        "submit_like_events": len(submit_like),
    }
