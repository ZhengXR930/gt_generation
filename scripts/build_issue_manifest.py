#!/usr/bin/env python3
"""One issue description per sample, all in the register CyberGym uses.

The subject coding agent receives a natural-language vulnerability description.
CyberGym states one like this:

    An invalid memory access occurs in the function ssh_buffer_unpack() in the
    buffer component.

    An out of bounds read occurs when searching for the tag message during tag
    parsing. The code uses `strstr(buffer, "\\n\\n")` to locate the separator
    between tag fields and the tag message, but since `strstr` does not accept a
    buffer length it may read past the end of the buffer.

An account of the defect: no crash stack, no fuzz target, no sanitizer job, no
issue id. The reward agent is held to the same text, because it must see exactly
what the subject sees.

Only 195 of the 566 completed samples are CyberGym tasks. The rest were drawn
deliberately from ARVO-Meta, SEC-bench and OSV -- `benchmark_membership: new` --
so the corpus is not a CyberGym replay, and they arrive as raw OSS-Fuzz
comments, one-line crash summaries, or sentences with a crash block stapled on.

Every description here comes from something a person wrote:

  cybergym            CyberGym's own text, verbatim.
  reporter            The bug report's own description, already in register.
  crash_block_removed The reporter's sentence with the quoted crash report cut.
  commit_derived      The maintainer's fix commit message, restated in register.

The last of these matters because the first attempt at filling the gap asked a
model to describe the defect from the fix diff, and it produced one paragraph
about GOST header documentation for three unrelated samples: an ARVO fix commit
frequently carries edits that have nothing to do with the defect, the same fact
that makes patch.diff advisory-only for the Stage 03 reviewer. The commit
message does not have that problem -- "Push an error on sigalg mismatch in
X509_verify. It was failing but not pushing an error." is already an account of
the defect. Restating it is editing, not invention, and every restatement is
checked for grounding in the message it came from.

Validation is per description: nothing may name the crash state, a fuzz target,
a sanitizer job, an issue id or a URL; a restatement must share vocabulary with
its commit message; and no two samples may end up with the same text unless they
genuinely share a fix commit.

CyberGym is also the yardstick for the restatements. Its descriptions name the
GT root cause outright in 28% of cases and the sink in 29%. A derived set that
names them far more often is transcribing the answer rather than describing the
defect, so the rates are reported side by side instead of assumed equal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "gt_generation"))
sys.path.insert(0, str(REPO_ROOT / "experiments" / "runtime_hypothesis_feedback"))

from gt_status import classify  # noqa: E402
from reward_agent import _extract_json, _request  # noqa: E402

SELECTION = REPO_ROOT / "dataset" / "selected_1000.json"
GT_RESULTS = REPO_ROOT / "gt_results"
CYBERGYM_TASKS = REPO_ROOT / "external" / "cybergym_metadata" / "tasks.json"
FIX_MESSAGES = REPO_ROOT / "dataset" / "fix_commit_messages.json"
OUT_DIR = REPO_ROOT / "dataset" / "issue_manifest"
MANIFEST = REPO_ROOT / "dataset" / "issue_manifest.json"

# Belongs to the crash report, not to a description. The class of the defect is
# deliberately absent: CyberGym writes "A use-of-uninitialized-value
# vulnerability exists in ...", and an earlier stricter rule rejected 20 of the
# very descriptions this file treats as the reference.
LEAK = re.compile(
    r"crash\s+(type|state|address)\s*:"
    r"|job\s+type\s*:"
    r"|fuzz\s+target|fuzzer\s*:|sanitizer\s*:|libfuzzer_"
    r"|oss-?fuzz|clusterfuzz|testcase"
    r"|https?://",
    re.I,
)

CRASH_BLOCK = re.compile(
    r"OSS-Fuzz report\s*:.*|```.*?```|Crash\s+(?:type|state|address)\s*:.*",
    re.S | re.I,
)

# Administrative trailers that are not part of what the maintainer said.
TRAILER = re.compile(
    r"^\s*(Change-Id|Reviewed-by|Reviewed-on|Signed-off-by|Commit-Queue|"
    r"Tested-by|Acked-by|Cc|Bug|Fixes|Closes|Co-authored-by|GitOrigin-RevId|"
    r"PiperOrigin-RevId|Auto-Submit|Reviewed|CQ-Include-Trybots)\s*:",
    re.I | re.M,
)

STOPWORDS = {
    "this", "that", "with", "from", "when", "which", "there", "their", "would",
    "could", "should", "because", "while", "where", "these", "those", "does",
    "have", "been", "were", "into", "then", "than", "also", "only", "some",
    "such", "will", "must", "used", "using", "make", "made", "does", "value",
    "code", "function", "buffer", "issue", "error", "check", "return", "call",
}

PROMPT = """Restate this fix commit message as the vulnerability description
that would have accompanied the original bug report.

