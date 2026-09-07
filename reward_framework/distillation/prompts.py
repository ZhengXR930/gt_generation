"""Task prompts for the distillation role subsessions.

Roles are agents with a working directory, so a prompt names the files to read
and the artifact to write rather than carrying the data inline. That is what
lets the diagnostician read a 300 KB trajectory instead of a truncated excerpt.
"""
from __future__ import annotations

from typing import Iterable

from .defaults import REPO_ROOT

TEMPLATE_DIR = REPO_ROOT / "reward_framework" / "distillation" / "prompt_templates"


def load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def role_task_prompt(
    template_name: str,
    *,
    inputs: Iterable[tuple[str, str]],
    output_rel: str = "OUTPUT.json",
) -> str:
    """Role template plus an index of what is on disk and where to write."""
    lines = [load_template(template_name).rstrip(), "", "## Inputs", "",
             "Everything you need is already in this directory. Read it with real tools;"
             " nothing has been truncated for you.", ""]
    for relative, description in inputs:
        lines.append(f"- `{relative}` — {description}")
    lines += [
        "",
        "## Output",
        "",
        f"Write exactly one JSON object to `{output_rel}` in this directory, with the keys"
        " described above and no Markdown around it. That file is the result: do not rely"
        " on your final message being read, and do not write any other file.",
        "",
    ]
    return "\n".join(lines)
