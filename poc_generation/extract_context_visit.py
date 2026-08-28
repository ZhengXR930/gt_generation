#!/usr/bin/env python3
"""Extract agent-visible source context visits from saved PoC checkpoints.

The output intentionally mirrors the lightweight shape of ``context_gt.json``
while recording what the agent inspected, not what the PoC executed.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = ROOT / "poc_generation" / "poc_results"
SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".inc",
    ".l",
    ".ll",
    ".m",
    ".mm",
    ".py",
    ".go",
    ".rs",
    ".java",
    ".js",
    ".ts",
    ".php",
    ".rb",
    ".y",
    ".yy",
}
SKIP_BASENAMES = {
    "analysis.json",
    "candidate_trace.json",
    "description.txt",
    "prompt.txt",
    "README.md",
    "readme.md",
    "submit.sh",
    "build.sh",
}
PATH_RE = re.compile(
    r"(?P<path>(?:/[^\s:'\"`<>]+|(?:repo-vul/src-vul|src-vul|/workspace/repo-vul/src-vul)/[^\s:'\"`<>]+))"
)
BACKTICK_IDENT_RE = re.compile(r"`([A-Za-z_~][A-Za-z0-9_:~]*)`")
FUNCTION_LINE_RE = re.compile(
    r"^\s*(?:(?P<line>\d+)[\s:]+)?"
    r"(?:(?:static|inline|extern|const|virtual|public|private|protected|async|export|def)\s+)*"
    r"(?:[A-Za-z_][\w:<>,~*&\[\]\s]+\s+)?"
    r"(?P<name>[A-Za-z_~][A-Za-z0-9_:~]*)\s*\([^;{}]*\)\s*(?:\{|:)?"
)
GREP_RESULT_RE = re.compile(
    r"(?P<path>(?:repo-vul/src-vul|src-vul|/workspace/repo-vul/src-vul|/[^:\s]+)/(?:[^:\s]+)):(?P<line>\d+):(?P<code>.*)"
)
SED_RANGE_RE = re.compile(
    r"sed\s+-n\s+['\"]?(?P<start>\d+)\s*,\s*(?P<end>\d+)p['\"]?\s+(?P<path>[^\s;&|]+)"
)
MAX_PLAIN_LOG_BYTES = 8 * 1024 * 1024
MAX_STRUCTURED_TRAJECTORY_BYTES = 12 * 1024 * 1024
MAX_EVENT_TEXT_CHARS = 240_000
MAX_PLAIN_LOG_LINE_CHARS = 40_000


def read_text_limited(path: Path, *, max_bytes: int = MAX_PLAIN_LOG_BYTES) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size <= max_bytes:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    head_size = max_bytes // 2
    tail_size = max_bytes - head_size
    try:
        with path.open("rb") as handle:
            head = handle.read(head_size)
            handle.seek(max(0, size - tail_size))
            tail = handle.read(tail_size)
    except OSError:
        return ""
    marker = (
        f"\n\n[context_visit extractor skipped {size - max_bytes} bytes "
        "from the middle of this oversized log]\n\n"
    ).encode()
    return (head + marker + tail).decode("utf-8", errors="replace")


def shorten_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return text[:head] + "\n[context_visit extractor skipped long text middle]\n" + text[-tail:]


@dataclass
class Visit:
    file: str
    function: str
    line: int
    kind: str = "function_visit"
    evidence: set[str] = field(default_factory=set)
    commands: set[str] = field(default_factory=set)
    count: int = 0

    def merge(self, *, line: int | None, evidence: str, command: str | None = None) -> None:
        if line and (self.line <= 0 or line < self.line):
            self.line = line
        if evidence:
            self.evidence.add(evidence)
        if command:
            self.commands.add(command)
        self.count += 1

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "file": self.file,
            "function": self.function,
            "line": self.line if self.line > 0 else 1,
            "visit_count": self.count,
        }


def normalize_source_path(value: str) -> str | None:
    value = value.strip().strip("'\"`.,;)")
    value = value.replace("\\", "/")
    for marker in (
        "/repo-vul/src-vul/",
        "repo-vul/src-vul/",
        "/src-vul/",
        "src-vul/",
        "/gt/_work/src/",
        "/_work/src/",
    ):
        if marker in value:
            value = value.split(marker, 1)[1]
            break
    if value.startswith("/workspace/"):
        value = value[len("/workspace/") :]
    value = re.sub(r"[:),.;]+$", "", value)
    if (
        not value
        or value.startswith(("/tmp/", "/out/", "/usr/", "/bin/", "../"))
        or "/../" in value
    ):
        return None
    basename = value.rsplit("/", 1)[-1]
    if basename in SKIP_BASENAMES:
        return None
    suffix = Path(basename).suffix
    if suffix and suffix not in SOURCE_EXTENSIONS:
        return None
    if not suffix:
        return None
    return value.lstrip("/")


def function_from_code_line(code: str) -> str | None:
    match = FUNCTION_LINE_RE.match(code.strip())
    if not match:
        return None
    name = match.group("name")
    if name in {
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "catch",
        "foreach",
    }:
        return None
    return name


def add_visit(
    visits: dict[tuple[str, str], Visit],
    *,
    file: str,
    function: str | None,
    line: int | None,
    evidence: str,
    command: str | None = None,
    kind: str | None = None,
) -> None:
    normalized = normalize_source_path(file)
    if not normalized:
        return
    function = (function or "").strip()
    if not function:
        function = "<file>"
        kind = kind or "file_visit"
    line_value = int(line) if isinstance(line, int) and line > 0 else 1
    key = (normalized, function)
    if key not in visits:
        visits[key] = Visit(
            file=normalized,
            function=function,
            line=line_value,
            kind=kind or ("file_visit" if function == "<file>" else "function_visit"),
        )
    visits[key].merge(line=line_value, evidence=evidence, command=command)


def paths_in_text(text: str) -> list[str]:
    out: list[str] = []
    for match in PATH_RE.finditer(text):
        path = normalize_source_path(match.group("path"))
        if path:
            out.append(path)
    return list(dict.fromkeys(out))


def extract_from_text(
    visits: dict[tuple[str, str], Visit],
    text: str,
    *,
    source: str,
    command: str | None = None,
) -> None:
    if not text:
        return
    text = shorten_middle(text, MAX_EVENT_TEXT_CHARS)
    for path in paths_in_text(text):
        add_visit(visits, file=path, function=None, line=None, evidence=source, command=command)

    for match in GREP_RESULT_RE.finditer(text):
        path = normalize_source_path(match.group("path"))
        if not path:
            continue
        line = int(match.group("line"))
        code = match.group("code")
        function = function_from_code_line(code)
        if function:
            add_visit(
                visits,
                file=path,
                function=function,
                line=line,
                evidence=f"{source}:grep_result",
                command=command,
            )
        else:
            for ident in BACKTICK_IDENT_RE.findall(command or ""):
                if ident and len(ident) > 2:
                    add_visit(
                        visits,
                        file=path,
                        function=ident,
                        line=line,
                        evidence=f"{source}:grep_pattern",
                        command=command,
                    )

    active_file: str | None = None
    active_line = 1
    sed = SED_RANGE_RE.search(command or "")
    if sed:
        active_file = normalize_source_path(sed.group("path"))
        active_line = int(sed.group("start"))
    command_paths = paths_in_text(command or "")
    if active_file is None and len(command_paths) == 1:
        active_file = command_paths[0]
    if active_file:
        for raw_line in text.splitlines():
            numbered = re.match(r"^\s*(?P<line>\d+)[\s:]+(?P<code>.*)$", raw_line)
            if numbered:
                line_no = int(numbered.group("line"))
                code = numbered.group("code")
            else:
                line_no = active_line
                code = raw_line
                active_line += 1
            function = function_from_code_line(code)
            if function:
                add_visit(
                    visits,
                    file=active_file,
                    function=function,
                    line=line_no,
                    evidence=f"{source}:code_snippet",
                    command=command,
                )

    mentioned_functions = [
        item
        for item in BACKTICK_IDENT_RE.findall(text)
        if len(item) > 2 and "/" not in item and "." not in item
    ]
    if mentioned_functions and command_paths:
        for path in command_paths[:3]:
            for function in mentioned_functions[:8]:
                add_visit(
                    visits,
                    file=path,
                    function=function,
                    line=None,
                    evidence=f"{source}:nearby_function_mention",
                    command=command,
                )


def iter_openhands_events(path: Path) -> Iterable[tuple[str, str | None, str]]:
    try:
        if path.stat().st_size > MAX_STRUCTURED_TRAJECTORY_BYTES:
            yield from iter_plain_log(path)
            return
    except OSError:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, list):
        return
    last_command: str | None = None
    for event in data:
        if not isinstance(event, dict):
            continue
        source = str(event.get("source") or "")
        if source not in {"agent", "environment"}:
            continue
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        command = str(args.get("command") or "")
        content = str(event.get("content") or args.get("content") or "")
        message = str(event.get("message") or "")
        if command:
            last_command = command
            yield "checkpoint/trajectory:command", command, command
        if content:
            yield "checkpoint/trajectory:output", command or last_command, content
        elif message and source == "environment":
            yield "checkpoint/trajectory:message", command or last_command, message


def iter_claude_jsonl(path: Path) -> Iterable[tuple[str, str | None, str]]:
    last_command: str | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "assistant":
            for item in (event.get("message") or {}).get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    tool_input = item.get("input") if isinstance(item.get("input"), dict) else {}
                    command = str(tool_input.get("command") or tool_input.get("file_path") or tool_input)
                    last_command = command
                    yield "checkpoint/claude_stdout.jsonl:tool", command, command
                elif item.get("type") == "text":
                    text = str(item.get("text") or "")
                    yield "checkpoint/claude_stdout.jsonl:assistant", last_command, text
        elif event_type == "user":
            for item in (event.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    yield "checkpoint/claude_stdout.jsonl:tool_result", last_command, str(item.get("content") or "")


def iter_observed_context_jsonl(path: Path) -> Iterable[tuple[str, str | None, str]]:
    """Read normalized tool observations saved by local harness adapters."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("is_code_context") is False:
            continue
        tool = str(event.get("tool") or "tool")
        command = str(event.get("command") or "")
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
        if not command:
            command = str(arguments.get("command") or arguments.get("file_path") or "")
        command_name = Path(command.strip().strip("'\"`.,;)")).name
        if command_name in SKIP_BASENAMES:
            continue
        output = str(event.get("output") or "")
        if command:
            yield f"observed_context.jsonl:{tool}:command", command, command
        if output:
            yield f"observed_context.jsonl:{tool}:output", command or None, output


