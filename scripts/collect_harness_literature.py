#!/usr/bin/env python3
"""Collect a traceable paper table for the vulnerability-reasoning harness idea."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from pathlib import Path
from typing import Any


FIELDS = "title,year,authors,venue,url,abstract,citationCount,externalIds"
DEFAULT_QUERIES = [
    "AI agents cybersecurity benchmark vulnerability exploit generation",
    "large language model agents vulnerability repair benchmark",
    "large language model software vulnerability detection repair reasoning",
    "automated program repair large language models benchmark security",
    "LLM automated program repair security vulnerability benchmark",
    "LLM software vulnerability detection benchmark dataset",
    "LLM code agent benchmark trajectory evaluation software engineering",
    "AI agent cyber range benchmark vulnerability",
    "SWE-bench software engineering agents benchmark OpenHands",
    "repository level code agent benchmark software engineering",
    "memory safety vulnerability benchmark fuzzing proof of concept",
    "sanitizer grounded vulnerability dataset exploit proof of concept",
    "LLM vulnerability reasoning source sink data flow benchmark",
    "cybersecurity agent harness benchmark trajectory evaluation",
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--target", type=int, default=80)
    ap.add_argument("--min-year", type=int, default=2020)
    ap.add_argument("--sleep", type=float, default=1.2)
    ap.add_argument("--provider", choices=["semanticscholar", "openalex"], default="semanticscholar")
    ap.add_argument("--query", action="append", help="Additional Semantic Scholar query")
    args = ap.parse_args()

    queries = DEFAULT_QUERIES + (args.query or [])
    papers: dict[str, dict[str, Any]] = {}
    for query in queries:
        for paper in search(query, limit=25, provider=args.provider, min_year=args.min_year):
            year = paper.get("year")
            if isinstance(year, int) and year < args.min_year:
                continue
            if not is_relevant(paper):
                continue
            key = paper_key(paper)
            if not key:
                continue
            paper.setdefault("matched_queries", [])
            paper["matched_queries"].append(query)
            if key in papers:
                papers[key]["matched_queries"] = sorted(set(papers[key].get("matched_queries", []) + [query]))
                continue
            papers[key] = paper
            if len(papers) >= args.target:
                break
        if len(papers) >= args.target:
            break
        time.sleep(args.sleep)

    ranked = sorted(
        papers.values(),
        key=lambda p: (int(p.get("year") or 0), int(p.get("citationCount") or 0)),
        reverse=True,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "papers.json").write_text(json.dumps(ranked, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (args.out_dir / "PAPER_TABLE.md").write_text(render_markdown(ranked), encoding="utf-8")
    (args.out_dir / "LITERATURE_LANDSCAPE_SEED.md").write_text(render_seed_notes(ranked, queries), encoding="utf-8")
    print(json.dumps({"paper_count": len(ranked), "out_dir": str(args.out_dir)}, indent=2))


def search(query: str, *, limit: int, provider: str, min_year: int) -> list[dict[str, Any]]:
    if provider == "openalex":
        return search_openalex(query, limit=limit, min_year=min_year)
    return search_semanticscholar(query, limit=limit)


def search_semanticscholar(query: str, *, limit: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"query": query, "limit": limit, "fields": FIELDS})
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "gt-generation-literature/0.1"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("data") or []
        except HTTPError as exc:
            if exc.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            print(f"warning: query failed ({exc.code}) {query}")
            return []
        except URLError as exc:
            print(f"warning: query failed ({exc}) {query}")
            time.sleep(3 * (attempt + 1))
    print(f"warning: query skipped after retries: {query}")
    return []


def search_openalex(query: str, *, limit: int, min_year: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "search": query,
            "filter": f"from_publication_date:{min_year}-01-01",
            "per-page": limit,
            "mailto": "codex@example.com",
        }
    )
    url = f"https://api.openalex.org/works?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "gt-generation-literature/0.1"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return [_openalex_to_paper(item, query) for item in payload.get("results") or []]
        except (HTTPError, URLError) as exc:
            print(f"warning: OpenAlex query failed ({exc}) {query}")
            time.sleep(3 * (attempt + 1))
    return []


def _openalex_to_paper(item: dict[str, Any], query: str) -> dict[str, Any]:
    primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    source = primary.get("source") if isinstance(primary.get("source"), dict) else {}
    authorships = item.get("authorships") if isinstance(item.get("authorships"), list) else []
    authors = []
    for authorship in authorships[:12]:
        author = authorship.get("author") if isinstance(authorship, dict) else {}
        if isinstance(author, dict) and author.get("display_name"):
            authors.append({"name": author["display_name"]})
    abstract = _invert_abstract(item.get("abstract_inverted_index"))
    return {
        "title": item.get("display_name"),
        "year": item.get("publication_year"),
        "authors": authors,
        "venue": source.get("display_name") or item.get("type_crossref") or item.get("type"),
        "url": item.get("doi") or item.get("id"),
        "abstract": abstract,
        "citationCount": item.get("cited_by_count") or 0,
        "externalIds": {"OpenAlex": item.get("id"), "DOI": item.get("doi")},
        "matched_queries": [query],
    }


def _invert_abstract(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positions = []
    for word, locs in index.items():
        if isinstance(locs, list):
            for pos in locs:
                if isinstance(pos, int):
                    positions.append((pos, word))
    return " ".join(word for _, word in sorted(positions))[:2500]


def paper_key(paper: dict[str, Any]) -> str | None:
    title = (paper.get("title") or "").strip().lower()
    if title:
        normalized = " ".join(title.split())
        return f"title:{normalized}"
    external = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
    for name in ("DOI", "ArXiv", "CorpusId"):
        val = external.get(name)
        if val:
            return f"{name}:{val}".lower()
    return None


def is_relevant(paper: dict[str, Any]) -> bool:
    title = str(paper.get("title") or "").strip().lower()
    if not title or title.startswith("title pending"):
        return False
    if not (paper.get("url") or external_link(paper)):
        return False
    text = f"{title} {paper.get('abstract') or ''}".lower()
    security_terms = [
        "vulnerab",
        "security",
        "secure code",
        "cyber",
        "exploit",
        "memory safety",
        "bug",
        "program repair",
        "software engineering",
        "code generation",
        "fuzz",
    ]
    agent_eval_terms = [
        "large language model",
        "llm",
        "agent",
        "benchmark",
        "evaluation",
        "dataset",
        "harness",
        "automated",
        "repair",
    ]
    return any(term in text for term in security_terms) and any(term in text for term in agent_eval_terms)


def render_markdown(papers: list[dict[str, Any]]) -> str:
    lines = [
        "# Paper Table",
        "",
        "| ID | Title | Year | Venue | Citations | Link | Relevance Seed |",
        "|---|---|---:|---|---:|---|---|",
    ]
    for idx, paper in enumerate(papers, 1):
        pid = f"R{idx:03d}"
        title = escape_md(paper.get("title") or "")
        year = paper.get("year") or ""
        venue = escape_md(paper.get("venue") or "")
        cites = paper.get("citationCount") or 0
        link = paper.get("url") or external_link(paper)
        q = "; ".join(paper.get("matched_queries", [])[:2])
        lines.append(f"| {pid} | {title} | {year} | {venue} | {cites} | {link or ''} | {escape_md(q)} |")
    lines.append("")
    return "\n".join(lines)


def render_seed_notes(papers: list[dict[str, Any]], queries: list[str]) -> str:
    return "\n".join(
        [
            "# Literature Landscape Seed",
            "",
            "This file is a machine-collected seed for the harness feasibility study. It is not the final synthesis.",
            "",
            "## Query Scope",
            "",
            *[f"- {q}" for q in queries],
            "",
            "## Next Synthesis Questions",
            "",
            "- Which benchmarks evaluate final success only, and which preserve trajectories or reasoning artifacts?",
            "- Which datasets provide executable PoCs, patches, sanitizer traces, or source-sink/root-cause annotations?",
            "- Where can our T1-T5 harness add diagnostic value over pass/fail exploit generation?",
            "- What claims are supportable from 50 CyberGym samples versus what needs a larger study?",
            "",
            f"Collected papers: {len(papers)}",
            "",
        ]
    )


def external_link(paper: dict[str, Any]) -> str:
    external = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
    if external.get("ArXiv"):
        return f"https://arxiv.org/abs/{external['ArXiv']}"
    if external.get("DOI"):
        return f"https://doi.org/{external['DOI']}"
    return ""


def escape_md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


if __name__ == "__main__":
    main()
