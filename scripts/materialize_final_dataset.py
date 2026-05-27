#!/usr/bin/env python3
"""Materialize selected samples into a uniform dataset directory.

Every sample directory gets:
  - poc or poc/
  - patch.diff
  - trigger.json
  - run.sh
  - source_sample.json

ARVO samples are processed serially and images are removed immediately after
extracting /tmp/poc and /bin/arvo.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
import urllib.parse
import zipfile
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "selected_samples_json" / "all_samples.json"
OUT = ROOT / "final_dataset"
SHA_RE = re.compile(r"\b[0-9a-fA-F]{40}\b")


def run(cmd: list[str], timeout: int = 300) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except Exception as exc:
        return False, str(exc)


def first_stdout_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("WARNING:"):
            return line
    return ""


def sample_id(row: dict) -> str:
    prefix = {
        "ARVO-Meta": "arvo",
        "SEC-bench:cve": "secbench_cve",
        "SEC-bench:oss": "secbench_oss",
        "OSV.dev:OSS-Fuzz": "osv_ossfuzz",
        "OSV.dev:GIT": "osv_git",
        "NIST NVD": "nvd",
        "GitHub Advisory Database": "ghsa",
    }.get(row["source_dataset"], "sample")
    raw = row["benchmark_id"].replace("/", "_").replace(":", "_")
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)
    return f"{prefix}_{raw}"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_json(path: Path, data: dict | list) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def copy_or_write_patch(row: dict, sample_dir: Path) -> tuple[str, str]:
    dst = sample_dir / "patch.diff"
    src = row.get("patch_url_or_path", "")
    if src == "embedded_patch":
        item = secbench_item(row["benchmark_id"])
        dst.write_text(item.get("patch", ""))
        return "patch.diff", "embedded_secbench_patch"
    if src.startswith("/") and Path(src).exists():
        shutil.copyfile(src, dst)
        return "patch.diff", "local_patch_file"
    for base in (OUT, ROOT):
        rel = base / src
        if src and rel.exists():
            shutil.copyfile(rel, dst)
            return "patch.diff", "local_patch_file"
    ok, out = run(["git", "show", "--format=fuller", "--patch", row["fix_commit"]], timeout=20)
    if ok and out:
        dst.write_text(out)
        return "patch.diff", "git_show_local"
    url = row.get("fix_commit_reference") or src
    dst.write_text(f"Patch not materialized locally. Fix reference: {url}\nFix commit: {row['fix_commit']}\n")
    return "patch.diff", "reference_only"


_secbench_cache: dict[str, dict] | None = None


def load_secbench() -> dict[str, dict]:
    global _secbench_cache
    if _secbench_cache is not None:
        return _secbench_cache
    out = {}
    for path in (ROOT / "data_hf_sec_bench").glob("eval-*.jsonl"):
        for line in path.read_text(errors="ignore").splitlines():
            if line.strip():
                item = json.loads(line)
                out[item["instance_id"]] = item
    _secbench_cache = out
    return out


def secbench_item(instance_id: str) -> dict:
    return load_secbench().get(instance_id, {})


def extract_inline_poc(item: dict, command: str) -> tuple[str, str]:
    report = item.get("bug_report", "")
    fence_blocks = re.findall(r"```(?:[A-Za-z0-9_+-]+)?\n(.*?)```", report, flags=re.S)
    candidates = []
    for block in fence_blocks:
        b = block.strip("\n")
        if not b or any(skip in b.lower() for skip in ["addresssanitizer", "stack dump", "build", "copyright"]):
            continue
        score = 0
        if any(tok in b for tok in ["function", "#include", "<?", "MP4Box", "AAAA", "\\x", "import "]):
            score += 2
        if len(b) < 20000:
            score += 1
        candidates.append((score, b))
    if candidates:
        candidates.sort(key=lambda x: (-x[0], len(x[1])))
        content = candidates[0][1] + "\n"
    else:
        content = report
    path_match = re.search(r"/testcase/([A-Za-z0-9_.-]+)", command)
    original_name = path_match.group(1) if path_match else "poc"
    return original_name, content


def materialize_secbench(row: dict, sample_dir: Path) -> dict:
    item = secbench_item(row["benchmark_id"])
    command = row.get("repro_command") or ""
    original_name, content = extract_inline_poc(item, command)
    name = "poc"
    poc_path = sample_dir / name
    poc_path.write_text(content)
    (sample_dir / "secb.sh").write_text(item.get("secb_sh", ""))
    runnable = bool(command)
    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail
echo "This sample uses the SEC-bench harness."
echo "Expected project layout: vulnerable checkout/build mounted at the paths used by SEC-bench."
echo "Original repro command:"
cat <<'EOF'
{command}
EOF
exit 2
"""
    write_executable(sample_dir / "run.sh", run_sh)
    return {
        "trigger_type": "secbench_harness",
        "runnable": False,
        "runnable_reason": "Requires SEC-bench project build/container; PoC content is materialized best-effort from bug report or testcase reference.",
        "command": command,
        "local_poc_path": name,
        "original_testcase_name": original_name,
        "harness_path": "secb.sh",
    }


