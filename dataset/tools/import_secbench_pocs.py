#!/usr/bin/env python3
"""Import public runnable SEC-bench PoC/testcase artifacts.

The selected SEC-bench samples historically used dataset/pocs/<sid>/poc for
two different things:
  * the real testcase file, when one was locally available;
  * the public issue/Huntr/OSS-Fuzz report text, when the testcase was not.

The GT runner stages a single /gt/poc, so report text is actively harmful. This
script downloads public attachments referenced by SEC-bench bug reports,
extracts archives, chooses the testcase named by secb_sh's /testcase/<name>
where possible, and updates dataset/selected_1000.json to point at the selected
artifact.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SEC_BENCH_FILES = (
    "https://huggingface.co/datasets/SEC-bench/SEC-bench/resolve/main/data/eval-cve.jsonl",
    "https://huggingface.co/datasets/SEC-bench/SEC-bench/resolve/main/data/eval-oss.jsonl",
)

USER_AGENT = "Mozilla/5.0 Codex SEC-bench PoC importer"
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".gz")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", default="dataset/selected_1000.json")
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    selection_path = (repo_root / args.selection).resolve()
    selected = json.loads(selection_path.read_text())
    rows = _load_secbench_rows(Path(args.work_dir) if args.work_dir else None)
    by_instance = {row.get("instance_id") or row.get("benchmark_id"): row for row in rows}

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    processed = 0
    for sample in selected:
        if sample.get("source_family") != "secbench":
            continue
        if args.limit and processed >= args.limit:
            break
        processed += 1
        sid = str(sample.get("sample_id") or "")
        row = by_instance.get(sample.get("benchmark_id"))
        if not row:
            skipped.append({"sample_id": sid, "reason": "missing_secbench_row"})
            continue
        result = _import_one(repo_root, sample, row)
        if result.get("imported"):
            imported.append(result)
        else:
            skipped.append(result)

    selection_path.write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "selection": str(selection_path),
        "processed": processed,
        "imported": len(imported),
        "skipped": len(skipped),
        "imported_samples": imported,
        "skipped_samples": skipped,
    }, ensure_ascii=False, indent=2))
    return 0


def _load_secbench_rows(work_dir: Path | None) -> list[dict[str, Any]]:
    cache_dir = work_dir or Path(tempfile.gettempdir())
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for url in SEC_BENCH_FILES:
        name = "secbench_" + Path(urlparse(url).path).name
        path = cache_dir / name
        if not path.is_file():
            _download(url, path)
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return rows


def _import_one(repo_root: Path, sample: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    sid = str(sample.get("sample_id") or "")
    poc_dir = repo_root / "dataset" / "pocs" / sid
    poc_dir.mkdir(parents=True, exist_ok=True)
    _preserve_report_poc(poc_dir)

    expected = _expected_testcase_names(row.get("secb_sh") or "")
    urls = _candidate_urls(row.get("bug_report") or "")
    if not urls:
        return {"sample_id": sid, "imported": False, "reason": "no_public_artifact_url"}

    downloads = poc_dir / "downloads"
    downloads.mkdir(exist_ok=True)
    testcase_dir = poc_dir / "testcase"
    testcase_dir.mkdir(exist_ok=True)

    errors: list[str] = []
    for url in urls:
        try:
            downloaded = _download_url_to_dir(url, downloads)
            extracted = _extract_candidate_files(downloaded, testcase_dir)
        except Exception as exc:  # noqa: BLE001 - report every failed public candidate.
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            continue
        chosen = _choose_testcase(extracted, testcase_dir, expected)
        if not chosen:
            errors.append(f"{url}: no usable extracted file")
            continue
        rel = chosen.relative_to(repo_root / "dataset")
        sample["poc_path"] = str(rel)
        sample["poc_artifact_path"] = str(rel)
        sample["poc_runnable"] = True
        sample["poc_status"] = "downloaded_public_testcase"
        sample["poc_source_url"] = url
        if expected:
            sample["poc_expected_testcase_name"] = expected[0]
        sample["poc_asset_sha256"] = "sha256:" + hashlib.sha256(chosen.read_bytes()).hexdigest()
        return {
            "sample_id": sid,
            "imported": True,
            "url": url,
            "artifact": str(rel),
            "size": chosen.stat().st_size,
            "expected": expected,
        }
    return {
        "sample_id": sid,
        "imported": False,
        "reason": "download_or_extract_failed",
        "expected": expected,
        "urls": urls,
        "errors": errors[:5],
    }


def _preserve_report_poc(poc_dir: Path) -> None:
    poc = poc_dir / "poc"
    if not poc.is_file() or (poc_dir / "bug_report.md").exists():
        return
    raw = poc.read_bytes()
    text = raw[:4096].decode("utf-8", errors="replace")
    if (
        text.startswith("================= Bug Report")
        or text.startswith("DESCRIPTION\n")
        or text.lstrip().startswith("./")
    ):
        shutil.copy2(poc, poc_dir / "bug_report.md")


def _expected_testcase_names(script: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"/testcase/([^\s'\";|&)<>)]+)", script):
        name = match.group(1).strip()
        if not name or name == "model_patch.diff" or name == "poc_file":
            continue
        if name not in values:
            values.append(name)
    return values


def _candidate_urls(text: str) -> list[str]:
    values: list[str] = []
    for raw in re.findall(r"https?://[^\s)\]<>\"']+", text):
        url = raw.rstrip(".,")
        parsed = urlparse(url)
        lower_path = parsed.path.lower()
        candidate = ""
        if parsed.netloc == "oss-fuzz.com" and parsed.path == "/download" and parsed.query:
            candidate = url
        elif parsed.netloc == "github.com" and "/files/" in parsed.path:
            candidate = url
        elif parsed.netloc == "github.com" and "/blob/" in parsed.path:
            candidate = _github_blob_to_raw(url)
        elif parsed.netloc == "raw.githubusercontent.com":
            candidate = url
        elif lower_path.endswith(ARCHIVE_SUFFIXES):
            candidate = url
        if candidate and candidate not in values:
            values.append(candidate)
    return values


def _github_blob_to_raw(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    # /owner/repo/blob/branch/path...
    if len(parts) < 5 or parts[2] != "blob":
        return url
    owner, repo, _, branch = parts[:4]
    path = "/".join(parts[4:])
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"


def _download_url_to_dir(url: str, destination_dir: Path) -> Path:
    parsed = urlparse(url)
    name = Path(parsed.path).name or hashlib.sha256(url.encode()).hexdigest()[:16]
    if parsed.netloc == "oss-fuzz.com" and parsed.path == "/download":
        testcase_id = ""
        for part in parsed.query.split("&"):
            if part.startswith("testcase_id="):
                testcase_id = part.split("=", 1)[1]
                break
        if testcase_id:
            name = f"oss-fuzz-testcase-{testcase_id}"
    destination = destination_dir / name
    if destination.is_file() and destination.stat().st_size:
        return destination
    _download(url, destination)
    return destination


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        destination.write_bytes(response.read())


def _extract_candidate_files(downloaded: Path, testcase_dir: Path) -> list[Path]:
    out_dir = testcase_dir / downloaded.stem
    if downloaded.name.endswith(".tar.gz"):
        out_dir = testcase_dir / downloaded.name[:-7]
    elif downloaded.name.endswith(".tgz"):
        out_dir = testcase_dir / downloaded.name[:-4]
    if out_dir.is_file():
        return [out_dir]
    out_dir.mkdir(parents=True, exist_ok=True)

    lower = downloaded.name.lower()
    if zipfile.is_zipfile(downloaded):
        with zipfile.ZipFile(downloaded) as archive:
            archive.extractall(out_dir)
    elif tarfile.is_tarfile(downloaded):
        with tarfile.open(downloaded) as archive:
            archive.extractall(out_dir)
    elif lower.endswith(".gz") and not lower.endswith(".tar.gz"):
        target = out_dir / downloaded.name[:-3]
        with gzip.open(downloaded, "rb") as source:
            target.write_bytes(source.read())
    else:
        shutil.copy2(downloaded, out_dir / downloaded.name)
    return sorted(path for path in out_dir.rglob("*") if path.is_file())


def _choose_testcase(files: list[Path], testcase_dir: Path, expected: list[str]) -> Path | None:
    usable = [
        path for path in files
        if path.name not in {"README", "README.md", "model_patch.diff", "patch.diff"}
        and not path.name.startswith(".")
    ]
    if not usable:
        return None
    for name in expected:
        for path in usable:
            rel = str(path.relative_to(testcase_dir))
            if path.name == name or rel == name or rel.endswith("/" + name):
                return path
    return max(usable, key=lambda path: path.stat().st_size)


if __name__ == "__main__":
    raise SystemExit(main())