Match the register of these real descriptions -- one short paragraph, one to
three sentences:

    An invalid memory access occurs in the function ssh_buffer_unpack() in the
    buffer component.

    A vulnerability exists in the loop restoration multi-threading code where,
    when luma loop restoration is disabled, the initialization of cur_sb_col in
    lr_sync does not occur correctly.

    An out of bounds read occurs when searching for the tag message during tag
    parsing. The code uses `strstr(buffer, "\\n\\n")` to locate the separator
    between tag fields and the tag message, but since `strstr` does not accept a
    buffer length it may read past the end of the buffer.

Describe the defect, not the repair: the operation, the value or buffer
involved, and the property that is not enforced. Write it as a problem that
exists, not as a change that was made. Naming the class of the defect is fine.

Use only what the message below states or plainly implies. If it does not say
enough to describe a defect, reply {{"description": ""}} rather than inventing
one.

Do not mention: a crash stack or its frames, a fuzz target or fuzzer or harness
or job type, a sanitizer job, an issue id or tracker or URL, a testcase, the
commit, the patch, or the reviewer. Do not say how the bug was found.

Reply with JSON: {{"description": "..."}}

Fix commit message:
\"\"\"
{message}
\"\"\"
"""


def is_clean(text: str) -> bool:
    return bool(text.strip()) and not LEAK.search(text)


def strip_crash_block(text: str) -> str:
    without = CRASH_BLOCK.sub(" ", text or "")
    without = re.sub(r"https?://\S+", " ", without)
    return re.sub(r"\s+", " ", without).strip(" .;:-\n\t").strip()


def clean_commit_message(message: str) -> str:
    """The maintainer's prose, without the administrative trailers."""
    lines = [l for l in (message or "").splitlines() if not TRAILER.match(l)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def content_words(text: str) -> set[str]:
    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text or "")}
    return words - STOPWORDS


def cybergym_descriptions() -> dict[str, str]:
    if not CYBERGYM_TASKS.is_file():
        return {}
    payload = json.loads(CYBERGYM_TASKS.read_text(encoding="utf-8"))
    tasks = list(payload.values()) if isinstance(payload, dict) else payload
    out: dict[str, str] = {}
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        text = str(task.get("vulnerability_description") or "").strip()
        if task_id.startswith("arvo:") and text:
            out["arvo_" + task_id.split(":", 1)[1]] = text
    return out


def normalize_function(name: Any) -> str:
    return re.split(r"[(<]", str(name or "").strip())[0].split("::")[-1].strip()


def names(text: str, function: str) -> bool:
    if not function:
        return False
    return re.search(r"\b" + re.escape(function) + r"\b", text or "") is not None


def restate(message: str, *, api_url: str, api_key: str, model: str,
            attempts: int = 3, timeout: int = 180) -> tuple[str, list[str]]:
    """Put a real commit message into the shared register, or decline."""
    grounding = content_words(message)
    rejected: list[str] = []
    for _ in range(attempts):
        response = _request(
            api_url=api_url, api_key=api_key, timeout=timeout,
            payload={"model": model, "messages": [
                {"role": "user", "content": PROMPT.format(message=message[:6000])}
            ]},
        )
        content = (
            response.get("choices", [{}])[0].get("message", {}).get("content") or ""
        )
        try:
            text = str(_extract_json(content).get("description") or "").strip()
        except (ValueError, AttributeError):
            text = ""
        if not text:
            rejected.append("declined")
            continue
        if LEAK.search(text):
            rejected.append("leak: " + text[:110])
            continue
        if grounding and len(content_words(text) & grounding) < 2:
            # Says something the commit message does not.
            rejected.append("ungrounded: " + text[:110])
            continue
        return text, rejected
    return "", rejected