def materialize_arvo(row: dict, sample_dir: Path, pull: bool) -> dict:
    arvo_id = row["benchmark_id"]
    image = f"n132/arvo:{arvo_id}-vul"
    trigger = {
        "trigger_type": "arvo_docker",
        "runnable": True,
        "container_image": image,
        "command": "./run.sh",
        "container_command": "arvo",
        "container_poc_path": "/tmp/poc",
        "local_poc_path": "poc",
        "entry_script_path": "entrypoint.sh",
        "arvo_original_id": arvo_id,
    }
    if not pull:
        (sample_dir / "poc").write_text(f"Not downloaded. Source URL: {row['poc_or_pov_url']}\n")
        (sample_dir / "entrypoint.sh").write_text("")
        trigger["runnable"] = False
        trigger["runnable_reason"] = "ARVO Docker extraction skipped."
    else:
        ok, msg = run(["docker", "pull", image], timeout=1800)
        if not ok:
            (sample_dir / "poc").write_text(f"Docker pull failed for {image}\n{msg}\n")
            (sample_dir / "entrypoint.sh").write_text("")
            trigger["runnable"] = False
            trigger["runnable_reason"] = f"Docker pull failed: {msg[:300]}"
        else:
            container = f"gt_arvo_{arvo_id}_{os.getpid()}"
            ok, msg = run(["docker", "create", "--name", container, image], timeout=120)
            if ok:
                run(["docker", "cp", f"{container}:/tmp/poc", str(sample_dir / "poc")], timeout=120)
                run(["docker", "cp", f"{container}:/bin/arvo", str(sample_dir / "entrypoint.sh")], timeout=120)
                ok_pwd, pwd = run(["docker", "run", "--rm", "--entrypoint", "/bin/pwd", image], timeout=120)
                trigger["container_workdir"] = first_stdout_line(pwd) if ok_pwd else ""
                ok_script, script = run(["docker", "run", "--rm", "--entrypoint", "/bin/bash", image, "-lc", "sed -n '1,220p' /bin/arvo"], timeout=120)
                if ok_script:
                    m = re.search(r"(/\S+)\s+/tmp/poc", script)
                    if m:
                        trigger["target_command"] = m.group(0)
                        trigger["target_binary"] = m.group(1)
                run(["docker", "rm", "-f", container], timeout=60)
            else:
                trigger["runnable"] = False
                trigger["runnable_reason"] = f"Docker create failed: {msg[:300]}"
            run(["docker", "rmi", image], timeout=300)
    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail
docker run --rm \\
  -v "$PWD/poc:/tmp/poc:ro" \\
  {image} \\
  arvo
"""
    write_executable(sample_dir / "run.sh", run_sh)
    return trigger


def download_url(url: str, dst: Path) -> tuple[bool, str]:
    ok, msg = run(["curl", "-L", "--connect-timeout", "10", "--max-time", "120", "-o", str(dst), url], timeout=140)
    if ok and dst.exists() and dst.stat().st_size > 0:
        return True, msg
    return False, msg


def materialize_public_url(row: dict, sample_dir: Path) -> dict:
    url = row["poc_or_pov_url"]
    parsed = urllib.parse.urlparse(url)
    suffix = Path(parsed.path).suffix
    if "oss-fuzz.com/download" in url:
        name = "poc"
    elif suffix:
        name = "poc" + suffix
    else:
        name = "poc.html"
    dst = sample_dir / name
    ok, msg = download_url(url, dst)
    trigger_type = "public_poc_url"
    if "oss-fuzz.com/download" in url:
        trigger_type = "oss_fuzz_testcase"
    elif "exploit-db.com" in url:
        trigger_type = "exploit_db"
    elif "github.com" in url:
        trigger_type = "github_poc_reference"
    run_sh = """#!/usr/bin/env bash