def iter_plain_log(path: Path) -> Iterable[tuple[str, str | None, str]]:
    text = read_text_limited(path)
    if not text:
        return
    if "\ncodex\n" in text:
        text = text.split("\ncodex\n", 1)[1]
    elif "\nexec\n" in text:
        text = text.split("\nexec\n", 1)[1]
    last_command: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("/bin/bash -lc", "bash -lc", "exec ", "read ")):
            last_command = stripped
            yield f"{path.name}:command", stripped, stripped
        elif "Command `" in stripped and "` executed" in stripped:
            command = stripped.split("Command `", 1)[1].split("` executed", 1)[0]
            last_command = command
            yield f"{path.name}:command", command, command
        else:
            yield f"{path.name}:output", last_command, shorten_middle(stripped, MAX_PLAIN_LOG_LINE_CHARS)


def source_streams(sample_dir: Path) -> Iterable[tuple[str, str | None, str]]:
    checkpoint = sample_dir / "checkpoint"
    checkpoint_sources = 0
    trajectory = checkpoint / "trajectory"
    if trajectory.is_file():
        checkpoint_sources += 1
        yield from iter_openhands_events(trajectory)
    claude_jsonl = checkpoint / "claude_stdout.jsonl"
    if claude_jsonl.is_file():
        checkpoint_sources += 1
        yield from iter_claude_jsonl(claude_jsonl)
    observed_jsonl = checkpoint / "observed_context.jsonl"
    if observed_jsonl.is_file():
        checkpoint_sources += 1
        yield from iter_observed_context_jsonl(observed_jsonl)
    for relative in (
        "claude_transcript.txt",
        "codex_stdout.txt",
        "agent.log",
        "dsh_stdout.txt",
        "dsh_stderr.txt",
    ):
        path = checkpoint / relative
        if path.is_file():
            checkpoint_sources += 1
            yield from iter_plain_log(path)
    for path in sorted(checkpoint.glob("sessions-jsonl/**/*.jsonl")):
        checkpoint_sources += 1
        yield from iter_plain_log(path)
    if checkpoint_sources == 0:
        for path in sorted((sample_dir / "runs").glob("*.log")):
            yield from iter_plain_log(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def build_context_visit(sample_dir: Path) -> dict[str, Any]:
    visits: dict[tuple[str, str], Visit] = {}
    source_counts: Counter[str] = Counter()
    stream_count = 0
    for source, command, text in source_streams(sample_dir):
        stream_count += 1
        source_counts[source] += 1
        extract_from_text(visits, text, source=source, command=command)

    manifest = load_json(sample_dir / "manifest.json")
    model = str(manifest.get("model") or "")
    harness = str(manifest.get("harness") or "")
    context = sorted(
        (visit.as_json() for visit in visits.values()),
        key=lambda item: (item["file"], item["function"], item["line"]),
    )
    return {
        "schema_version": "gt-context-v1",
        "sample_id": sample_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collection": {
            "mode": "agent_checkpoint_context_visit",
            "source": "poc_generation_checkpoint",
            "harness": harness,
            "model": model,
            "stream_records": stream_count,
            "context_count": len(context),
            "recoverable": bool(context),
        },
        "context": context,
    }


