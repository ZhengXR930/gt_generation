"""Thin, portable wrapper over the repo's reachability engine.

Roles call `python3 -m reachability.cli`, which resolves with `evaluator/`
on PYTHONPATH. This wrapper finds it relative to the repo root and delegates, so
any coding-agent CLI can just call
`python3 -m gt_toolkit reachability ...` from anywhere in the tree.
"""

from __future__ import annotations

import argparse
import shlex
import re
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    """Find the repo root by walking up until the evaluation engine is present.

    gt_toolkit/ lives at gt_generation/gt_toolkit/, while the reachability engine
    evaluator/ lives at the actual repo root one level above, so
    a fixed parents[] index is fragile — search instead.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "evaluator" / "reachability").exists():
            return parent
    # Fall back to the repo root (parent of gt_generation/).
    return here.parents[2]


def _has_sanitizer_finding(text: str) -> bool:
    try:
        from reachability.engine import parse_sanitizer_trace
    except ModuleNotFoundError:
        root = _repo_root()
        sys.path.insert(0, str(root / "evaluator"))
        from reachability.engine import parse_sanitizer_trace

    observed = parse_sanitizer_trace(text)
    return bool(observed.get("crash_type") or observed.get("sanitizer"))


def _engine_env(root: Path) -> dict[str, str]:
    eval_dir = root / "evaluator"
    missing = [str(eval_dir)] if not (eval_dir / "reachability").exists() else []
    if missing:
        raise FileNotFoundError(
            "reachability engine not found; expected evaluator/reachability under "
            f"{root} (missing: {', '.join(missing)})"
        )
    env = dict(os.environ)
    extra = str(eval_dir)
    env["PYTHONPATH"] = extra + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")




def _pad_separators(command: str) -> str:
    """Make shell separators their own words.

    shlex keeps punctuation attached, so "pipefail;" arrives as one token and the
    rest of the line looks like arguments to `set`.
    """
    command = command.replace("\r\n", "\n").replace("\n", " ; ")
    return re.sub(r"(\|\||&&|[;|&])", r" \1 ", command)


# Shell words that introduce a new command rather than being one.
_SEPARATORS = {";", "&&", "||", "|", "&"}
# Builtins Stage 01 tends to prefix; none of them is the program under test.
_PREAMBLE = {"set", "cd", "export", "ulimit", "source", ".", "exec", "time", "env"}


def _program_invocation(parts: list[str]) -> list[str]:
    """Reduce a shell line to the single command that names a real program.

    Splits on separators, skips segments that are only shell preamble, and stops
    at the first pipeline stage or redirection so the debugger receives an argv
    rather than a script.
    """
    segments: list[list[str]] = [[]]
    for part in parts:
        if part in _SEPARATORS:
            segments.append([])
            continue
        segments[-1].append(part)

    for segment in segments:
        words = list(segment)
        while words and (_ENV_ASSIGNMENT.match(words[0]) or words[0] in _PREAMBLE):
            head = words.pop(0)
            if head in {"time", "exec", "env"}:
                # These take the real command as their remaining words.
                continue
            if head in _PREAMBLE:
                # `set -o pipefail`, `cd /x`: the rest belongs to the builtin.
                words = []
                break
        if not words:
            continue
        # A redirection ends the argv; gdb has no shell to interpret it.
        cleaned: list[str] = []
        skip_next = False
        for word in words:
            if skip_next:
                skip_next = False
                continue
            if word in {">", ">>", "<", "2>", "&>"}:
                skip_next = True
                continue
            if word == "2>&1" or word.startswith(("2>", ">", "<")):
                continue
            cleaned.append(word)
        if cleaned:
            return cleaned
    return []


def derive_debug_command(result_dir: Path) -> str | None:
    """Recover the bare program and arguments Stage 01 used to reproduce.

    The recorded command carries sanitizer environment assignments and may be
    wrapped in build.sh; gdb --args wants neither.
    """
    report = result_dir / "reproduction_report.json"
    if not report.is_file():
        return None
    try:
        command = str(json.loads(report.read_text(encoding="utf-8")).get("command") or "")
        parts = shlex.split(_pad_separators(command))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if parts and parts[0].endswith("build.sh") and len(parts) >= 2:
        try:
            parts = shlex.split(_pad_separators(parts[1]))
        except ValueError:
            return None
    parts = _program_invocation(parts)
    if not parts:
        return None
    poc_names = {"/gt/poc", "poc", "./poc", str(result_dir / "poc")}
    argv = ["{poc}" if part in poc_names else part for part in parts]
    if "{poc}" not in argv:
        argv.append("{poc}")
    if argv and not argv[0].startswith("/"):
        # Stage 01 records the command relative to the build directory, but the
        # debugger is spawned as a grandchild process and gdb resolved it
        # against a different working directory ("./fuzz/xml: No such file or
        # directory", every breakpoint left pending). Anchor it instead of
        # depending on where the process happens to start.
        program = argv[0][2:] if argv[0].startswith("./") else argv[0]
        argv[0] = "/gt/_work/src/" + program
    return " ".join(argv)


def _sample_vulnerable_commit(result_dir: Path) -> str:
    for name in ("sample_info.json", "prepare_report.json"):
        path = result_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        commit = str(data.get("vulnerable_commit") or "")
        if commit:
            return commit
    gt_path = result_dir / "ground_truth.json"
    if gt_path.is_file():
        try:
            gt = json.loads(gt_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            gt = {}
        project = gt.get("project") if isinstance(gt.get("project"), dict) else {}
        commit = str(project.get("vulnerable_commit") or gt.get("vulnerable_commit") or "")
        if commit:
            return commit
    return ""


def _restore_vulnerable_source(result_dir: Path, timeout: int = 1800) -> dict[str, Any]:
    """Put repo-track reachability back on the vulnerable, uninstrumented build.

    Stage 04 runs both vulnerable and fixed instrumentation in the same result
    workspace.  A clean tree may therefore still be checked out at the fixed
    commit, and the root sanitizer trace may be stale.  Reachability is a
    vulnerable-target measurement, so make that state explicit and rebuild.
    """
    src = result_dir / "_work" / "src"
    status = {"reset": False, "checked_out": False, "rebuilt": False}
    if not (src / ".git").exists():
        return status

    before = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        capture_output=True, text=True, errors="replace",
    )
    if before.returncode == 0:
        status["before_commit"] = before.stdout.strip()

    vulnerable_commit = _sample_vulnerable_commit(result_dir)
    if not vulnerable_commit:
        status["error"] = "missing vulnerable_commit"
        return status
    status["target_commit"] = vulnerable_commit

    reset = subprocess.run(
        ["git", "-C", str(src), "reset", "--hard", "HEAD"],
        capture_output=True, text=True, errors="replace",
    )
    if reset.returncode != 0:
        status["error"] = reset.stderr[-300:]
        return status
    status["reset"] = True

    checkout = subprocess.run(
        ["git", "-C", str(src), "checkout", "-q", vulnerable_commit],
        capture_output=True, text=True, errors="replace",
    )
    if checkout.returncode != 0:
        status["error"] = checkout.stderr[-300:]
        return status
    status["checked_out"] = True

    # Rebuild with the command Stage 01 established, so the binary the debugger
    # attaches to is the one the GT line numbers belong to.
    report = result_dir / "reproduction_report.json"
    if not report.is_file():
        return status
    try:
        setup = str(json.loads(report.read_text(encoding="utf-8")).get("setup_command") or "")
    except (json.JSONDecodeError, OSError):
        return status
    if not setup.strip():
        return status

    build_sh = result_dir / "build.sh"
    inner = setup
    parts = shlex.split(setup) if setup else []
    if parts and parts[0].endswith("build.sh") and len(parts) >= 2:
        inner = parts[1]
    vulnerable_commit = _sample_vulnerable_commit(result_dir)
    if vulnerable_commit:
        inner = inner.replace("<COMMIT>", vulnerable_commit)
    proc = subprocess.run(
        [str(build_sh), inner], capture_output=True, text=True,
        errors="replace", timeout=timeout,
    )
    status["rebuilt"] = proc.returncode == 0
    if proc.returncode != 0:
        status["error"] = proc.stderr[-300:]
    return status


def _recorded_setup_inner(result_dir: Path) -> str:
    report = result_dir / "reproduction_report.json"
    if not report.is_file():
        return ""
    try:
        setup = str(json.loads(report.read_text(encoding="utf-8")).get("setup_command") or "")
    except (json.JSONDecodeError, OSError):
        return ""
    if not setup.strip():
        return ""
    try:
        parts = shlex.split(setup)
    except ValueError:
        return setup
    if parts and parts[0].endswith("build.sh") and len(parts) >= 2:
        return parts[1]
    return setup


def _optional_reachability_args(result_dir: Path) -> list[str]:
    """Return optional files that actually exist in this compacted GT package."""
    args: list[str] = []
    assertion_spec = result_dir / "candidate_assertions.json"
    if not assertion_spec.is_file():
        assertion_spec = result_dir / "verified_assertions.json"
    if assertion_spec.is_file():
        args += ["--assertion-spec", f"/gt/{assertion_spec.name}"]

    optional_files = {
        "--assertion-trace": "vulnerable_assertion_trace.txt",
        "--verified-invariants": "verified_invariants.json",
        "--sanitizer-trace": "sanitizer_trace.txt",
    }
    for flag, name in optional_files.items():
        if (result_dir / name).is_file():
            args += [flag, f"/gt/{name}"]
    return args


def _arvo_target_from_traces(result_dir: Path) -> str:
    """The binary libFuzzer reported running, taken from a saved trace."""
    for name in ("sanitizer_trace.txt", "default_crash_trace.txt",
                 "vulnerable_assertion_trace.txt"):
        path = result_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # libFuzzer announces the binary it is about to run; that line is exact.
        found = re.search(r"/out/([A-Za-z0-9_.-]+):\s+Running", text)
        if not found:
            # Otherwise a stack frame names it, with a +0x offset to strip.
            found = re.search(r"/out/([A-Za-z0-9_.-]+)(?:\+0x[0-9a-f]+)?\b", text)
        if found:
            return found.group(1)
    return ""


def _arvo_context(result_dir: Path) -> dict[str, str] | None:
    """Image, target and project for an ARVO package, or None if not ARVO.

    prepare_report.json is not relied on: a completed package has been compacted
    and no longer carries it.
    """
    report = result_dir / "prepare_report.json"
    data: dict[str, Any] = {}
    if report.is_file():
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    if data and str(data.get("track") or "") not in ("arvo", ""):
        return None

    sample_id = str(data.get("sample_id") or result_dir.name)
    info = result_dir / "sample_info.json"
    project = str(data.get("project") or "")
    if info.is_file():
        try:
            loaded = json.loads(info.read_text(encoding="utf-8"))
            sample_id = str(loaded.get("sample_id") or sample_id)
            project = project or str(loaded.get("project") or "")
        except (json.JSONDecodeError, OSError):
            pass

    arvo_id = str(data.get("arvo_id") or "")
    if not arvo_id:
        if not sample_id.startswith("arvo_"):
            return None
        arvo_id = sample_id[len("arvo_"):]
    if not arvo_id:
        return None

    return {
        "arvo_id": arvo_id,
        "image": str(data.get("vul_image") or f"n132/arvo:{arvo_id}-vul"),
        "target": str(data.get("target") or "") or _arvo_target_from_traces(result_dir),
        "project": project,
    }


def run_for_arvo(result_dir: Path, timeout: int = 2400) -> int:
    """Run the debugger inside the sample's own ARVO image.

    The image has the built target and the source but no gdb; installing it from
    the image's own archive is what makes the measurement possible at all.
    """
    context = _arvo_context(result_dir)
    if context is None:
        return 1
    target = context["target"]
    if target:
        binary = f"/out/{target}"
    else:
        # Nothing recorded it; if the image built exactly one target, that is it.
        binary = ("$(set -- /out/*; for f; do [ -x \"$f\" ] && [ -f \"$f\" ] && "
                  "echo \"$f\"; done | head -1)")

    repo_root = Path(__file__).resolve().parents[2]
    optional = {
        "--assertion-spec": "candidate_assertions.json",
        "--assertion-trace": "vulnerable_assertion_trace.txt",
        "--verified-invariants": "verified_invariants.json",
        "--sanitizer-trace": "sanitizer_trace.txt",
    }
    arguments = [
        "--gt", "/gt/ground_truth.json",
        "--codebase", '"$SRC_ROOT"',
        "--debug-command", f'"{binary} /gt/poc"',
        "--poc", "/gt/poc",
        "--out-dir", "/gt/reachability",
    ]
    for flag, name in optional.items():
        if (result_dir / name).is_file():
            arguments += [flag, f"/gt/{name}"]

    # Prefer the checkout named after the project; fall back to the first one.
    inner = (
        "set -e\n"
        "if ! command -v gdb >/dev/null 2>&1; then\n"
        "  apt-get update -qq >/dev/null 2>&1 || true\n"
        "  apt-get install -y -qq gdb >/dev/null 2>&1 || true\n"
        "fi\n"
        "command -v gdb >/dev/null 2>&1 || { echo 'no gdb in image' >&2; exit 3; }\n"
        f"SRC_ROOT=/src/{shlex.quote(context['project'])}\n"
        'if [ ! -d "$SRC_ROOT" ]; then\n'
        "  SRC_ROOT=$(for d in /src/*/; do [ -d \"$d/.git\" ] && { echo \"${d%/}\"; break; }; done)\n"
        "fi\n"
        'if [ -z "$SRC_ROOT" ] || [ ! -d "$SRC_ROOT" ]; then SRC_ROOT=/src; fi\n'
        "export PYTHONPATH=/repo/gt_generation:/repo\n"
        "python3 -m gt_toolkit reachability " + " ".join(arguments) + "\n"
        f"chown -R {os.getuid()}:{os.getgid()} /gt/reachability 2>/dev/null || true\n"
    )

    proc = subprocess.run(
        [
            "docker", "run", "--rm", "--entrypoint", "bash",
            *_proxy_environment(),
            "-v", f"{repo_root}:/repo:ro",
            "-v", f"{result_dir}:/gt",
            context["image"], "-lc", inner,
        ],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    produced = result_dir / "reachability" / "reachability_report.json"
    if produced.is_file():
        (result_dir / "reachability_report.json").write_text(
            produced.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(json.dumps({"reachability": "ran", "track": "arvo",
                          "returncode": proc.returncode}))
        return 0
    print(json.dumps({
        "reachability": "failed", "track": "arvo",
        "returncode": proc.returncode,
        "stderr": proc.stderr[-600:],
    }))
    return 1


def _proxy_environment() -> list[str]:
    """Forward whichever proxy variables are set; apt inside needs them."""
    forwarded: list[str] = []
    for name in ("http_proxy", "https_proxy", "no_proxy",
                 "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        value = os.environ.get(name)
        if value:
            forwarded += ["-e", f"{name}={value}"]
    return forwarded


def run_for_result_dir(result_dir: Path, timeout: int = 900) -> int:
    """Run reachability for one package, on whichever side of the wall it lives.

    Repo-track packages execute through their own build.sh so the tool, the
    target and gdb share one filesystem; ARVO keeps the direct invocation the
    agent already builds around its workspace container.
    """
    result_dir = result_dir.resolve()
    build_sh = result_dir / "build.sh"
    if not build_sh.is_file():
        print(json.dumps({"reachability": "skipped", "reason": "no build.sh"}))
        return 0
    if "gt-memory-env" not in build_sh.read_text(encoding="utf-8", errors="replace"):
        # ARVO: no build.sh wrapper, so run the debugger in the sample's image.
        return run_for_arvo(result_dir)
    debug_command = derive_debug_command(result_dir)
    if not debug_command:
        print(json.dumps({"reachability": "skipped", "reason": "no reproduction command"}))
        return 0

    restored = _restore_vulnerable_source(result_dir)
    arguments = [
        "PYTHONPATH=/repo/gt_generation:/repo",
        "python3", "-m", "gt_toolkit", "reachability",
        "--gt", "/gt/ground_truth.json",
        "--codebase", "/gt/_work/src",
    ]
    arguments.extend(_optional_reachability_args(result_dir))
    arguments.extend([
        "--sanitizer-command", shlex.quote(f"{debug_command} 2>&1"),
        "--debug-command", shlex.quote(debug_command),
        "--poc", "/gt/poc",
        "--out-dir", "/gt/reachability",
    ])
    inner = " ".join(arguments)
    proc = subprocess.run(
        [str(build_sh), inner], capture_output=True, text=True,
        errors="replace", timeout=timeout,
    )
    produced = result_dir / "reachability" / "reachability_report.json"
    if produced.is_file():
        # audit-package reads the report at the package root.
        (result_dir / "reachability_report.json").write_text(
            produced.read_text(encoding="utf-8"), encoding="utf-8"
        )
        produced_trace = result_dir / "reachability" / "sanitizer_trace.txt"
        if produced_trace.is_file():
            produced_text = produced_trace.read_text(
                encoding="utf-8", errors="replace"
            )
            if _has_sanitizer_finding(produced_text):
                (result_dir / "sanitizer_trace.txt").write_text(
                    produced_text,
                    encoding="utf-8",
                )
        print(json.dumps({
            "reachability": "ran",
            "returncode": proc.returncode,
            "source_restored": restored,
        }))
        return 0
    print(json.dumps({
        "reachability": "failed",
        "returncode": proc.returncode,
        "stderr": proc.stderr[-600:],
    }))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gt-toolkit reachability",
        description="Run R1-R5 PoC reachability against a ground_truth.json.",
    )
    parser.add_argument("--gt", type=Path)
    parser.add_argument("--poc", type=Path)
    parser.add_argument("--debug-command", help="Debug command with optional {poc} placeholder.")
    parser.add_argument(
        "--debug-wrapper",
        help="Run gdb through this command (e.g. the sample build.sh) as one shell word.",
    )
    parser.add_argument(
        "--debug-path-map",
        help="HOST=CONTAINER prefix rewrite for paths passed to --debug-wrapper.",
    )
    parser.add_argument("--sanitizer-command", help="Sanitizer command with optional {poc} placeholder.")
    parser.add_argument("--sanitizer-trace", type=Path)
    parser.add_argument("--codebase", type=Path)
    parser.add_argument("--assertion-spec", type=Path)
    parser.add_argument("--assertion-trace", type=Path)
    parser.add_argument("--verified-invariants", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("reachability_out"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--for-result-dir",
        type=Path,
        help=(
            "Run reachability for this package without an agent: recover the "
            "reproduction command and, on the repo track, execute the tool "
            "inside the sample image through its build.sh."
        ),
    )
    args = parser.parse_args(argv)
    if not args.for_result_dir and not args.gt:
        parser.error('--gt is required unless --for-result-dir is given')
    if args.for_result_dir:
        return run_for_result_dir(args.for_result_dir, timeout=max(args.timeout, 900))

    root = _repo_root()
    try:
        env = _engine_env(root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cmd = [sys.executable, "-m", "reachability.cli", "--gt", str(args.gt)]
    for flag, value in [
        ("--poc", args.poc),
        ("--debug-command", args.debug_command),
        ("--debug-wrapper", args.debug_wrapper),
        ("--debug-path-map", args.debug_path_map),
        ("--sanitizer-command", args.sanitizer_command),
        ("--sanitizer-trace", args.sanitizer_trace),
        ("--codebase", args.codebase),
        ("--assertion-spec", args.assertion_spec),
        ("--assertion-trace", args.assertion_trace),
        ("--verified-invariants", args.verified_invariants),
        ("--out-dir", args.out_dir),
        ("--timeout", args.timeout),
    ]:
        if value is not None:
            cmd += [flag, str(value)]

    proc = subprocess.run(cmd, env=env, cwd=str(root))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