set -euo pipefail
echo "PoC artifact is stored in this directory, but project-specific build/run command is not normalized."
echo "See trigger.json for source URL and expected crash evidence."
exit 2
"""
    write_executable(sample_dir / "run.sh", run_sh)
    return {
        "trigger_type": trigger_type,
        "runnable": False,
        "runnable_reason": "Project-specific trigger command is not normalized.",
        "source_url": url,
        "local_poc_path": name if ok else "",
        "download_ok": ok,
        "download_note": msg[:500],
    }


def materialize_sample(row: dict, base: Path, pull_arvo: bool) -> dict:
    sid = sample_id(row)
    sample_dir = base / "pocs" / sid
    sample_dir.mkdir(parents=True, exist_ok=True)
    patch_path, patch_kind = copy_or_write_patch(row, sample_dir)
    if row["source_dataset"] == "ARVO-Meta":
        trigger = materialize_arvo(row, sample_dir, pull_arvo)
    elif row["source_dataset"].startswith("SEC-bench"):
        trigger = materialize_secbench(row, sample_dir)
    else:
        trigger = materialize_public_url(row, sample_dir)
    trigger.update(
        {
            "sample_id": sid,
            "source_dataset": row["source_dataset"],
            "benchmark_id": row["benchmark_id"],
            "primary_category": row["primary_category"],
            "expected_crash_evidence": row.get("evidence", ""),
        }
    )
    write_json(sample_dir / "trigger.json", trigger)
    write_json(sample_dir / "source_sample.json", row)
    enriched = dict(row)
    enriched.update(
        {
            "sample_id": sid,
            "poc_dir": f"pocs/{sid}",
            "patch_diff_path": f"pocs/{sid}/{patch_path}",
            "patch_materialization_kind": patch_kind,
            "trigger_path": f"pocs/{sid}/trigger.json",
            "run_script_path": f"pocs/{sid}/run.sh",
            "artifact_status": "materialized",
            "trigger_runnable": trigger.get("runnable", False),
        }
    )
    local_poc = trigger.get("local_poc_path")
    if local_poc:
        enriched["poc_path"] = f"pocs/{sid}/{local_poc}"
    else:
        enriched["poc_path"] = ""
    return enriched


def load_materialized_sample(sample_dir: Path) -> dict | None:
    source_path = sample_dir / "source_sample.json"
    trigger_path = sample_dir / "trigger.json"
    if not source_path.exists() or not trigger_path.exists():
        return None
    row = json.loads(source_path.read_text())
    trigger = json.loads(trigger_path.read_text())
    sid = sample_dir.name
    row.update(
        {
            "sample_id": sid,
            "poc_dir": f"pocs/{sid}",
            "patch_diff_path": f"pocs/{sid}/patch.diff",
            "trigger_path": f"pocs/{sid}/trigger.json",
            "run_script_path": f"pocs/{sid}/run.sh",
            "artifact_status": "materialized",
            "trigger_runnable": trigger.get("runnable", False),
        }
    )
    local_poc = trigger.get("local_poc_path")
    row["poc_path"] = f"pocs/{sid}/{local_poc}" if local_poc else ""
    row["patch_materialization_kind"] = "existing"
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sources", default="", help="Comma-separated source_dataset filter")
    ap.add_argument("--pull-arvo", action="store_true", help="Pull ARVO Docker images and extract /tmp/poc and /bin/arvo")
    ap.add_argument("--resume", action="store_true", help="Reuse existing sample directories with trigger.json/source_sample.json")
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args()

    rows = json.loads(INPUT.read_text())
    if args.sources:
        wanted = set(args.sources.split(","))
        rows = [r for r in rows if r["source_dataset"] in wanted]
    if args.limit:
        rows = rows[: args.limit]
    out = Path(args.output)
    (out / "pocs").mkdir(parents=True, exist_ok=True)
    enriched = []
    for i, row in enumerate(rows, 1):
        sid = sample_id(row)
        existing = load_materialized_sample(out / "pocs" / sid) if args.resume else None
        if existing:
            print(f"[{i}/{len(rows)}] reuse {row['source_dataset']} {row['benchmark_id']}", flush=True)
            enriched.append(existing)
            continue
        print(f"[{i}/{len(rows)}] {row['source_dataset']} {row['benchmark_id']}", flush=True)
        enriched.append(materialize_sample(row, out, args.pull_arvo))
    archive = [r for r in enriched if r["dataset_group"] == "archive"]
    latest = [r for r in enriched if r["dataset_group"] == "latest"]
    summary = {
        "total": len(enriched),
        "archive": len(archive),
        "latest": len(latest),
        "by_source": dict(Counter(r["source_dataset"] for r in enriched)),
        "by_category": dict(Counter(r["primary_category"] for r in enriched)),
        "trigger_runnable": dict(Counter(str(r["trigger_runnable"]) for r in enriched)),
    }
    write_json(out / "all_samples.json", enriched)
    write_json(out / "archive_samples.json", archive)
    write_json(out / "latest_samples.json", latest)
    write_json(out / "summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
