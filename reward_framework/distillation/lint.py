"""Outbound guard for text that is about to enter a skill packet.

The diagnostician and Teacher are allowed to read ground truth: on the training
split that is just label access. What must never happen is ground-truth text
surviving into `SKILL.md`, because that file is an *input to the agent at test
time*. This module is the single checkpoint between the two.

Two independent rules run on every proposal:

- structural regexes reject source-file names, ``file:line`` anchors, hex
  constants, long literals, and sample ids outright: a transferable lesson has
  no business naming any of them;
- a literal index built from the local ``gt_results`` corpus rejects
  ground-truth function names, project ids, and code fragments.

Evidence never reaches this module, because evidence never reaches the packet.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
GT_RESULTS = REPO_ROOT / "gt_results"
DEFAULT_INDEX = REPO_ROOT / "dataset" / "gt_literal_index.json"

MIN_IDENTIFIER = 6
MIN_SNIPPET = 24

_SOURCE_FILE_RE = re.compile(
    r"\b[\w./-]+\.(?:c|h|cc|cpp|cxx|hpp|hh|inc|py|rs|go|java|m|mm)\b", re.IGNORECASE
)
_FILE_LINE_RE = re.compile(r"[\w/.-]+\s*[:#]\s*\d+")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]{3,}\b")
_LONG_NUMBER_RE = re.compile(r"\b\d{4,}\b")
_SAMPLE_ID_RE = re.compile(r"\b(?:arvo|nvd|osv|secbench)[_-][A-Za-z0-9._-]+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_SPACE_RE = re.compile(r"\s+")

_RULES = (
    (_SAMPLE_ID_RE, "names a benchmark sample id"),
    (_SOURCE_FILE_RE, "names a source file"),
    (_FILE_LINE_RE, "carries a file:line anchor"),
    (_HEX_RE, "carries a concrete hex constant"),
    (_LONG_NUMBER_RE, "carries a concrete numeric literal"),
)


def _is_codey(name: str) -> bool:
    """Reject dictionary words; keep identifiers that look like real symbols."""
    if "_" in name or any(ch.isdigit() for ch in name):
        return True
    if any(ch.isupper() for ch in name[1:]) and any(ch.islower() for ch in name):
        return True
    return len(name) >= 12


def _norm(text: str) -> str:
    return _SPACE_RE.sub(" ", str(text or "")).strip().lower()


def _add_identifier(bucket: set, value: Any) -> None:
    name = str(value or "").strip()
    if len(name) >= MIN_IDENTIFIER and _is_codey(name):
        bucket.add(name.lower())


def _add_snippet(bucket: set, value: Any) -> None:
    snippet = _norm(value)
    if len(snippet) >= MIN_SNIPPET:
        bucket.add(snippet)


def build_literal_index(gt_results: Path = GT_RESULTS) -> dict[str, list[str]]:
    """Collect ground-truth identifiers and code fragments from the local corpus."""
    identifiers: set = set()
    snippets: set = set()
    if not gt_results.is_dir():
        return {"identifiers": [], "snippets": []}
    for sample_dir in sorted(gt_results.iterdir()):
        path = sample_dir / "ground_truth.json"
        if not path.is_file():
            continue
        try:
            gt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        project = gt.get("project")
        if isinstance(project, dict):
            _add_identifier(identifiers, project.get("id"))
        for step in gt.get("fine_trace") or []:
            if not isinstance(step, dict):
                continue
            _add_identifier(identifiers, step.get("function"))
            _add_identifier(identifiers, Path(str(step.get("file") or "")).stem)
            _add_snippet(snippets, step.get("code"))
        for key in ("source", "root_cause", "sink"):
            anchor = gt.get(key)
            if not isinstance(anchor, dict):
                continue
            _add_identifier(identifiers, anchor.get("function"))
            _add_identifier(identifiers, Path(str(anchor.get("file") or "")).stem)
            for operand in anchor.get("operands") or []:
                _add_identifier(identifiers, operand)
    return {"identifiers": sorted(identifiers), "snippets": sorted(snippets)}


def load_literal_index(cache: Path = DEFAULT_INDEX, *, rebuild: bool = False) -> dict[str, Any]:
    if not rebuild and cache.is_file():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and "identifiers" in payload:
                return {"identifiers": set(payload["identifiers"]), "snippets": set(payload.get("snippets") or [])}
        except (OSError, ValueError):
            pass
    index = build_literal_index()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(index, indent=0, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"identifiers": set(index["identifiers"]), "snippets": set(index["snippets"])}


def lint_lesson_text(text: str, index: dict[str, Any] | None = None) -> list[str]:
    """Return the reasons this text must not enter a skill packet (empty = clean)."""
    value = str(text or "")
    reasons: list[str] = []
    for pattern, reason in _RULES:
        match = pattern.search(value)
        if match:
            reasons.append("{}: {!r}".format(reason, match.group(0)[:60]))
    if index:
        identifiers = index.get("identifiers") or set()
        snippets = index.get("snippets") or set()
        seen = {token.lower() for token in _TOKEN_RE.findall(value)}
        hits = sorted(seen & identifiers)
        if hits:
            reasons.append("reuses ground-truth identifiers: {}".format(", ".join(hits[:5])))
        normalized = _norm(value)
        for snippet in snippets:
            if snippet in normalized:
                reasons.append("quotes ground-truth code: {!r}".format(snippet[:60]))
                break
    return reasons
