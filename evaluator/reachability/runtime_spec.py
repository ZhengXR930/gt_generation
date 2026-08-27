"""Compile durable evaluator-only execution contracts for reachability."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tarfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _remove_prefix(value: str, prefix: str) -> str:
    return value[len(prefix):] if value.startswith(prefix) else value


def _split_shell_sequence(command: str) -> list[str]:
    """Split top-level shell sequences without splitting quoted content."""
    pieces: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            index += 1
            continue
        if char in {";", "\n"} or command[index:index + 2] == "&&":
            piece = "".join(current).strip()
            if piece:
                pieces.append(piece)
            current = []
            index += 2 if command[index:index + 2] == "&&" else 1
            continue
        current.append(char)
        index += 1
    piece = "".join(current).strip()
    if piece:
        pieces.append(piece)
    return pieces


class RuntimeSpecError(RuntimeError):
    """The frozen package cannot currently reconstruct its target runtime."""


@dataclass(frozen=True)
class RuntimeSpec:
    sample_id: str
    backend: str
    image: str
    workdir: str
    executable: str
    arguments: list[str]
    environment: dict[str, str]
    input_placeholder: str
    source: str
    build_commands: list[str] = field(default_factory=list)
    build_workdir: str = "/gt/_work/src"
    source_repo: str = ""
    source_commit: str = ""
    run_timeout: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compile_runtime_spec(
    gt_dir: Path, *, require_artifacts: bool = True, prefer_frozen: bool = True
) -> RuntimeSpec:
    """Compile a minimal runtime contract without exposing it to the agent."""
    gt_dir = gt_dir.resolve()
    sample_id = gt_dir.name
    if sample_id.startswith("arvo_"):
        arvo_id = _remove_prefix(sample_id, "arvo_")
        return RuntimeSpec(
            sample_id=sample_id,
            backend="arvo_image",
            image=f"n132/arvo:{arvo_id}-vul",
            workdir="/",
            executable="/bin/arvo-resolved-target",
            arguments=["{poc}"],
            environment={"ASAN_OPTIONS": "detect_leaks=0"},
            input_placeholder="{poc}",
            source="arvo_image_convention",
        )

    frozen_path = gt_dir / "runtime_spec.json"
    if prefer_frozen and frozen_path.is_file():
        spec = _load_frozen_spec(
            frozen_path, sample_id, require_artifacts=False
        )
        if not require_artifacts:
            return spec
        _hydrate_runtime_archive(gt_dir)
        spec = _relocate_missing_executable(spec, gt_dir)
        spec = _unwrap_libtool_executable(spec, gt_dir)
        if not _runtime_executable_exists(gt_dir, spec):
            build_runtime_workspace(gt_dir)
            spec = _relocate_missing_executable(spec, gt_dir)
            spec = _unwrap_libtool_executable(spec, gt_dir)
        validate_runtime_spec(spec, gt_dir, require_artifacts=True)
        return spec

    report_path = gt_dir / "reproduction_report.json"
    source = "reproduction_report.json"
    if report_path.is_file():
        report = _load_json(report_path)
        command = _unwrap_reproduction_command(str(report.get("command") or ""))
    else:
        source, command = _fallback_runtime_command(gt_dir)
    if not command:
        raise RuntimeSpecError("reproduction report has no command")
    workdir, invocation = _select_invocation(command)
    environment, executable, arguments = _parse_invocation(invocation)
    workdir, executable = _normalize_gt_workdir_executable(workdir, executable)
    if not any("{poc}" in item for item in arguments):
        raise RuntimeSpecError("runtime invocation does not consume the PoC")
    image = _image_from_build_script(gt_dir / "build.sh")
    spec = RuntimeSpec(
        sample_id=sample_id,
        backend="local_workspace",
        image=image,
        workdir=workdir,
        executable=executable,
        arguments=arguments,
        environment=environment,
        input_placeholder="{poc}",
        source=source,
    )
    if require_artifacts:
        spec = _unwrap_libtool_executable(spec, gt_dir)
    validate_runtime_spec(spec, gt_dir, require_artifacts=require_artifacts)
    return spec


def validate_runtime_spec(
    spec: RuntimeSpec, gt_dir: Path, *, require_artifacts: bool = True
) -> None:
    if spec.backend == "arvo_image":
        return
    if spec.backend != "local_workspace":
        raise RuntimeSpecError(f"unsupported runtime backend: {spec.backend}")
    if not spec.image or not spec.workdir.startswith("/gt/"):
        raise RuntimeSpecError("local runtime image/workdir is invalid")
    if spec.build_commands and not spec.build_workdir.startswith("/gt/"):
        raise RuntimeSpecError("local runtime build_workdir is invalid")
    if spec.run_timeout < 0:
        raise RuntimeSpecError("local runtime run_timeout is invalid")
    if not require_artifacts:
        return
    if not (gt_dir / "_work" / "src").is_dir():
        if not (spec.source_repo and spec.source_commit):
            raise RuntimeSpecError("source workspace is missing after GT compaction")
    executable = container_path_on_host(gt_dir, spec.executable, spec.workdir)
    if not executable.is_file():
        if spec.build_commands:
            return
        raise RuntimeSpecError(f"runtime executable is missing: {executable}")
    if not executable.stat().st_mode & 0o111:
        if spec.build_commands:
            return
        raise RuntimeSpecError(f"runtime executable is not executable: {executable}")


def container_path_on_host(gt_dir: Path, value: str, workdir: str) -> Path:
    if value.startswith("/gt/"):
        return gt_dir / _remove_prefix(value, "/gt/")
    if value.startswith("/"):
        raise RuntimeSpecError(f"runtime path is outside /gt: {value}")
    host_workdir = gt_dir / _remove_prefix(workdir, "/gt/")
    return (host_workdir / value).resolve()


def write_runtime_spec(gt_dir: Path) -> Path:
    spec = compile_runtime_spec(gt_dir)
    path = gt_dir / "runtime_spec.json"
    path.write_text(
        json.dumps(spec.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def ensure_source_workspace(gt_dir: Path, spec: RuntimeSpec, *, timeout: int = 1800) -> None:
    """Restore git-ignored source worktrees for source-backed runtime specs."""
    if spec.backend != "local_workspace":
        return
    src = gt_dir / "_work" / "src"
    repo = spec.source_repo
    commit = spec.source_commit
    if not repo or not commit:
        sample_info = _load_json(gt_dir / "sample_info.json")
        repo = str(sample_info.get("repo") or sample_info.get("repo_url") or "").strip()
        commit = str(sample_info.get("vulnerable_commit") or "").strip()
    if _source_workspace_ready(src, commit=commit):
        return
    if not repo or not commit:
        raise RuntimeSpecError("source workspace is missing and repo/commit are unavailable")
    src.parent.mkdir(parents=True, exist_ok=True)
    tmp = src.parent / "src.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    if src.exists() and not _source_workspace_ready(src):
        shutil.rmtree(src, ignore_errors=True)
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                "--filter=blob:none",
                repo,
                str(tmp),
            ],
            check=True,
            timeout=timeout,
        )
        subprocess.run(
            ["git", "-C", str(tmp), "fetch", "--quiet", "--depth", "1", "origin", commit],
            check=True,
            timeout=timeout,
        )
        subprocess.run(
            ["git", "-C", str(tmp), "checkout", "--quiet", commit],
            check=True,
            timeout=300,
        )
        tmp.rename(src)
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeSpecError(f"failed to restore source workspace: {exc}") from exc


def _source_workspace_ready(src: Path, *, commit: str = "") -> bool:
    if not src.is_dir():
        return False
    try:
        if not any(src.iterdir()):
            return False
    except OSError:
        return False
    if not (src / ".git").exists():
        return False
    try:
        top = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "--show-toplevel"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        ).stdout.strip()
        if Path(top).resolve() != src.resolve():
            return False
        if commit:
            head = subprocess.run(
                ["git", "-C", str(src), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
            ).stdout.strip()
            return head == commit
        return True
    except Exception:
        return False


def runtime_spec_inner_command(spec: RuntimeSpec, *, poc_path: str = "/gt/poc") -> str:
    """Render the hidden runtime command used by host-side generation validation."""
    if spec.backend != "local_workspace":
        raise RuntimeSpecError(f"unsupported runtime backend: {spec.backend}")
    build_prefix = ""
    if spec.build_commands:
        executable_for_guard = spec.executable
        if not executable_for_guard.startswith("/"):
            executable_for_guard = f"{spec.workdir.rstrip('/')}/{executable_for_guard}"
        build_commands = []
        for command in spec.build_commands:
            if "/gt/runtime_support/ossfuzz_project/build.sh" in command:
                command = command.replace(
                    "bash /gt/runtime_support/ossfuzz_project/build.sh",
                    "if [ -f /gt/runtime_support/runtime_prelude.sh ]; then "
                    "bash /gt/runtime_support/runtime_prelude.sh; fi; "
                    "bash /gt/runtime_support/ossfuzz_project/build.sh",
                )
            build_commands.append(command)
        build_prelude = (
            "git config --global --add safe.directory /gt/_work/src 2>/dev/null || true; "
            "git config --global --add safe.directory '*' 2>/dev/null || true"
        )
        build_script = " && ".join(f"({command})" for command in build_commands)
        build_prefix = (
            f"if [ ! -x {shlex.quote(executable_for_guard)} ]; then "
            f"cd {shlex.quote(spec.build_workdir)} && {build_prelude} && {build_script} || exit $?; "
            "fi; "
        )
    run_env = " ".join(
        f"{key}={shlex.quote(value)}" for key, value in sorted(spec.environment.items())
    )
    executable = spec.executable if spec.executable.startswith("/") else f"./{spec.executable}"
    arguments = [
        item.replace(spec.input_placeholder, poc_path)
        for item in spec.arguments
    ]
    run_command = " ".join(shlex.quote(item) for item in [executable, *arguments])
    if spec.run_timeout > 0:
        timeout_prefix = ["timeout", "-s", "KILL", str(spec.run_timeout)]
        run_command = " ".join(shlex.quote(item) for item in timeout_prefix) + f" {run_command}"
    if run_env:
        run_command = f"{run_env} {run_command}"
    return f"{build_prefix}cd {shlex.quote(spec.workdir)} && {run_command}"


def remap_checkpoints_to_workspace(
    checkpoints: list[dict[str, Any]], gt_dir: Path
) -> list[dict[str, Any]]:
    """Relocate frozen GT statements after deterministic instrumentation shifts.

    Matching is only used to recover the executable source location.  The
    checkpoint kind and statement still come from GT; this is not semantic
    scoring or model-trace matching.
    """
    source_root = gt_dir / "_work" / "src"
    remapped: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        item = dict(checkpoint)
        source = _resolve_source_file(source_root, str(item.get("file") or ""))
        code = str(item.get("code") or "").strip()
        expected_line = _as_int(item.get("line"))
        if source is None or not code or expected_line is None:
            remapped.append(item)
            continue
        needle = _normalize_statement(code.splitlines()[0])
        candidates = [
            index
            for index, line in enumerate(
                source.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            )
            if needle and needle in _normalize_statement(line)
        ]
        if candidates:
            resolved_line = min(candidates, key=lambda value: abs(value - expected_line))
            if resolved_line != expected_line:
                item["gt_line"] = expected_line
                item["line"] = resolved_line
                item["runtime_location_remapped"] = True
        remapped.append(item)
    return remapped


def apply_checkpoint_lines_to_gt(
    gt: dict[str, Any], checkpoints: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return a scoring GT projected onto the current instrumented workspace."""
    import copy

    result = copy.deepcopy(gt)
    by_kind = {str(item.get("kind")): item for item in checkpoints}
    for field, kind in (
        ("source", "source"),
        ("root_cause", "root_cause_line"),
        ("sink", "sink_line"),
    ):
        checkpoint = by_kind.get(kind)
        if checkpoint and isinstance(result.get(field), dict):
            result[field]["line"] = checkpoint.get("line")
    admission = by_kind.get("parser_admitted")
    reachability = result.get("reachability_checkpoints") or {}
    if admission and isinstance(reachability.get("parser_admitted"), dict):
        parser = reachability["parser_admitted"]
        target = parser.get("admitted_location") or parser
        if isinstance(target, dict):
            target["line"] = admission.get("line")
    sink = by_kind.get("sink_line")
    sanitizer = result.get("sanitizer_ground_truth") or {}
    if sink:
        original_sink_line = _as_int(sink.get("gt_line") or gt.get("sink", {}).get("line"))
        for name in ("crash_location", "runtime_crash_location"):
            location = sanitizer.get(name)
            if not isinstance(location, dict) or not _same_source_location(location, sink):
                continue
            # Only remap sanitizer locations that represented the GT sink line.
            # Runtime crash locations can be adjacent or more precise than the
            # abstract sink checkpoint, and overwriting them breaks sanitizer
            # matching for local-workspace samples.
            if _as_int(location.get("line")) == original_sink_line:
                location["line"] = sink.get("line")
    return result