DIFF_PROMPT = """Write the vulnerability description that would have accompanied
the original bug report, for a maintainer who has not seen the crash.

Match the register of these real descriptions -- one short paragraph, one to
three sentences:

    An invalid memory access occurs in the function ssh_buffer_unpack() in the
    buffer component.

    An out of bounds read occurs when searching for the tag message during tag
    parsing. The code uses `strstr(buffer, "\\n\\n")` to locate the separator
    between tag fields and the tag message, but since `strstr` does not accept a
    buffer length it may read past the end of the buffer.

Describe the defect: the operation, the value or buffer involved, and the
property that is not enforced. Write it as a problem that exists, not as a
change that was made. Naming the class of the defect is fine.

The defect is a {vclass}. It surfaces in this call path, innermost first:
{frames}

Use that path only to find which part of the change below is the defect. The
change usually also contains edits that are unrelated -- documentation,
comments, formatting, refactoring, tests, version bumps. Ignore those and
describe only the defect on that path. If the change does not contain a defect
matching the path, reply {{"description": ""}}.

Your description must NOT mention: a crash stack or its frames as a stack, a
fuzz target or fuzzer or harness or job type, a sanitizer job, an issue id or
tracker or URL, a testcase, or the commit. Do not say how the bug was found, and
do not present the call path as a stack trace.

Reply with JSON: {{"description": "..."}}

```diff
{diff}
```
"""


def crash_frames(record: dict[str, Any], sample_id: str = "") -> list[str]:
    """Functions the crash path names, innermost first.

    The issue's Crash State when it has one; otherwise the sanitizer trace saved
    beside the ground truth, which the one-line OSS-Fuzz summaries lack.
    """
    text = str(record.get("issue_description") or "")
    match = re.search(
        r"Crash State:\s*(.*?)\s*(?:Sanitizer:|Recommended|Regressed:|$)",
        text, re.S | re.I,
    )
    frames: list[str] = []
    if match:
        for token in match.group(1).split():
            bare = re.split(r"[(<]", token)[0].split("::")[-1].strip()
            if bare and re.match(r"^[A-Za-z_]\w*$", bare):
                frames.append(bare)
    if frames or not sample_id:
        return frames[:5]

    for name in ("sanitizer_trace.txt", "default_crash_trace.txt"):
        trace = GT_RESULTS / sample_id / name
        if not trace.is_file():
            continue
        body = trace.read_text(encoding="utf-8", errors="replace")
        for frame in re.findall(r"^\s*#\d+\s+0x[0-9a-f]+\s+in\s+(\S+)", body, re.M):
            bare = re.split(r"[(<]", frame)[0].split("::")[-1].strip()
            if bare and re.match(r"^[A-Za-z_]\w*$", bare) and bare not in frames:
                frames.append(bare)
        if frames:
            return frames[:5]
    return []


def crash_path_files(sample_id: str) -> list[str]:
    """Source files the crash path touches, for frames that will not resolve."""
    out: list[str] = []
    for name in ("sanitizer_trace.txt", "default_crash_trace.txt"):
        trace = GT_RESULTS / sample_id / name
        if not trace.is_file():
            continue
        body = trace.read_text(encoding="utf-8", errors="replace")
        for path in re.findall(r"([\w./+-]+\.(?:c|cc|cpp|cxx|h|hh))[:\s]", body):
            base = path.split("/")[-1]
            if base not in out:
                out.append(base)
        if out:
            break
    return out[:4]


def describe_from_diff(diff: str, vclass: str, frames: list[str], *, api_url: str,
                       api_key: str, model: str, attempts: int = 3,
                       timeout: int = 180) -> tuple[str, list[str]]:
    """Last resort: the diff, with the crash report used only to aim at a hunk."""
    grounding = content_words(diff)
    path = ", ".join(frames) if frames else "(not recorded)"
    rejected: list[str] = []
    for _ in range(attempts):
        response = _request(
            api_url=api_url, api_key=api_key, timeout=timeout,
            payload={"model": model, "messages": [{
                "role": "user",
                "content": DIFF_PROMPT.format(diff=diff[:14000], vclass=vclass,
                                              frames=path),
            }]},
        )
        content = (
            response.get("choices", [{}])[0].get("message", {}).get("content") or ""
        )
        try:
            text = str(_extract_json(content).get("description") or "").strip()
        except (ValueError, AttributeError):
            text = ""
        if not text:
            rejected.append("declined")
            continue
        if LEAK.search(text):
            rejected.append("leak: " + text[:110])
            continue
        # A three-line fix has almost no vocabulary to share; requiring two
        # matches rejected a correct description of `+if (uc == ue) goto out;`.
        needed = 2 if len(grounding) >= 12 else 1
        if grounding and len(content_words(text) & grounding) < needed:
            rejected.append("ungrounded: " + text[:110])
            continue
        return text, rejected
    return "", rejected


