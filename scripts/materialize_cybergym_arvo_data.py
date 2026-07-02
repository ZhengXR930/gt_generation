#!/usr/bin/env python3
"""Materialize CyberGym ARVO level1 data from ARVO Docker images and local metadata.

The CyberGym OpenHands adapter needs data/arvo/<id>/repo-vul.tar.gz and
description.txt for level1. The CyberGym server can still validate PoCs by
running n132/arvo:<id>-vul/fix directly, so this script only constructs the
agent-visible task data.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_TAR_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ninja_deps",
    ".ninja_log",
}
EXCLUDED_TAR_SUFFIXES = {
    ".o",
    ".a",
    ".so",
    ".dylib",
    ".pyc",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=ROOT / "evaluation_runs/cybergym_level1_50_manifest.json")
    ap.add_argument("--manifest-key", default="remaining_40")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "external/cybergym_data_subset/data")
    ap.add_argument("--force", action="store_true", help="Recreate files even when they already exist")
    ap.add_argument("--keep-images", action="store_true", help="Do not remove ARVO images after each sample")
    ap.add_argument(
        "--preextract-repo",
        action="store_true",
        help="Write data/arvo/<id>/repo-vul/src-vul/ instead of repo-vul.tar.gz for normalized local runs",
    )
    ap.add_argument("--task-id", action="append", help="Optional task id override; can repeat")
    args = ap.parse_args()

    task_ids = args.task_id or _tasks_from_manifest(args.manifest, args.manifest_key)
    samples = _load_sample_metadata()
    args.data_dir.mkdir(parents=True, exist_ok=True)

    failures: list[dict[str, str]] = []
    for task_id in task_ids:
        arvo_id = task_id.split(":", 1)[1] if ":" in task_id else task_id
        sample_id = f"arvo_{arvo_id}"
        sample = samples.get(sample_id, {})
        out_dir = args.data_dir / "arvo" / arvo_id
        out_dir.mkdir(parents=True, exist_ok=True)
        repo_tar = out_dir / "repo-vul.tar.gz"
        repo_dir = out_dir / "repo-vul"
        desc_path = out_dir / "description.txt"
        error_path = out_dir / "error.txt"
        patch_path = out_dir / "patch.diff"

        repo_ready = repo_dir.exists() if args.preextract_repo else repo_tar.exists()
        if repo_ready and desc_path.exists() and not args.force:
            print(f"[skip] {task_id}: level1 data already exists")
            continue

        image = f"n132/arvo:{arvo_id}-vul"
        print(f"[materialize] {task_id}: {image}")
        try:
            ensure_image(image)
            if args.preextract_repo:
                if repo_dir.exists():
                    shutil.rmtree(repo_dir)
                repo_dir.mkdir(parents=True, exist_ok=True)
                repo_src = repo_dir / "src-vul"
                copy_from_image(image, "/src", repo_src)
                prune_task_source_tree(repo_src)
                repo_tar.unlink(missing_ok=True)
            else:
                with tempfile.TemporaryDirectory(prefix=f"cybergym_arvo_{arvo_id}_") as td:
                    tmp = Path(td)
                    src_dir = tmp / "src-vul"
                    copy_from_image(image, "/src", src_dir)
                    write_tar(src_dir, repo_tar)
                if repo_dir.exists():
                    shutil.rmtree(repo_dir)
            desc = description_for_sample(sample, arvo_id)
            desc_path.write_text(desc.rstrip() + "\n", encoding="utf-8")
            if not error_path.exists() or args.force:
                err = sample.get("evidence") or sample.get("normalized_bug_description") or ""
                error_path.write_text(str(err).rstrip() + "\n", encoding="utf-8")
            local_patch = sample.get("patch_diff_path") or sample.get("patch_url_or_path")
            if local_patch and (not patch_path.exists() or args.force):
                src_patch = ROOT / "final_dataset" / str(local_patch)
                if src_patch.exists():
                    shutil.copy2(src_patch, patch_path)
            repo_artifact = repo_dir if args.preextract_repo else repo_tar
            print(f"[ok] {task_id}: wrote {repo_artifact} and {desc_path}")
        except Exception as exc:
            failures.append({"task_id": task_id, "error": str(exc)})
            print(f"[fail] {task_id}: {exc}")
        finally:
            if not args.keep_images:
                subprocess.run(["docker", "rmi", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    report = {
        "data_dir": str(args.data_dir),
        "task_count": len(task_ids),
        "failure_count": len(failures),
        "failures": failures,
    }
    report_path = args.data_dir / "materialize_arvo_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


def _tasks_from_manifest(path: Path, key: str) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get(key, data) if isinstance(data, dict) else data
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(item if item.startswith("arvo:") else f"arvo:{item}")
        else:
            out.append(item["task_id"])
    return out


def _load_sample_metadata() -> dict[str, dict[str, Any]]:
    samples: dict[str, dict[str, Any]] = {}
    for path in [
        ROOT / "selected_samples_json/cybergym_overlap_50_effective.json",
        ROOT / "final_dataset/all_samples.json",
    ]:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("samples", [])
        for item in items:
            sample_id = item.get("local_sample_id") or item.get("sample_id")
            if sample_id:
                samples[str(sample_id)] = item
    for path in (ROOT / "gt_results").glob("arvo_*/source_sample.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        sample_id = item.get("local_sample_id") or item.get("sample_id") or path.parent.name
        samples[str(sample_id)] = {**samples.get(str(sample_id), {}), **item}
    return samples


def description_for_sample(sample: dict[str, Any], arvo_id: str) -> str:
    for key in ("original_bug_description", "normalized_bug_description", "vulnerable_ref", "evidence"):
        value = sample.get(key)
        if value:
            return str(value)
    return f"OSS-Fuzz issue {arvo_id}: ARVO/CyberGym memory-safety vulnerability. See local GT metadata for details."


def ensure_image(image: str) -> None:
    if subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return
    pulled = subprocess.run(["docker", "pull", image])
    if pulled.returncode == 0:
        return
    archive = Path(tempfile.gettempdir()) / (image.replace("/", "__").replace(":", "__") + ".oci.tar")
    subprocess.check_call([str(ROOT / "scripts/download_dockerhub_oci.py"), image, "--out", str(archive)])
    try:
        subprocess.check_call(["docker", "load", "-i", str(archive)])
    finally:
        archive.unlink(missing_ok=True)
    if subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        loaded_id = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        for line in loaded_id.splitlines():
            name, _, image_id = line.partition(" ")
            if name == image and image_id:
                subprocess.run(["docker", "tag", image_id, image], check=False)
                break
    subprocess.check_call(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL)


def copy_from_image(image: str, src: str, dst: Path) -> None:
    name = f"materialize_{image.replace('/', '_').replace(':', '_')}_{os.getpid()}"
    subprocess.check_call(["docker", "create", "--name", name, image], stdout=subprocess.DEVNULL)
    try:
        subprocess.check_call(["docker", "cp", f"{name}:{src}", str(dst)])
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_tar(src_dir: Path, out_tar: Path) -> None:
    out_tar.parent.mkdir(parents=True, exist_ok=True)
    tmp_tar = out_tar.with_suffix(out_tar.suffix + ".tmp")
    with tarfile.open(tmp_tar, "w:gz") as tar:
        for path in sorted(src_dir.rglob("*")):
            if should_exclude_from_task_tar(path, src_dir):
                continue
            tar.add(path, arcname=Path("src-vul") / path.relative_to(src_dir))
    tmp_tar.replace(out_tar)


def should_exclude_from_task_tar(path: Path, src_dir: Path) -> bool:
    rel = path.relative_to(src_dir)
    parts = set(rel.parts)
    if parts & EXCLUDED_TAR_DIRS:
        return True
    if path.is_file() and path.suffix in EXCLUDED_TAR_SUFFIXES:
        return True
    if path.name.endswith((".tmp", ".part", ".log")):
        return True
    return False


def prune_task_source_tree(src_dir: Path) -> None:
    for path in sorted(src_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path == src_dir:
            continue
        if should_exclude_from_task_tar(path, src_dir):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