def _fallback_runtime_command(gt_dir: Path) -> tuple[str, str]:
    reachability_path = gt_dir / "reachability_report.json"
    if reachability_path.is_file():
        report = _load_json(reachability_path)
        command = report.get("debug_command", {}).get("command")
        if isinstance(command, list) and "--args" in command:
            index = command.index("--args")
            args = [str(item) for item in command[index + 1:]]
            if args:
                return "reachability_report.debug_command", " ".join(
                    shlex.quote(item).replace("/gt/poc", "{poc}") for item in args
                )
    gt_path = gt_dir / "ground_truth.json"
    if gt_path.is_file():
        payload = _load_json(gt_path)
        trigger = payload.get("poc", {}).get("trigger")
        if trigger:
            return "ground_truth.poc.trigger", _unwrap_reproduction_command(str(trigger))
    raise RuntimeSpecError("reproduction recipe is missing after GT compaction")


def _hydrate_runtime_archive(gt_dir: Path) -> None:
    if (gt_dir / "_work" / "src").is_dir():
        return
    archive = gt_dir / "runtime_work.tar.gz"
    if not archive.is_file():
        return
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (gt_dir / member.name).resolve()
            if not str(target).startswith(str(gt_dir.resolve())):
                raise RuntimeSpecError(f"runtime archive escapes sample directory: {member.name}")
        tar.extractall(gt_dir)