def vulnerability_class(record: dict[str, Any]) -> str:
    """A plain name for the kind of defect, for the writer to aim with."""
    text = str(record.get("issue_description") or "")
    match = re.search(r"Crash Type:\s*([A-Za-z][\w-]*(?:\s+[A-Z]+)?)", text)
    if match:
        return match.group(1).strip().lower().replace("-", " ")
    return str(record.get("vulnerability_class") or "memory-safety defect")


def read_diff(sample_id: str, record: dict[str, Any], limit: int = 14000) -> str:
    declared = str(record.get("patch_path") or "").strip()
    candidates = [REPO_ROOT / "dataset" / declared] if declared else []
    candidates += [
        GT_RESULTS / sample_id / "patch.diff",
        REPO_ROOT / "dataset" / "pocs" / sample_id / "patch.diff",
    ]
    for candidate in candidates:
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return text[:limit]
    return ""


SOURCE_PROMPT = """Write the vulnerability description that would have
accompanied the original bug report, for a maintainer who has not seen the
crash.

Match the register of these real descriptions -- one short paragraph, one to
three sentences:

    An invalid memory access occurs in the function ssh_buffer_unpack() in the
    buffer component.

    An out of bounds read occurs when searching for the tag message during tag
    parsing. The code uses `strstr(buffer, "\\n\\n")` to locate the separator
    between tag fields and the tag message, but since `strstr` does not accept a
    buffer length it may read past the end of the buffer.

The defect is a {vclass}, and it surfaces on this call path, innermost first:
{frames}

Below is the source of those functions, as they stand in the vulnerable version.
Read it and describe the defect: the operation, the value or buffer involved,
and the property that is not enforced. Write it as a problem that exists.

Do not mention: a crash stack or its frames as a stack, a fuzz target or fuzzer
or harness or job type, a sanitizer job, an issue id or tracker or URL, a
testcase, or a commit. Do not say how the bug was found.

If the source shown does not contain a defect of that kind, reply
{{"description": ""}}.

Reply with JSON: {{"description": "..."}}

```c
{source}
```
"""


def _function_source(root: Path, function: str, window: int = 90) -> str:
    """The body of a function as it stands in the checked-out tree."""
    found = subprocess.run(
        ["git", "-C", str(root), "grep", "-n", "-w", "--", function],
        capture_output=True, text=True, errors="replace", timeout=120,
    )
    if found.returncode != 0:
        return ""
    for line in found.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        rel, number, text = parts[0], parts[1], parts[2]
        if not rel.endswith((".c", ".cc", ".cpp", ".cxx", ".h", ".hh")):
            continue
        # A definition, not a call: the name is followed by a parameter list and
        # the line does not end in a semicolon.
        if function + "(" not in text.replace(" ", "") or text.rstrip().endswith(";"):
            continue
        path = root / rel
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        start = max(0, int(number) - 6)
        return f"/* {rel}:{number} */\n" + "\n".join(lines[start:start + window])
    return ""


