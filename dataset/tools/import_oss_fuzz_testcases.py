#!/usr/bin/env python3
"""Recover public OSS-Fuzz testcases referenced by new-diverse OSV samples.

The migrated public issue page embeds an OSS-Fuzz download URL containing a
testcase_id.  This tool extracts that ID, optionally downloads the testcase,
and writes an audit report.  A downloaded testcase is deliberately not marked
as runnable: reproduction against the vulnerable revision is a separate step.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import warnings
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTION = REPO_ROOT / "dataset" / "selected_1000.json"
REPORT = REPO_ROOT / "dataset" / "new_diverse_oss_fuzz_poc_audit.json"
ISSUE_ID_RE = re.compile(r"(?:[?&]id=|issues/)(\d+)")
TESTCASE_ID_RE = re.compile(
    r"testcase_id(?:=|%3[dD]|\\u003[dD]|\\\\u003[dD])(\d+)"
)
USER_AGENT = "gt-generation-public-poc-import/1.0"


def fetch(url: str, *, attempts: int = 3, timeout: int = 45) -> tuple[bytes, str, dict[str, str]]:
    error = ""
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                return response.read(), response.geturl(), dict(response.headers.items())
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(error)


def issue_id_for(sample: dict) -> str:
    public_context = " ".join(
        str(sample.get(key) or "")
        for key in ("poc_source_url", "issue_description", "public_id")
    )
    match = ISSUE_ID_RE.search(public_context)
    return match.group(1) if match else ""


def testcase_ids_from_page(raw: bytes) -> list[str]:
    text = raw.decode("utf-8", errors="replace")
    return sorted(set(TESTCASE_ID_RE.findall(text)), key=int)


def decoded_issue_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            text = bytes(text, "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            pass
    return text


def issue_metadata_from_page(raw: bytes) -> dict[str, str]:
    text = decoded_issue_text(raw)
    fields = {
        "oss_fuzz_project": r"Project:\s*([^\n]+)",
        "oss_fuzz_engine": r"Fuzzing Engine:\s*([^\n]+)",
        "oss_fuzz_target_from_issue": r"Fuzz Target:\s*([^\n]+)",
        "oss_fuzz_job": r"Job Type:\s*([^\n]+)",
        "oss_fuzz_platform": r"Platform Id:\s*([^\n]+)",
        "oss_fuzz_sanitizer": r"Sanitizer:\s*([^\n]+)",
    }
    metadata: dict[str, str] = {}
    for key, pattern in fields.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            metadata[key] = match.group(1).strip()
    return metadata


def testcase_filename_from_response(final_url: str, headers: dict[str, str]) -> str:
    disposition = next(
        (value for key, value in headers.items() if key.lower() == "content-disposition"),
        "",
    )
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip())
    query = parse_qs(urlparse(final_url).query)
    for value in query.get("response-content-disposition", []):
        match = re.search(r'filename="?([^";]+)"?', unquote(value), re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def target_from_testcase_filename(filename: str, testcase_id: str) -> str:
    if not filename:
        return ""
    stem = filename.rsplit("/", 1)[-1]
    stem = re.sub(r"\.[A-Za-z0-9_.-]+$", "", stem)
    match = re.match(
        rf"clusterfuzz-testcase(?:-minimized)?-(.+)-{re.escape(testcase_id)}$",
        stem,
    )
    return match.group(1) if match else ""


def fetch_osv(public_id: str) -> dict:
    if not public_id:
        return {}
    raw, _, _ = fetch(f"https://api.osv.dev/v1/vulns/{public_id}")
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def osv_revision_metadata(public_id: str) -> dict[str, str]:
    data = fetch_osv(public_id)
    metadata: dict[str, str] = {}
    for affected in data.get("affected") or []:
        package = affected.get("package") or {}
        if package.get("ecosystem") and package.get("ecosystem") != "OSS-Fuzz":
            continue
        db_specific = affected.get("database_specific") or {}
        fixed_range = str(db_specific.get("fixed_range") or "")
        introduced_range = str(db_specific.get("introduced_range") or "")
        if fixed_range:
            metadata["oss_fuzz_fixed_range"] = fixed_range
            left, _, right = fixed_range.partition(":")
            if left:
                metadata["oss_fuzz_last_known_bad_commit"] = left
            if right:
                metadata["oss_fuzz_first_known_good_commit"] = right
        if introduced_range:
            metadata["oss_fuzz_introduced_range"] = introduced_range
            left, _, right = introduced_range.partition(":")
            if left:
                metadata["oss_fuzz_last_known_good_commit"] = left
            if right:
                metadata["oss_fuzz_first_known_bad_commit"] = right
        if metadata:
            metadata["oss_fuzz_revision_source"] = "osv.dev affected.database_specific"
            break
    return metadata


def looks_like_html(raw: bytes) -> bool:
    prefix = raw[:2048].lstrip().lower()
    return (
        prefix.startswith(b"<!doctype html")
        or prefix.startswith(b"<html")
        or b"<title>sign in" in prefix
    )


def download_testcase(sample_id: str, testcase_id: str) -> dict:
    url = f"https://oss-fuzz.com/download?testcase_id={testcase_id}"
    raw, final_url, headers = fetch(url)
    if not raw:
        raise RuntimeError("empty response")
    if looks_like_html(raw):
        raise RuntimeError("download returned HTML instead of a testcase")

    destination = REPO_ROOT / "dataset" / "pocs" / sample_id / "poc"
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".poc-download-", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass

    filename = testcase_filename_from_response(final_url, headers)
    target = target_from_testcase_filename(filename, testcase_id)
    return {
        "download_url": url,
        "final_download_url": final_url,
        "testcase_filename": filename,
        "oss_fuzz_target": target,
        "poc_path": str(destination.relative_to(REPO_ROOT / "dataset")),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def inspect_sample(sample: dict, download: bool) -> dict:
    sample_id = str(sample.get("sample_id") or "")
    issue_id = issue_id_for(sample)
    result = {
        "sample_id": sample_id,
        "public_id": sample.get("public_id"),
        "project": sample.get("project"),
        "issue_id": issue_id or None,
        "issue_url": f"https://issues.oss-fuzz.com/issues/{issue_id}" if issue_id else None,
        "status": "missing_issue_id",
        "testcase_ids": [],
    }
    if not issue_id:
        return result

    try:
        raw, final_url, _ = fetch(str(result["issue_url"]))
        testcase_ids = testcase_ids_from_page(raw)
        result.update(issue_metadata_from_page(raw))
        result.update(osv_revision_metadata(str(sample.get("public_id") or "")))
        result["final_issue_url"] = final_url
        result["testcase_ids"] = testcase_ids
        if not testcase_ids:
            result["status"] = "no_public_testcase_id"
            return result
        result["status"] = "testcase_id_found"
        if download:
            # OSS-Fuzz reports normally expose one minimized testcase.  If an
            # issue contains more than one ID, retain all IDs in the report and
            # use the first stable numeric ID as this sample's canonical PoC.
            result.update(download_testcase(sample_id, testcase_ids[0]))
            if not result.get("oss_fuzz_target"):
                issue_target = result.get("oss_fuzz_target_from_issue")
                if issue_target:
                    result["oss_fuzz_target"] = issue_target
            result["status"] = "downloaded"
    except RuntimeError as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, default=SELECTION)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--update-selection", action="store_true")
    args = parser.parse_args()
    if args.update_selection and not args.download:
        parser.error("--update-selection requires --download")

    selection_path = args.selection.resolve()
    samples = json.loads(selection_path.read_text(encoding="utf-8"))
    if not isinstance(samples, list):
        raise SystemExit(f"selection must be a JSON list: {selection_path}")

    targets = [
        sample
        for sample in samples
        if sample.get("selection_group") == "new_diverse"
        and sample.get("source_dataset") == "OSV.dev:OSS-Fuzz"
    ]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(inspect_sample, sample, args.download): sample
            for sample in targets
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["sample_id"])

    by_status: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        by_status[status] = by_status.get(status, 0) + 1

    if args.update_selection:
        downloaded = {
            result["sample_id"]: result
            for result in results
            if result["status"] == "downloaded"
        }
        for sample in samples:
            result = downloaded.get(str(sample.get("sample_id") or ""))
            if not result:
                continue
            sample["poc_path"] = result["poc_path"]
            sample["poc_status"] = "downloaded_public_testcase"
            sample["poc_evidence_kind"] = "oss_fuzz_testcase"
            sample["poc_source_url"] = result["download_url"]
            sample["poc_sha256"] = result["sha256"]
            sample["poc_size"] = result["size"]
            sample["poc_runnable"] = False
            for key in (
                "oss_fuzz_project",
                "oss_fuzz_engine",
                "oss_fuzz_target",
                "oss_fuzz_target_from_issue",
                "oss_fuzz_job",
                "oss_fuzz_platform",
                "oss_fuzz_sanitizer",
                "oss_fuzz_fixed_range",
                "oss_fuzz_introduced_range",
                "oss_fuzz_last_known_good_commit",
                "oss_fuzz_first_known_bad_commit",
                "oss_fuzz_last_known_bad_commit",
                "oss_fuzz_first_known_good_commit",
                "oss_fuzz_revision_source",
                "testcase_filename",
            ):
                if result.get(key):
                    sample[key] = result[key]
            if result.get("oss_fuzz_last_known_bad_commit"):
                previous = sample.get("vulnerable_commit")
                sample["vulnerable_commit"] = result["oss_fuzz_last_known_bad_commit"]
                sample["vulnerable_commit_source"] = "oss_fuzz_last_known_bad_commit"
                if previous and previous != sample["vulnerable_commit"]:
                    sample["previous_vulnerable_commit"] = previous
            if result.get("oss_fuzz_first_known_good_commit"):
                sample["fix_commit"] = result["oss_fuzz_first_known_good_commit"]
        selection_path.write_text(
            json.dumps(samples, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    report = {
        "scope": "new_diverse OSV.dev:OSS-Fuzz",
        "selection": str(selection_path.relative_to(REPO_ROOT)),
        "target_count": len(targets),
        "download_requested": args.download,
        "selection_updated": args.update_selection,
        "status_counts": by_status,
        "results": results,
    }
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2))
    return 0 if not by_status.get("error") else 2


if __name__ == "__main__":
    raise SystemExit(main())