def sample_dirs(root: Path, namespaces: list[str]) -> Iterable[Path]:
    selected = namespaces or [
        path.name
        for path in sorted(root.iterdir())
        if path.is_dir() and not path.name.startswith("_")
    ]
    for namespace in selected:
        ns_dir = root / namespace
        if not ns_dir.is_dir():
            continue
        for child in sorted(ns_dir.iterdir()):
            if child.is_dir() and ((child / "manifest.json").is_file() or (child / "checkpoint").is_dir()):
                yield child


def update_manifest(sample_dir: Path, context_visit: dict[str, Any]) -> None:
    manifest_path = sample_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    manifest = load_json(manifest_path)
    manifest["context_visit"] = {
        "path": "context_visit.json",
        "schema_version": context_visit.get("schema_version"),
        "context_count": context_visit.get("collection", {}).get("context_count", 0),
        "recoverable": context_visit.get("collection", {}).get("recoverable", False),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_context_visit(sample_dir: Path, *, overwrite: bool, update_manifest_flag: bool) -> bool:
    out = sample_dir / "context_visit.json"
    if out.is_file() and not overwrite:
        return False
    report = build_context_visit(sample_dir)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if update_manifest_flag:
        update_manifest(sample_dir, report)
    return True


def _process_sample_dir(payload: tuple[str, bool, bool]) -> dict[str, Any]:
    sample_dir_text, overwrite, update_manifest_flag = payload
    sample_dir = Path(sample_dir_text)
    written = write_context_visit(
        sample_dir,
        overwrite=overwrite,
        update_manifest_flag=update_manifest_flag,
    )
    report = load_json(sample_dir / "context_visit.json")
    return {
        "sample_dir": sample_dir_text,
        "written": written,
        "recoverable": bool(report.get("collection", {}).get("recoverable")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=RESULTS_ROOT)
    parser.add_argument("--namespace", action="append", default=[])
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-update-manifest", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sample", action="append", default=[])
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args(argv)

    wanted_samples = set(args.sample or [])
    dirs: list[Path] = []
    for sample_dir in sample_dirs(args.root, args.namespace):
        if wanted_samples and sample_dir.name not in wanted_samples:
            continue
        dirs.append(sample_dir)
        if args.limit and len(dirs) >= args.limit:
            break

    counts = {"checked": 0, "written": 0, "skipped": 0, "recoverable": 0}
    payloads = [
        (str(sample_dir), bool(args.overwrite), not args.no_update_manifest)
        for sample_dir in dirs
    ]
    if args.jobs > 1 and len(payloads) > 1:
        with multiprocessing.Pool(processes=args.jobs) as pool:
            results = pool.imap_unordered(_process_sample_dir, payloads, chunksize=8)
            for result in results:
                counts["checked"] += 1
                if result["written"]:
                    counts["written"] += 1
                else:
                    counts["skipped"] += 1
                if result["recoverable"]:
                    counts["recoverable"] += 1
    else:
        for sample_dir in dirs:
            written = write_context_visit(
                sample_dir,
                overwrite=args.overwrite,
                update_manifest_flag=not args.no_update_manifest,
            )
            counts["checked"] += 1
            if written:
                counts["written"] += 1
            else:
                counts["skipped"] += 1
            report = load_json(sample_dir / "context_visit.json")
            if report.get("collection", {}).get("recoverable"):
                counts["recoverable"] += 1
    print(json.dumps({"counts": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
