#!/usr/bin/env python3
"""Derive the repo track's OSS-Fuzz configuration from google/oss-fuzz.

Repo-track samples (SEC-bench / OSV / NVD) have no prebuilt image: they clone
the project and build it inside the shared gt-memory-env. Two things then decide
whether Stage 01 can reproduce anything.

  1. The entry point. A PoC from OSS-Fuzz is a libFuzzer testcase, meaningless
     unless it is fed to the right fuzz target. SEC-bench states the target in
     bug_report.md; OSV samples state only a crash type and crash state, so the
     target has to come from the project's OSS-Fuzz recipe.
  2. The build dependencies. Every repo-track sample shares one image, so a
     header one project needs and the image lacks fails that build outright --
     a missing libclang-rt-18-dev is what killed 8 of 9 OSV samples once.

This fetches projects/<name>/{project.yaml,build.sh,Dockerfile} for every
project still in the backlog, writes one config per project under
dataset/ossfuzz_project_config/ (read by gt_toolkit.prepare when it assembles a
bug_report.md), and reports which apt packages the recipes declare that
gt-memory-env does not install.

    python3 scripts/survey_ossfuzz_projects.py
    python3 scripts/survey_ossfuzz_projects.py --track osv --no-fetch
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "gt_generation"))

RAW = "https://raw.githubusercontent.com/google/oss-fuzz/master/projects/{p}/{f}"
CFG_DIR = REPO_ROOT / "dataset" / "ossfuzz_project_config"
MISSING_REPORT = REPO_ROOT / "dataset" / "ossfuzz_missing_packages.json"
IMAGE_DOCKERFILE = REPO_ROOT / "docker" / "gt-memory-env" / "Dockerfile"

# Samples occasionally carry the upstream repo name rather than the oss-fuzz
# project name. Order matters: the first that resolves wins.
ALIASES = {
    "behaviortree.cpp": ["behaviortreecpp", "behaviortree_cpp"],
    "libdwarf-code": ["libdwarf"],
    "pjproject": ["pjsip"],
    "rizin": ["radare2"],
    "php-src": ["php"],
}

# $OUT also receives dictionaries, seed corpora and options files.
NON_TARGET_SUFFIX = (".dict", ".options", ".zip", ".txt", ".so", ".conf", ".json", ".xml")

# Words inside an apt-get install line that are not package names.
APT_NOISE = {"install", "apt-get", "update", "y", "true", "false", "rm", "rf",
             "then", "fi", "do", "done", "echo", "cd", "set"}


def fetch(project: str, filename: str) -> str:
    try:
        req = urllib.request.Request(RAW.format(p=project, f=filename),
                                     headers={"User-Agent": "gt-ossfuzz-survey"})
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return ""


def resolve(project: str) -> tuple[str, str, str, str]:
    """(oss_fuzz_name, project.yaml, build.sh, Dockerfile); empty name if absent."""
    for candidate in [project, *ALIASES.get(project, [])]:
        yaml_text = fetch(candidate, "project.yaml")
        if yaml_text:
            return (candidate, yaml_text,
                    fetch(candidate, "build.sh"), fetch(candidate, "Dockerfile"))
    return "", "", "", ""


def parse_project_yaml(text: str) -> dict:
    out: dict = {}
    for key in ("language", "main_repo", "homepage"):
        match = re.search(rf"^{key}\s*:\s*(\S.*)$", text, re.M)
        if match:
            out[key] = match.group(1).strip().strip('"\'')
    for key in ("sanitizers", "fuzzing_engines", "architectures"):
        block = re.search(rf"^{key}\s*:\s*\n((?:\s*-\s*\S+\n?)+)", text, re.M)
        if block:
            out[key] = [v.rstrip(":") for v in re.findall(r"-\s*(\S+)", block.group(1))]
        else:
            inline = re.search(rf"^{key}\s*:\s*\[([^\]]*)\]", text, re.M)
            if inline:
                out[key] = [x.strip().strip('"\'') for x in inline.group(1).split(",") if x.strip()]
    return out


def parse_targets(build_sh: str) -> tuple[list[str], list[str]]:
    """(binaries installed into $OUT, harness sources build.sh compiles).

    Neither is complete on its own: a project that builds its targets in a loop
    or through a helper never names the binary textually. prepare closes the gap
    by grepping the checkout for LLVMFuzzerTestOneInput.
    """
    binaries = set()
    for token in re.findall(r"\$\{?OUT\}?/([A-Za-z0-9_.+-]+)", build_sh):
        if token.endswith(NON_TARGET_SUFFIX) or token in {".", ".."}:
            continue
        binaries.add(token)

    sources = set()
    for name in re.findall(r"\b([A-Za-z0-9_.+-]+\.(?:c|cc|cpp|cxx|rs|go))\b", build_sh):
        stem = name.rsplit(".", 1)[0]
        if len(stem) > 1 and ("fuzz" in stem.lower() or "harness" in stem.lower()):
            sources.add(name)
    return sorted(binaries), sorted(sources)


def apt_packages(dockerfile: str) -> list[str]:
    """Packages a Dockerfile installs with apt.

    Comment lines are stripped before backslash continuations are joined:
    Docker drops them, so a comment in the middle of a package list does not
    end the RUN instruction.
    """
    body = "\n".join(l for l in dockerfile.splitlines() if not l.lstrip().startswith("#"))
    joined = re.sub(r"\\\s*\n", " ", body)
    pkgs = set()
    for run in re.findall(r"apt-get(?:\s+[-\w=]+)*\s+install([^\n&|;]*)", joined):
        for token in run.split():
            if token.startswith("-") or "=" in token or "$" in token or token in APT_NOISE:
                continue
            if re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", token):
                pkgs.add(token)
    return sorted(pkgs)


def backlog_projects(track: str) -> Counter:
    from gt_status import classify
    selection = REPO_ROOT / "dataset" / "selected_1000.json"
    records = json.loads(selection.read_text(encoding="utf-8"))
    weight: Counter = Counter()
    for record in records:
        sid = str(record.get("sample_id") or "")
        if track != "all" and not sid.startswith(track):
            continue
        if classify(sid)[0] == "complete":
            continue
        project = str(record.get("project") or "").strip()
        if project:
            weight[project] += 1
    return weight


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--track", default="osv",
                    help="sample id prefix to survey, or 'all' (default: osv)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="reconcile packages from the stored configs without refetching")
    args = ap.parse_args()

    CFG_DIR.mkdir(parents=True, exist_ok=True)

    if not args.no_fetch:
        weight = backlog_projects(args.track)
        projects = sorted(weight)
        print(f"backlog: {sum(weight.values())} samples across {len(projects)} projects")
        with ThreadPoolExecutor(max_workers=10) as pool:
            resolved = dict(zip(projects, pool.map(resolve, projects)))

        unresolved = []
        for project in projects:
            name, yaml_text, build_sh, dockerfile = resolved[project]
            if not name:
                unresolved.append(project)
                continue
            binaries, sources = parse_targets(build_sh)
            config = {
                "project": project,
                "oss_fuzz_project": name,
                "source": f"https://github.com/google/oss-fuzz/tree/master/projects/{name}",
                "backlog_samples": weight[project],
                **parse_project_yaml(yaml_text),
                "fuzz_target_binaries": binaries,
                "harness_sources": sources,
                "apt_packages": apt_packages(dockerfile),
                "build_sh": build_sh,
                "project_yaml": yaml_text,
                "dockerfile": dockerfile,
            }
            (CFG_DIR / f"{project}.json").write_text(
                json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"resolved {len(projects) - len(unresolved)}/{len(projects)} projects")
        if unresolved:
            print(f"  no oss-fuzz project directory: {unresolved}")

    installed = set(apt_packages(IMAGE_DOCKERFILE.read_text(encoding="utf-8")))
    wanted: dict[str, list[str]] = {}
    for path in sorted(CFG_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for pkg in data.get("apt_packages") or []:
            wanted.setdefault(pkg, []).append(path.stem)

    missing = {p: v for p, v in sorted(wanted.items()) if p not in installed}
    print(f"\ngt-memory-env installs {len(installed)} packages; recipes declare "
          f"{len(wanted)}; {len(missing)} not installed")
    for pkg, users in sorted(missing.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"  {len(users):2d}x  {pkg:32s} {', '.join(users[:6])}")
    MISSING_REPORT.write_text(json.dumps(missing, indent=2, sort_keys=True) + "\n",
                              encoding="utf-8")
    print(f"\nwritten: {CFG_DIR.relative_to(REPO_ROOT)}/, "
          f"{MISSING_REPORT.relative_to(REPO_ROOT)}")
    print("Not every missing package should be added -- some are virtual, "
          "transitional or conflicting; see the Dockerfile's exclusion list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
