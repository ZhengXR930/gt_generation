"""Compile durable evaluator-only execution contracts for reachability."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _remove_prefix(value: str, prefix: str) -> str:
    return value[len(prefix):] if value.startswith(prefix) else value


def _shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(str(item)) for item in argv)


def _split_shell_sequence(command: str) -> list[str]:
    """Split simple shell command lists on top-level ';' and '&&' only."""
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
        if char == ";":
            piece = "".join(current).strip()
            if piece:
                pieces.append(piece)
            current = []
            index += 1
            continue
        if command[index:index + 2] == "&&":
            piece = "".join(current).strip()
            if piece:
                pieces.append(piece)
            current = []
            index += 2
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

    if require_artifacts:
        hydrate_runtime_workspace(gt_dir)

    frozen_path = gt_dir / "runtime_spec.json"
    if prefer_frozen and frozen_path.is_file():
        spec = _load_frozen_spec(
            frozen_path, sample_id, require_artifacts=require_artifacts
        )
        if _frozen_spec_needs_reparse(spec):
            reparsed = _compile_spec_from_packaged_command(gt_dir, sample_id)
            if reparsed is not None:
                spec = reparsed
        if require_artifacts:
            spec = _resolve_runtime_artifact_spec(spec, gt_dir)
        return (
            _unwrap_libtool_executable(spec, gt_dir)
            if require_artifacts
            else spec
        )

    spec = _compile_spec_from_packaged_command(gt_dir, sample_id)
    if spec is None:
        raise RuntimeSpecError("runtime recipe is missing after GT compaction")
    spec = _normalize_local_workspace_spec(spec)
    if require_artifacts:
        spec = _unwrap_libtool_executable(spec, gt_dir)
    validate_runtime_spec(spec, gt_dir, require_artifacts=require_artifacts)
    return spec


def _compile_spec_from_packaged_command(
    gt_dir: Path, sample_id: str
) -> RuntimeSpec | None:
    command, source = _runtime_command_from_packaged_artifacts(gt_dir)
    if not command:
        return None
    workdir, invocation = _select_invocation(command)
    environment, executable, arguments = _parse_invocation(invocation)
    if not any("{poc}" in item for item in arguments):
        raise RuntimeSpecError("runtime invocation does not consume the PoC")
    image = _image_from_build_script(gt_dir / "build.sh")
    return RuntimeSpec(
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


def hydrate_runtime_workspace(gt_dir: Path) -> dict[str, Any]:
    """Materialize a compact local-workspace package before artifact checks."""
    src = gt_dir / "_work" / "src"
    if src.is_dir() and any(src.iterdir()):
        return {"prepared": True, "reused": True, "source": str(src)}
    try:
        from gt_toolkit.prepare import hydrate_runtime
    except ImportError:
        from gt_generation.gt_toolkit.prepare import hydrate_runtime
    report = hydrate_runtime(gt_dir)
    if not report.get("prepared"):
        raise RuntimeSpecError(str(report.get("reason") or "runtime hydration failed"))
    return report


def validate_runtime_spec(
    spec: RuntimeSpec, gt_dir: Path, *, require_artifacts: bool = True
) -> None:
    if spec.backend == "arvo_image":
        return
    if spec.backend != "local_workspace":
        raise RuntimeSpecError(f"unsupported runtime backend: {spec.backend}")
    if not spec.image or not spec.workdir.startswith("/gt/"):
        raise RuntimeSpecError("local runtime image/workdir is invalid")
    if not require_artifacts:
        return
    if not (gt_dir / "_work" / "src").is_dir():
        raise RuntimeSpecError("source workspace is missing after GT compaction")
    if _is_system_executable(spec.executable):
        return
    executable = container_path_on_host(gt_dir, spec.executable, spec.workdir)
    if not executable.is_file():
        raise RuntimeSpecError(f"runtime executable is missing: {executable}")
    if not executable.stat().st_mode & 0o111:
        raise RuntimeSpecError(f"runtime executable is not executable: {executable}")


def container_path_on_host(gt_dir: Path, value: str, workdir: str) -> Path:
    if value.startswith("/gt/"):
        return gt_dir / _remove_prefix(value, "/gt/")
    if value.startswith("/"):
        raise RuntimeSpecError(f"runtime path is outside /gt: {value}")
    host_workdir = gt_dir / _remove_prefix(workdir, "/gt/")
    return (host_workdir / value).resolve()


def write_runtime_spec(gt_dir: Path) -> Path:
    spec = compile_runtime_spec(
        gt_dir, require_artifacts=False, prefer_frozen=False
    )
    path = gt_dir / "runtime_spec.json"
    path.write_text(
        json.dumps(spec.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


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
        for name in ("crash_location", "runtime_crash_location"):
            location = sanitizer.get(name)
            if isinstance(location, dict) and _same_source_location(location, sink):
                location["line"] = sink.get("line")
    return result


def _load_frozen_spec(
    path: Path, sample_id: str, *, require_artifacts: bool
) -> RuntimeSpec:
    data = _load_json(path)
    try:
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
        )
    except (KeyError, TypeError) as exc:
        raise RuntimeSpecError(f"invalid frozen runtime spec: {exc}") from exc
    spec = _unwrap_env_executable(spec)
    validate_runtime_spec(spec, path.parent, require_artifacts=False)
    if require_artifacts:
        _resolve_runtime_artifact_spec(spec, path.parent)
    return spec


def _normalize_local_workspace_spec(spec: RuntimeSpec) -> RuntimeSpec:
    """Prefer /gt/_work/src as the mounted source workdir when possible."""
    if spec.backend != "local_workspace":
        return spec
    if spec.workdir == "/gt" and spec.executable.startswith("./_work/src/"):
        return replace(
            spec,
            workdir="/gt/_work/src",
            executable="./" + spec.executable[len("./_work/src/"):],
        )
    if spec.workdir == "/gt" and spec.executable.startswith("/gt/_work/src/"):
        return replace(
            spec,
            workdir="/gt/_work/src",
            executable="./" + spec.executable[len("/gt/_work/src/"):],
        )
    return spec


def _runtime_command_from_packaged_artifacts(gt_dir: Path) -> tuple[str, str]:
    for supplier in (
        _command_from_reproduction_report,
        _command_from_ground_truth_trigger,
        _command_from_reachability_report,
    ):
        command, source = supplier(gt_dir)
        if command:
            return command, source
    return "", ""


def _command_from_reproduction_report(gt_dir: Path) -> tuple[str, str]:
    report_path = gt_dir / "reproduction_report.json"
    if not report_path.is_file():
        return "", ""
    report = _load_json(report_path)
    command = _unwrap_reproduction_command(str(report.get("command") or ""))
    return (command, "reproduction_report.json") if command else ("", "")


def _command_from_ground_truth_trigger(gt_dir: Path) -> tuple[str, str]:
    gt_path = gt_dir / "ground_truth.json"
    if not gt_path.is_file():
        return "", ""
    gt = _load_json(gt_path)
    trigger = str((gt.get("poc") or {}).get("trigger") or "").strip()
    if not trigger or "\n" in trigger or len(trigger) >= 1000:
        return "", ""
    try:
        parts = shlex.split(trigger)
    except ValueError:
        return "", ""
    if (
        len(parts) == 2
        and parts[0] == "./build.sh"
        and ("/gt/poc" in parts[1] or "{poc}" in parts[1])
    ):
        return (
            parts[1].replace("{poc}", "/gt/poc"),
            "ground_truth.poc.trigger",
        )
    if (
        ("/gt/poc" in trigger or "{poc}" in trigger)
        and not re.search(
            r"\b(run|pass|saved|reproduced|trigger|input)\b", trigger, re.I
        )
    ):
        return (
            trigger.replace("{poc}", "/gt/poc"),
            "ground_truth.poc.trigger",
        )
    return "", ""


def _command_from_reachability_report(gt_dir: Path) -> tuple[str, str]:
    path = gt_dir / "reachability_report.json"
    if not path.is_file():
        return "", ""
    report = _load_json(path)
    debug_command = report.get("debug_command") or {}
    command = (
        debug_command.get("command") if isinstance(debug_command, dict) else None
    )
    if not isinstance(command, list):
        return "", ""
    try:
        args_index = command.index("--args")
    except ValueError:
        return "", ""
    argv = [str(item) for item in command[args_index + 1:] if str(item)]
    if not argv or not any("/gt/poc" in item or "{poc}" in item for item in argv):
        return "", ""
    return (
        _shell_join([item.replace("{poc}", "/gt/poc") for item in argv]),
        "reachability_report.debug_command",
    )


def _unwrap_reproduction_command(command: str) -> str:
    if not command.strip():
        return ""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise RuntimeSpecError(f"invalid reproduction shell quoting: {exc}") from exc
    if parts and parts[0].endswith("build.sh") and len(parts) >= 2:
        return parts[1]
    for index in range(len(parts) - 2):
        if parts[index : index + 2] == ["bash", "-lc"]:
            return parts[index + 2]
    return command


def _select_invocation(command: str) -> tuple[str, str]:
    workdir = "/gt/_work/src"
    pieces = _split_shell_sequence(command)
    selected_indexes = [
        index for index, piece in enumerate(pieces)
        if "/gt/poc" in piece or "{poc}" in piece
    ]
    selected_index = selected_indexes[-1] if selected_indexes else -1
    if selected_index < 0:
        raise RuntimeSpecError("reproduction command has no /gt/poc invocation")
    for prior in reversed(pieces[:selected_index]):
        match = re.fullmatch(r"cd\s+(.+)", prior)
        if match:
            workdir = shlex.split(match.group(1))[0]
            break
    invocation = _select_real_invocation_after_poc_copy(pieces, selected_index)
    if not invocation:
        invocation = _select_real_invocation(pieces[selected_index])
    return workdir, invocation


def _select_real_invocation_after_poc_copy(
    pieces: list[str], selected_index: int
) -> str:
    """Handle `cp /gt/poc tmp && real_target tmp` style repro commands."""
    selected = pieces[selected_index]
    try:
        copy_tokens = shlex.split(selected)
    except ValueError:
        return ""
    if len(copy_tokens) < 3 or copy_tokens[0] != "cp":
        return ""
    if not any(token in {"/gt/poc", "{poc}"} for token in copy_tokens[1:-1]):
        return ""
    copied_to = copy_tokens[-1]
    for piece in pieces[selected_index + 1:]:
        if _looks_like_setup_piece(piece):
            continue
        rewritten = piece.replace(shlex.quote(copied_to), "/gt/poc").replace(
            copied_to, "/gt/poc"
        )
        if "/gt/poc" in rewritten or "{poc}" in rewritten:
            return rewritten.split("|", 1)[0].strip()
    return ""


def _select_real_invocation(command: str) -> str:
    command = command.split("|", 1)[0].strip()
    pieces = _split_shell_sequence(command)
    selected = ""
    for piece in pieces:
        if "/gt/poc" not in piece and "{poc}" not in piece:
            continue
        try:
            tokens = shlex.split(piece)
        except ValueError:
            selected = piece
            continue
        if tokens and tokens[0] in {"cp", "cat", "printf", "echo"}:
            continue
        selected = piece
    return selected or command


def _looks_like_setup_piece(piece: str) -> bool:
    try:
        tokens = shlex.split(piece)
    except ValueError:
        return False
    return bool(tokens and tokens[0] in {"cp", "cat", "printf", "echo", "mkdir", "rm"})


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
    spec = RuntimeSpec(
        sample_id="",
        backend="local_workspace",
        image="",
        workdir="",
        executable=executable,
        arguments=arguments,
        environment=environment,
        input_placeholder="{poc}",
        source="",
    )
    spec = _unwrap_env_executable(spec)
    return spec.environment, spec.executable, spec.arguments


def _unwrap_env_executable(spec: RuntimeSpec) -> RuntimeSpec:
    """Turn `env A=B target ...` into target plus environment entries."""
    if spec.executable != "env":
        return spec
    environment = dict(spec.environment)
    arguments = list(spec.arguments)
    while arguments and "=" in arguments[0]:
        key, value = arguments.pop(0).split("=", 1)
        if not _ENV_NAME.fullmatch(key):
            break
        environment[key] = value
    if not arguments:
        return spec
    executable = arguments.pop(0)
    return replace(spec, executable=executable, arguments=arguments, environment=environment)


def _resolve_runtime_artifact_spec(spec: RuntimeSpec, gt_dir: Path) -> RuntimeSpec:
    """Resolve shell-derived executable placeholders against materialized artifacts."""
    if spec.backend != "local_workspace":
        return spec
    spec = _unwrap_env_executable(spec)
    executable = spec.executable
    if executable.startswith("$"):
        resolved = _resolve_shell_variable_executable(executable, gt_dir)
        if resolved:
            executable = resolved
    resolved_spec = replace(spec, executable=executable)
    try:
        validate_runtime_spec(resolved_spec, gt_dir, require_artifacts=True)
    except RuntimeSpecError:
        relocated = _unique_packaged_executable_with_basename(
            gt_dir, Path(executable).name, workdir=resolved_spec.workdir
        )
        if relocated is None:
            target_name = _oss_fuzz_target_name(gt_dir)
            if target_name and target_name != Path(executable).name:
                relocated = _unique_packaged_executable_with_basename(
                    gt_dir, target_name, workdir=resolved_spec.workdir
                )
        if relocated is None:
            raise
        relative = relocated.resolve().relative_to(gt_dir.resolve()).as_posix()
        resolved_spec = replace(
            resolved_spec,
            executable=f"/gt/{relative}",
            source=resolved_spec.source + "+basename_relocated",
        )
        validate_runtime_spec(resolved_spec, gt_dir, require_artifacts=True)
    return resolved_spec


def _unique_packaged_executable_with_basename(
    gt_dir: Path, basename: str, *, workdir: str = ""
) -> Path | None:
    """Find a moved frozen target only when the match is unambiguous."""
    if not basename or basename in {".", ".."}:
        return None
    candidates: list[Path] = []
    for root in (gt_dir / "_work", gt_dir / "_out"):
        if not root.exists():
            continue
        candidates.extend(
            path for path in root.rglob(basename)
            if path.is_file() and path.stat().st_mode & 0o111
        )
    root_candidate = gt_dir / basename
    if root_candidate.is_file() and root_candidate.stat().st_mode & 0o111:
        candidates.append(root_candidate)
    unique = sorted({path.resolve() for path in candidates})
    if workdir.startswith("/gt/"):
        workdir_host = (gt_dir / _remove_prefix(workdir, "/gt/")).resolve()
        in_workdir = []
        for path in unique:
            try:
                path.relative_to(workdir_host)
            except ValueError:
                continue
            in_workdir.append(path)
        if len(in_workdir) == 1:
            return in_workdir[0]
    return unique[0] if len(unique) == 1 else None


def _oss_fuzz_target_name(gt_dir: Path) -> str:
    """Return the sample-local official OSS-Fuzz target, when recorded."""
    sample_info = gt_dir / "sample_info.json"
    if not sample_info.is_file():
        return ""
    try:
        value = str(_load_json(sample_info).get("oss_fuzz_target") or "")
    except (OSError, json.JSONDecodeError, RuntimeSpecError):
        return ""
    value = Path(value).name
    return value if value not in {"", ".", ".."} else ""


def _resolve_shell_variable_executable(variable: str, gt_dir: Path) -> str:
    variable_name = variable.lstrip("${}").lstrip("$")
    if variable_name != "target":
        return ""
    name = _target_find_name_from_packaged_command(gt_dir)
    if not name:
        return ""
    for root in (gt_dir / "_work", gt_dir / "_out"):
        if not root.exists():
            continue
        matches = sorted(
            path for path in root.rglob(name)
            if path.is_file() and path.stat().st_mode & 0o111
        )
        if matches:
            match = matches[0].resolve()
            relative = match.relative_to(gt_dir.resolve()).as_posix()
            return f"/gt/{relative}"
    return ""


def _target_find_name_from_packaged_command(gt_dir: Path) -> str:
    command, _source = _runtime_command_from_packaged_artifacts(gt_dir)
    if not command:
        return ""
    match = re.search(
        r"\bfind\s+/gt/_work\b[^;&|]*\s-name\s+(?P<name>'[^']+'|\"[^\"]+\"|[^\s;&|]+)",
        command,
    )
    if not match:
        return ""
    return match.group("name").strip("'\"")


def _is_system_executable(value: str) -> bool:
    if not value or value.startswith(("/", "./", "../", "$")):
        return False
    return "/" not in value


def _frozen_spec_needs_reparse(spec: RuntimeSpec) -> bool:
    if spec.executable.startswith("$"):
        return True
    return spec.executable in {"cp", "cat", "printf", "echo", "mkdir", "rm"}


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
