"""Frontend-neutral prompt rendering and workspace augmentation hooks."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any, MutableMapping


def validate_candidate_input_bytes(content: bytes) -> str | None:
    """Reject only payloads that are clearly metadata/prompt text, not input bytes."""
    if not content:
        return "submitted candidate input is empty"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    stripped = text.lstrip("\ufeff\x00\r\n\t ")
    if stripped.startswith(("{", "[")):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            keys = set(value)
            if {"sample_id", "fine_trace", "vuln_logic"} <= keys or {
                "fine_trace",
                "vuln_logic",
            } <= keys:
                return "submitted candidate input looks like an analysis artifact JSON"
        elif isinstance(value, list) and value:
            required = {"step", "file", "function", "line", "var", "code", "note"}
            if all(
                isinstance(item, dict) and required <= set(item)
                for item in value[:20]
            ):
                return "submitted candidate input looks like a fine_trace JSON array"

    prompt_markers = (
        "# PoC generation task:",
        "# Regression input task:",
        "regression benchmark issue",
        "Generate the benchmark regression-input candidate",
        "You are working on a local benchmark sample",
        "Workspace layout:",
        "# Submission Instructions",
        "analysis.json artifact",
        "/workspace/submit.sh",
        "bash ./submit.sh",
        "Every submitted PoC is an independently evaluated candidate",
        "Every submitted input is an independently evaluated candidate",
        "candidate_trace.json",
        "The submission records the PoC bytes",
        "The submission records the candidate bytes",
    )
    marker_count = sum(marker in text for marker in prompt_markers)
    if stripped.startswith(("# PoC generation task:", "# Regression input task:")):
        return "submitted candidate input looks like prompt/report text"
    if marker_count >= 3:
        return "submitted candidate input looks like prompt/report text"
    return None


def candidate_guard_shell_snippet(
    *, candidate_var: str = "POC_FILE", analysis_var: str = "ANALYSIS_FILE"
) -> str:
    """Return shell code that rejects non-input artifacts before submission."""
    return f"""
# gt_generation submit candidate guard
POC_REAL="$(readlink -f "${{{candidate_var}}}" 2>/dev/null || printf '%s' "${{{candidate_var}}}")"
ANALYSIS_REAL="$(readlink -f "${{{analysis_var}}}" 2>/dev/null || printf '%s' "${{{analysis_var}}}")"
if [ "$POC_REAL" = "$ANALYSIS_REAL" ]; then
    echo "Error: submitted candidate input must not be the analysis artifact." >&2
    exit 2
fi

python3 - "${{{candidate_var}}}" <<'GT_CANDIDATE_GUARD_PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = path.read_bytes()
if not data:
    print("Error: submitted candidate input is empty.", file=sys.stderr)
    sys.exit(2)

try:
    text = data.decode("utf-8")
except UnicodeDecodeError:
    sys.exit(0)

stripped = text.lstrip("\\ufeff\\x00\\r\\n\\t ")
if stripped.startswith(("{{", "[")):
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        keys = set(value)
        if {{"sample_id", "fine_trace", "vuln_logic"}} <= keys or {{"fine_trace", "vuln_logic"}} <= keys:
            print("Error: submitted candidate input looks like an analysis artifact JSON, not input bytes.", file=sys.stderr)
            sys.exit(2)
    elif isinstance(value, list) and value:
        required = {{"step", "file", "function", "line", "var", "code", "note"}}
        if all(isinstance(item, dict) and required <= set(item) for item in value[:20]):
            print("Error: submitted candidate input looks like a fine_trace JSON array, not input bytes.", file=sys.stderr)
            sys.exit(2)

prompt_markers = (
    "# PoC generation task:",
    "# Regression input task:",
    "regression benchmark issue",
    "Generate the benchmark regression-input candidate",
    "You are working on a local benchmark sample",
    "Workspace layout:",
    "# Submission Instructions",
    "analysis.json artifact",
    "/workspace/submit.sh",
    "bash ./submit.sh",
    "Every submitted PoC is an independently evaluated candidate",
    "Every submitted input is an independently evaluated candidate",
    "candidate_trace.json",
    "The submission records the PoC bytes",
    "The submission records the candidate bytes",
)
marker_count = sum(marker in text for marker in prompt_markers)
if stripped.startswith(("# PoC generation task:", "# Regression input task:")) or marker_count >= 3:
    print("Error: submitted candidate input looks like prompt/report text, not input bytes.", file=sys.stderr)
    sys.exit(2)
GT_CANDIDATE_GUARD_PY
POC_CHECK_EXIT="$?"
if [ "$POC_CHECK_EXIT" -ne 0 ]; then
    exit "$POC_CHECK_EXIT"
fi
"""


def install_submit_candidate_guard(
    submit_path: Path,
    *,
    candidate_var: str = "POC_FILE",
    analysis_var: str = "ANALYSIS_FILE",
) -> bool:
    """Inject the candidate guard into a generated submit.sh script."""
    if not submit_path.is_file():
        return False
    text = submit_path.read_text(encoding="utf-8", errors="replace")
    if "gt_generation submit candidate guard" in text:
        return False
    anchor = "RESPONSE_FILE="
    index = text.find(anchor)
    if index < 0:
        raise RuntimeError(f"cannot install candidate guard in {submit_path}")
    snippet = candidate_guard_shell_snippet(
        candidate_var=candidate_var, analysis_var=analysis_var
    )
    submit_path.write_text(text[:index] + snippet + "\n" + text[index:], encoding="utf-8")
    submit_path.chmod(0o755)
    return True


def render_prompt(prompt_file: Path, *, sample_id: str, workspace: Path) -> str:
    """Render the caller-owned prompt for one concrete workspace."""
    prompt_file = prompt_file.expanduser().resolve()
    if not prompt_file.is_file():
        raise FileNotFoundError(f"prompt file not found: {prompt_file}")
    return (
        prompt_file.read_text(encoding="utf-8", errors="replace")
        .replace("<current sample id>", sample_id)
        .replace("/workspace", str(workspace))
    )


def run_workspace_installer(
    target: str | None,
    *,
    harness: str,
    workspace: Path,
    sample_id: str,
    scratch: Path,
    env: MutableMapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Invoke an explicitly selected frontend adapter hook.

    The neutral runtime never discovers a frontend and never falls back to a
    legacy installer.  Baseline callers pass no target; reward callers pass the
    adapter module they own.
    """
    target = str(target or "").strip()
    if not target:
        return None
    module_name, separator, function_name = target.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError("workspace installer must be module:function")
    installer = getattr(importlib.import_module(module_name), function_name)
    result = installer(
        harness=harness,
        workspace=workspace,
        sample_id=sample_id,
        scratch=scratch,
        env=env if env is not None else os.environ,
    )
    if result is None:
        return None
    if not isinstance(result, dict):
        raise TypeError("workspace installer must return a dict or None")
    return result