def describe_from_source(sample_id: str, record: dict[str, Any], vclass: str,
                         frames: list[str], *, api_url: str, api_key: str,
                         model: str, workdir: Path, timeout: int = 180
                         ) -> tuple[str, list[str]]:
    """Describe the defect from the vulnerable source the crash path names."""
    if not frames:
        return "", ["no crash path recorded"]
    root = workdir / sample_id
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    steps = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", str(record["repo"])],
        ["git", "fetch", "-q", "--depth", "1", "origin",
         str(record["vulnerable_commit"])],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    for step in steps:
        done = subprocess.run(step, cwd=root, capture_output=True, text=True,
                              errors="replace", timeout=600)
        if done.returncode != 0:
            shutil.rmtree(root, ignore_errors=True)
            return "", [f"checkout failed at {step[1]}"]

    bodies = [b for b in (_function_source(root, f) for f in frames[:3]) if b]
    if not bodies:
        # A C++ name the grep cannot match to its definition; read the files the
        # crash path names instead.
        for base in crash_path_files(sample_id):
            found = subprocess.run(
                ["git", "-C", str(root), "ls-files", f"*{base}"],
                capture_output=True, text=True, errors="replace", timeout=120,
            )
            rel = (found.stdout or "").split("\n")[0].strip()
            if not rel:
                continue
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            bodies.append(f"/* {rel} */\n" + text[:9000])
            if len(bodies) >= 2:
                break
    shutil.rmtree(root, ignore_errors=True)
    if not bodies:
        return "", ["crash path functions not found in the vulnerable tree"]

    source = "\n\n".join(bodies)[:14000]
    grounding = content_words(source)
    rejected: list[str] = []
    for _ in range(3):
        response = _request(
            api_url=api_url, api_key=api_key, timeout=timeout,
            payload={"model": model, "messages": [{
                "role": "user",
                "content": SOURCE_PROMPT.format(
                    source=source, vclass=vclass, frames=", ".join(frames)),
            }]},
        )
        content = (
            response.get("choices", [{}])[0].get("message", {}).get("content") or ""
        )
        try:
            text = str(_extract_json(content).get("description") or "").strip()
        except (ValueError, AttributeError):
            text = ""
        if not text:
            rejected.append("declined")
            continue
        if LEAK.search(text):
            rejected.append("leak: " + text[:110])
            continue
        if grounding and not (content_words(text) & grounding):
            rejected.append("ungrounded: " + text[:110])
            continue
        return text, rejected
    return "", rejected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--model", default="gpt-5.4-2026-03-05")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args(argv)

    records = {r["sample_id"]: r for r in json.loads(SELECTION.read_text())}
    pool = [
        sid for sid in sorted(records)
        if classify(sid)[0] == "complete"
        and (GT_RESULTS / sid / "ground_truth.json").is_file()
    ]
    cybergym = cybergym_descriptions()
    messages = json.loads(FIX_MESSAGES.read_text()) if FIX_MESSAGES.is_file() else {}
    previous = json.loads(MANIFEST.read_text()) if MANIFEST.is_file() else {}

    entries: dict[str, dict[str, Any]] = {}
    needs: list[str] = []
    for sid in pool:
        original = str(records[sid].get("issue_description") or "")
        if sid in cybergym:
            entries[sid] = {"origin": "cybergym", "text": cybergym[sid]}
        elif is_clean(original) and len(original.strip()) >= 40:
            entries[sid] = {"origin": "reporter", "text": original.strip()}
        else:
            recovered = strip_crash_block(original)
            if len(recovered) >= 40 and is_clean(recovered):
                entries[sid] = {"origin": "crash_block_removed", "text": recovered}
            elif previous.get(sid, {}).get("origin") == "commit_derived" \
                    and previous[sid].get("text"):
                entries[sid] = previous[sid]
            else:
                needs.append(sid)

    ready = [s for s in needs if (messages.get(s) or {}).get("usable")]
    blocked = [s for s in needs if s not in ready]
    counts = Counter(e["origin"] for e in entries.values())
    print(f"pool {len(pool)}: " +
          ", ".join(f"{v} {k}" for k, v in counts.most_common()))
    print(f"  to restate from a fix commit message: {len(ready)}")
    print(f"  no usable source yet: {len(blocked)}")
    if args.audit_only:
        return 0

    api_key = os.environ.get(args.api_key_env or "", "")
    if ready and (not api_key or not args.api_url):
        print(f"restating needs --api-url and ${args.api_key_env}", file=sys.stderr)
        return 2

    todo = ready[: args.limit] if args.limit else ready
    for index, sid in enumerate(todo, 1):
        message = clean_commit_message(messages[sid]["message"])
        text, rejected = restate(message, api_url=args.api_url,
                                 api_key=api_key, model=args.model)
        if text:
            entries[sid] = {
                "origin": "commit_derived", "text": text,
                "derived_from": {"fix_commit": messages[sid]["fix_commit"],
                                 "repo": messages[sid]["repo"]},
                "rejected_attempts": len(rejected),
            }
            print(f"[{index}/{len(todo)}] {sid}: {text[:110]}", flush=True)
        else:
            entries[sid] = {"origin": "unavailable", "text": "",
                            "reason": "; ".join(rejected)[:300]}
            print(f"[{index}/{len(todo)}] {sid}: declined ({len(rejected)}x)",
                  flush=True)
    # Whatever the commit message could not describe, the diff may -- aimed by
    # the crash report at the hunk that matters.
    remaining = [
        sid for sid in pool
        if not (entries.get(sid) or {}).get("text")
    ]
    if args.limit:
        remaining = remaining[: args.limit]
    for index, sid in enumerate(remaining, 1):
        diff = read_diff(sid, records[sid])
        if not diff:
            entries[sid] = {"origin": "unavailable", "text": "",
                            "reason": "no fix diff and no usable message"}
            continue
        text, rejected = describe_from_diff(
            diff, vulnerability_class(records[sid]), crash_frames(records[sid], sid),
            api_url=args.api_url, api_key=api_key, model=args.model,
        )
        if text:
            entries[sid] = {"origin": "diff_derived", "text": text,
                            "rejected_attempts": len(rejected)}
            print(f"[diff {index}/{len(remaining)}] {sid}: {text[:105]}", flush=True)
        else:
            entries[sid] = {"origin": "unavailable", "text": "",
                            "reason": "; ".join(rejected)[:300]}
            print(f"[diff {index}/{len(remaining)}] {sid}: declined", flush=True)

    # Whatever the change itself does not contain, read out of the vulnerable
    # source the crash path names.
    still = [sid for sid in pool if not (entries.get(sid) or {}).get("text")]
    workdir = Path("/tmp/manifest_src")
    for index, sid in enumerate(still, 1):
        text, rejected = describe_from_source(
            sid, records[sid], vulnerability_class(records[sid]),
            crash_frames(records[sid], sid), api_url=args.api_url, api_key=api_key,
            model=args.model, workdir=workdir,
        )
        if text:
            entries[sid] = {"origin": "source_derived", "text": text,
                            "rejected_attempts": len(rejected)}
            print(f"[src {index}/{len(still)}] {sid}: {text[:105]}", flush=True)
        else:
            entries[sid] = {"origin": "unavailable", "text": "",
                            "reason": "; ".join(rejected)[:300]}
            print(f"[src {index}/{len(still)}] {sid}: {rejected[:1]}", flush=True)
    shutil.rmtree(workdir, ignore_errors=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stats: Counter = Counter()
    lengths: dict[str, list[int]] = {}
    for sid, entry in entries.items():
        if not entry.get("text"):
            stats["unavailable"] += 1
            continue
        (OUT_DIR / f"{sid}.txt").write_text(entry["text"] + "\n", encoding="utf-8")
        gt = json.loads((GT_RESULTS / sid / "ground_truth.json").read_text())
        entry["names_gt_root"] = names(
            entry["text"], normalize_function((gt.get("root_cause") or {}).get("function")))
        entry["names_gt_sink"] = names(
            entry["text"], normalize_function((gt.get("sink") or {}).get("function")))
        entry["length"] = len(entry["text"])
        origin = entry["origin"]
        stats[origin] += 1
        stats[f"{origin}|root"] += entry["names_gt_root"]
        stats[f"{origin}|sink"] += entry["names_gt_sink"]
        lengths.setdefault(origin, []).append(entry["length"])

    MANIFEST.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print("\n=== manifest ===")
    print(f"  {'origin':22s} {'n':>4s} {'median len':>10s} {'names root':>11s} "
          f"{'names sink':>11s}")
    for origin in ("cybergym", "reporter", "crash_block_removed", "commit_derived",
                   "diff_derived", "source_derived"):
        total = stats[origin]
        if not total:
            continue
        median = sorted(lengths[origin])[total // 2]
        print(f"  {origin:22s} {total:4d} {median:10d} "
              f"{stats[f'{origin}|root']:5d} ({stats[f'{origin}|root']/total:3.0%}) "
              f"{stats[f'{origin}|sink']:5d} ({stats[f'{origin}|sink']/total:3.0%})")
    if stats["unavailable"]:
        print(f"  {'unavailable':22s} {stats['unavailable']:4d}")
    print("\ncybergym is the reference row. A derived row that names the GT far "
          "more often is transcribing the answer.")
    print(f"written: {OUT_DIR}/ and {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