def build_runtime_workspace(gt_dir: Path) -> dict[str, Any]:
    commands = _runtime_build_commands(gt_dir)
    if not commands:
        return {"prepared": False, "built": False}
    spec_path = gt_dir / "runtime_spec.json"
    image = "gt-memory-env:latest"
    workdir = "/gt/_work/src"
    if spec_path.is_file():
        try:
            spec = _load_frozen_spec(spec_path, gt_dir.name, require_artifacts=False)
            image = spec.image or image
            workdir = spec.build_workdir or workdir
        except RuntimeSpecError:
            pass
    script = "set -euo pipefail\n" + "\n".join(commands)
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{gt_dir}:/gt",
            "-w", workdir,
            image,
            "bash", "-lc", script,
        ],
        check=True,
        timeout=1800,
    )
    return {"prepared": True, "built": True}


def _runtime_executable_exists(gt_dir: Path, spec: RuntimeSpec) -> bool:
    try:
        executable = container_path_on_host(gt_dir, spec.executable, spec.workdir)
    except RuntimeSpecError:
        return False
    return executable.is_file() and bool(executable.stat().st_mode & 0o111)


def _relocate_missing_executable(spec: RuntimeSpec, gt_dir: Path) -> RuntimeSpec:
    if spec.backend != "local_workspace" or _runtime_executable_exists(gt_dir, spec):
        return spec
    sample_info = _load_optional_json(gt_dir / "sample_info.json")
    target_name = str(sample_info.get("oss_fuzz_target") or "").strip()
    if target_name:
        target = gt_dir / "_out" / target_name
        if target.is_file() and target.stat().st_mode & 0o111:
            return replace(
                spec,
                executable=f"/gt/_out/{target_name}",
                source=spec.source + "+oss_fuzz_target",
            )
    basename = Path(spec.executable).name
    if not basename:
        return spec
    roots = [gt_dir / "_work" / "src", gt_dir / "_work", gt_dir / "_out"]
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(
                path for path in root.rglob(basename)
                if path.is_file() and path.stat().st_mode & 0o111
            )
    unique = list(dict.fromkeys(path.resolve() for path in candidates))
    if not unique:
        return spec
    workdir_root = (gt_dir / _remove_prefix(spec.workdir, "/gt/")).resolve()
    workdir_matches = [path for path in unique if _is_relative_to(path, workdir_root)]
    selected: Path | None = None
    if len(workdir_matches) == 1:
        selected = workdir_matches[0]
    elif len(unique) == 1:
        selected = unique[0]
    if selected is None:
        return spec
    return replace(
        spec,
        executable=f"/gt/{selected.relative_to(gt_dir.resolve()).as_posix()}",
        source=spec.source + "+basename_relocated",
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return _load_json(path)
    except RuntimeSpecError:
        return {}

def _load_frozen_spec(
    path: Path, sample_id: str, *, require_artifacts: bool
) -> RuntimeSpec:
    data = _load_json(path)
    try:
        build_commands = _string_list(data.get("build_commands"))
        if not build_commands:
            build_commands = _runtime_build_commands(path.parent)
        spec = RuntimeSpec(
            sample_id=str(data.get("sample_id") or sample_id),
            backend=str(data["backend"]),
            image=str(data["image"]),
            workdir=str(data["workdir"]),
            executable=str(data["executable"]),
            arguments=[str(item) for item in data["arguments"]],
            environment={str(k): str(v) for k, v in data.get("environment", {}).items()},
            input_placeholder=str(data.get("input_placeholder") or "{poc}"),
            source=str(data.get("source") or "runtime_spec.json"),
            build_commands=build_commands,
            build_workdir=str(data.get("build_workdir") or "/gt/_work/src"),
            source_repo=str(data.get("source_repo") or ""),
            source_commit=str(data.get("source_commit") or ""),
            run_timeout=int(data.get("run_timeout") or 0),
        )
    except (KeyError, TypeError) as exc:
        raise RuntimeSpecError(f"invalid frozen runtime spec: {exc}") from exc
    validate_runtime_spec(
        spec, path.parent, require_artifacts=require_artifacts
    )
    return spec


def _runtime_build_commands(gt_dir: Path) -> list[str]:
    path = gt_dir / "runtime_build.json"
    if not path.is_file():
        return []
    try:
        payload = _load_json(path)
    except RuntimeSpecError:
        return []
    commands = []
    for item in payload.get("commands") or []:
        if isinstance(item, dict):
            command = str(item.get("command") or "").strip()
        else:
            command = str(item).strip()
        if command:
            commands.append(command)
    return commands


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    raise RuntimeSpecError("expected build_commands to be a string or array")


def _unwrap_reproduction_command(command: str) -> str:
    if not command.strip():
        return ""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise RuntimeSpecError(f"invalid reproduction shell quoting: {exc}") from exc
    index = 0
    if parts and parts[0] == "env":
        index = 1
    while index < len(parts) and "=" in parts[index]:
        key, _value = parts[index].split("=", 1)
        if not _ENV_NAME.fullmatch(key):
            break
        index += 1
    if index < len(parts) and parts[index].endswith("build.sh") and index + 1 < len(parts):
        return parts[index + 1]
    for index in range(len(parts) - 2):
        if parts[index : index + 2] == ["bash", "-lc"]:
            return parts[index + 2]
    return command


def _select_invocation(command: str) -> tuple[str, str]:
    workdir = "/gt/_work/src"
    pieces = _split_shell_sequence(command)
    selected_index = -1
    for index, piece in enumerate(pieces):
        if "/gt/poc" in piece or "{poc}" in piece:
            selected_index = index
    if selected_index < 0:
        raise RuntimeSpecError("reproduction command has no /gt/poc invocation")
    for prior in reversed(pieces[:selected_index]):
        match = re.fullmatch(r"cd\s+(.+)", prior)
        if match:
            workdir = shlex.split(match.group(1))[0]
            break
    invocation = pieces[selected_index].split("|", 1)[0].strip()
    return workdir, invocation


def _normalize_gt_workdir_executable(workdir: str, executable: str) -> tuple[str, str]:
    if workdir == "/gt" and executable.startswith("./_work/src/"):
        return "/gt/_work/src", "./" + _remove_prefix(executable, "./_work/src/")
    if workdir == "/gt" and executable.startswith("/gt/_work/src/"):
        return "/gt/_work/src", "./" + _remove_prefix(executable, "/gt/_work/src/")
    return workdir, executable


def _parse_invocation(command: str) -> tuple[dict[str, str], str, list[str]]:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise RuntimeSpecError(f"invalid invocation shell quoting: {exc}") from exc
    redirection = next(
        (
            index
            for index, token in enumerate(tokens)
            if token.startswith(">") or re.match(r"^\d+>", token)
        ),
        len(tokens),
    )
    tokens = tokens[:redirection]
    environment: dict[str, str] = {}
    if tokens and tokens[0] == "env":
        tokens.pop(0)
    while tokens and "=" in tokens[0]:
        key, value = tokens.pop(0).split("=", 1)
        if not _ENV_NAME.fullmatch(key):
            break
        environment[key] = value
    if tokens and tokens[0] == "timeout":
        tokens.pop(0)
        while tokens and (tokens[0].startswith("-") or re.fullmatch(r"\d+[smhd]?", tokens[0])):
            option = tokens.pop(0)
            if option in {"-s", "--signal", "-k", "--kill-after"} and tokens:
                tokens.pop(0)
    if not tokens:
        raise RuntimeSpecError("runtime invocation has no executable")
    executable = tokens.pop(0)
    arguments = [token.replace("/gt/poc", "{poc}") for token in tokens]
    return environment, executable, arguments


def _image_from_build_script(path: Path) -> str:
    if not path.is_file():
        raise RuntimeSpecError("build.sh is missing")
    match = re.search(r"(?m)^IMAGE=(?P<image>[^\s]+)\s*$", path.read_text())
    if not match:
        raise RuntimeSpecError("build.sh does not declare IMAGE")
    return shlex.split(match.group("image"))[0]


def _unwrap_libtool_executable(spec: RuntimeSpec, gt_dir: Path) -> RuntimeSpec:
    """Point GDB at the ELF behind a generated libtool shell wrapper."""
    executable = container_path_on_host(gt_dir, spec.executable, spec.workdir)
    try:
        wrapper = executable.read_text(encoding="utf-8", errors="replace")
        header = wrapper.encode()[:128]
    except OSError:
        return spec
    if not header.startswith(b"#!"):
        return spec
    actual = executable.parent / ".libs" / executable.name
    if not actual.is_file() or not actual.stat().st_mode & 0o111:
        return spec
    if spec.executable.startswith("/gt/"):
        relative = actual.relative_to(gt_dir).as_posix()
        value = f"/gt/{relative}"
    else:
        workdir_host = gt_dir / _remove_prefix(spec.workdir, "/gt/")
        value = f"./{actual.relative_to(workdir_host).as_posix()}"
    environment = dict(spec.environment)
    library_match = re.search(
        r'(?m)^\s*LD_LIBRARY_PATH="(?P<prefix>[^"$]+):\$LD_LIBRARY_PATH"',
        wrapper,
    )
    if library_match:
        existing = environment.get("LD_LIBRARY_PATH", "")
        prefix = library_match.group("prefix")
        environment["LD_LIBRARY_PATH"] = (
            f"{prefix}:{existing}" if existing else prefix
        )
    return replace(
        spec,
        executable=value,
        environment=environment,
        source=spec.source + "+libtool_unwrapped",
    )


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeSpecError(f"expected JSON object: {path}")
    return data


def _resolve_source_file(source_root: Path, value: str) -> Path | None:
    normalized = value.replace("\\", "/").lstrip("/")
    candidates = [source_root / normalized]
    if normalized.startswith("src/"):
        candidates.append(source_root / _remove_prefix(normalized, "src/"))
    candidates.append(source_root / Path(normalized).name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    basename = Path(normalized).name
    matches = list(source_root.rglob(basename)) if basename else []
    suffix = _remove_prefix(normalized, "src/")
    return next(
        (item for item in matches if str(item).replace("\\", "/").endswith(suffix)),
        matches[0] if len(matches) == 1 else None,
    )


def _normalize_statement(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _same_source_location(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_file = str(left.get("file") or "").replace("\\", "/")
    right_file = str(right.get("file") or "").replace("\\", "/")
    left_function = str(left.get("function") or "")
    right_function = str(right.get("function") or "")
    return bool(
        left_file
        and right_file
        and (left_file.endswith(right_file) or right_file.endswith(left_file))
        and (
            not left_function
            or not right_function
            or left_function == right_function
        )
    )
