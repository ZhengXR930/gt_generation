#!/usr/bin/env python3
"""Project the portable roles/ into installable Claude Code skills.

This is an L3 adapter: it does NOT own any content. It reads the single source
of truth in roles/*.md and emits <out>/<name>/SKILL.md with the frontmatter the
Claude Code skill loader expects. The same roles drive Codex/other CLIs via
runner.py, so content is written once and projected per-CLI.

Usage:
  python3 adapters/claude_code/build_skills.py            # -> adapters/claude_code/skills/
  python3 adapters/claude_code/build_skills.py --out ~/.claude/skills
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLES_DIR = REPO_ROOT / "roles"

# Role file stem -> (skill name, one-line description trigger).
SKILLS = {
    "00_materialize": ("gt-materialize", "Resolve one vulnerability id into a self-contained sample workspace (repo, commit, PoC, patch.diff, sample_state.json) before GT generation."),
    "01_reproducer": ("gt-reproduce", "Build sanitizer/debug variants in Docker and reproduce a memory-safety crash, capturing sanitizer_trace.txt and valgrind_trace.txt."),
    "02_gt_generator": ("gt-generate", "Author fine-grained ground_truth.json (source, sink, root cause, coarse/fine trace, sanitizer_ground_truth, poc) for a reproduced memory-safety bug."),
    "03_source_auditor": ("gt-source-audit", "Audit the source/tainted-value-origin/parser-admitted parts of a ground_truth.json and emit source_review.json."),
    "04_semantic_reviewer": ("gt-semantic-review", "Audit whether a ground_truth.json faithfully represents the vulnerability logic and emit semantic_review.json."),
    "05_runtime_validator": ("gt-runtime-validate", "Validate GT against runtime artifacts via gt-toolkit reachability (R1-R5) and gdb watchpoints, emitting reachability_report.json."),
}

TOOLKIT_NOTE = (
    "\n\n## Toolkit\n\n"
    "All deterministic checks are provided by the portable `gt_toolkit` package. "
    "Invoke it without installation from the repo root:\n\n"
    "```bash\n"
    "python3 -m gt_toolkit validate <ground_truth.json>\n"
    "python3 -m gt_toolkit state init --sample-id <id> --output <dir>/sample_state.json\n"
    "python3 -m gt_toolkit reachability --gt <dir>/ground_truth.json --poc <poc> ...\n"
    "python3 -m gt_toolkit gdb-watch --binary <debug_bin> --watch <expr> --break <file:line>\n"
    "```\n"
)


def first_paragraph(text: str) -> str:
    body = re.sub(r"^#.*$", "", text, count=1, flags=re.MULTILINE).strip()
    para = body.split("\n\n", 1)[0].replace("\n", " ").strip()
    return para[:400]


def build(out_dir: Path) -> list[Path]:
    written: list[Path] = []
    for stem, (name, description) in SKILLS.items():
        role_file = ROLES_DIR / f"{stem}.md"
        if not role_file.exists():
            continue
        content = role_file.read_text(encoding="utf-8")
        desc = description or first_paragraph(content)
        frontmatter = f"---\nname: {name}\ndescription: {desc}\n---\n\n"
        skill_dir = out_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        target = skill_dir / "SKILL.md"
        target.write_text(frontmatter + content.strip() + TOOLKIT_NOTE, encoding="utf-8")
        written.append(target)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "skills")
    args = parser.parse_args()
    written = build(args.out)
    for path in written:
        print(path)
    print(f"generated {len(written)} skills into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
