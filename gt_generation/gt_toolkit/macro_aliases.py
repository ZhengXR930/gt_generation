"""Augment field_bindings.json aliases with per-sample constant macro forms."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import operator
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_DEFINE_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)(.*)$")
_COMMENT_RE = re.compile(r"/\*.*?\*/|//.*?$", re.DOTALL | re.MULTILINE)
_STRING_OR_CHAR_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_C_INT_SUFFIX_RE = re.compile(r"\b(0[xX][0-9A-Fa-f]+|\d+)(?:[uUlL]+)\b")
_NON_MACRO_IDENTIFIERS = {
    "sizeof",
    "alignof",
    "_Alignof",
    "true",
    "false",
    "NULL",
}
_C_TYPE_KEYWORDS = {
    "char",
    "const",
    "double",
    "enum",
    "float",
    "int",
    "long",
    "short",
    "signed",
    "size_t",
    "static",
    "struct",
    "uint16_t",
    "uint32_t",
    "uint64_t",
    "uint8_t",
    "uintptr_t",
    "unsigned",
    "void",
}


@dataclass(frozen=True)
class Macro:
    name: str
    value: str


@dataclass
class BindingUpdate:
    key: str
    source_file: str | None
    added: list[str]


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text).strip()


def _is_function_like_macro(rest: str) -> bool:
    return rest.startswith("(")


def _macro_body(rest: str) -> str:
    body = rest.strip()
    if not body:
        return "1"
    return _strip_comments(body).strip()


def _looks_safe_object_macro(value: str) -> bool:
    if not value:
        return False
    if "\\" in value:
        return False
    if any(token in value for token in ("++", "--", "=", ";", "{", "}")):
        return False
    if '"' in value or "'" in value:
        return False
    # Object-like constants may contain grouping parentheses, but a bare
    # identifier followed by '(' would be a function call or a function-like
    # macro body; leave those out of deterministic alias expansion.
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(", value):
        return False
    return bool(
        re.fullmatch(r"[A-Za-z0-9_\s()+\-*/%<>=!&|^~?:.,]+", value)
    )


def _looks_compile_time_macro_value(value: str) -> bool:
    text = _code_view(value)
    if not text:
        return False
    if any(token in text for token in ("->", ".", "[", "]", "++", "--", "=", ";", "{", "}")):
        return False
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(", text):
        return False
    for match in _IDENT_RE.finditer(text):
        name = match.group(0)
        if name in _NON_MACRO_IDENTIFIERS or name in _C_TYPE_KEYWORDS:
            continue
        if _is_strong_macro_token(name):
            continue
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_\s()+\-*/%<>=!&|^~?:,]+", text))


def _macro_has_constant_value(macro: Macro, macros: dict[str, Macro]) -> bool:
    if not _looks_compile_time_macro_value(macro.value):
        return False
    expanded = expand_macro_value(macro.value, macros)
    return _looks_compile_time_macro_value(expanded)


def _parse_macro_definitions(text: str) -> dict[str, Macro]:
    macros: dict[str, Macro] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = _DEFINE_RE.match(line)
        if not match:
            continue
        name, rest = match.groups()
        if _is_function_like_macro(rest):
            continue
        body = _macro_body(rest)
        if not _looks_safe_object_macro(body):
            continue
        macros[name] = Macro(name=name, value=body)
    return macros


def _blank_matches(pattern: re.Pattern[str], text: str) -> str:
    return pattern.sub(lambda match: " " * len(match.group(0)), text)


def _code_view(expr: str) -> str:
    return _blank_matches(_STRING_OR_CHAR_RE, _blank_matches(_COMMENT_RE, expr))


def _previous_nonspace(text: str, index: int) -> tuple[str, int]:
    pos = index - 1
    while pos >= 0 and text[pos].isspace():
        pos -= 1
    return (text[pos] if pos >= 0 else "", pos)


def _next_nonspace(text: str, index: int) -> tuple[str, int]:
    pos = index
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return (text[pos] if pos < len(text) else "", pos)


def _is_member_identifier(text: str, start: int) -> bool:
    prev, prev_pos = _previous_nonspace(text, start)
    if prev == ".":
        return True
    if prev == ">" and prev_pos > 0 and text[prev_pos - 1] == "-":
        return True
    if prev == ":" and prev_pos > 0 and text[prev_pos - 1] == ":":
        return True
    return False


def _is_strong_macro_token(name: str) -> bool:
    stripped = name.strip("_")
    if not stripped:
        return False
    if stripped.upper() == stripped and any(ch.isalpha() for ch in stripped):
        return True
    if name.startswith("k") and len(name) > 1 and name[1].isupper():
        return True
    return bool(re.fullmatch(r"[A-Z][A-Za-z0-9]*_[A-Za-z0-9_]+", name))


def _candidate_macro_tokens(expr: str) -> set[str]:
    tokens: set[str] = set()
    code = _code_view(expr)
    for match in _IDENT_RE.finditer(code):
        name = match.group(0)
        if name in _NON_MACRO_IDENTIFIERS:
            continue
        if _is_member_identifier(code, match.start()):
            continue
        next_char, _ = _next_nonspace(code, match.end())
        if next_char == "(":
            continue
        if _is_strong_macro_token(name):
            tokens.add(name)
    return tokens


def _run_preprocessor(source_file: Path, include_roots: list[Path]) -> dict[str, Macro]:
    compiler = "g++" if source_file.suffix.lower() in {".cc", ".cpp", ".cxx", ".hpp"} else "gcc"
    include_args: list[str] = []
    seen: set[Path] = set()
    for root in [source_file.parent, *include_roots]:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        include_args.extend(["-I", str(resolved)])
        for child in ("src", "include"):
            nested = resolved / child
            if nested.is_dir():
                include_args.extend(["-I", str(nested)])
    cmd = [compiler, "-E", "-dM", *include_args, str(source_file)]
    proc = subprocess.run(
        cmd,
        cwd=str(source_file.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{compiler} -E -dM failed")
    return _parse_macro_definitions(proc.stdout)


def _parse_static_macro_lines(lines: list[str]) -> dict[str, Macro]:
    macros: dict[str, Macro] = {}
    for line in lines:
        macros.update(_parse_macro_definitions(line))
    return macros


def _rg_define_lines(source_root: Path, tokens: set[str]) -> list[str]:
    if not tokens or shutil.which("rg") is None:
        return []
    pattern = r"^\s*#\s*define\s+(?:" + "|".join(re.escape(token) for token in sorted(tokens)) + r")\b"
    cmd = [
        "rg",
        "--no-heading",
        "-n",
        "--glob",
        "*.h",
        "--glob",
        "*.hh",
        "--glob",
        "*.hpp",
        "--glob",
        "*.hxx",
        "--glob",
        "*.c",
        "--glob",
        "*.cc",
        "--glob",
        "*.cpp",
        "--glob",
        "*.cxx",
        "--glob",
        "!build/**",
        "--glob",
        "!cmake-build-debug/**",
        "--glob",
        "!cmake-build-release/**",
        "--glob",
        "!node_modules/**",
        pattern,
        str(source_root),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode not in (0, 1):
        return []
    lines: list[str] = []
    for raw in proc.stdout.splitlines():
        parts = raw.split(":", 2)
        if len(parts) == 3:
            lines.append(parts[2])
    return lines


def _walk_define_lines(source_root: Path, tokens: set[str]) -> list[str]:
    token_needles = tuple(f"define {token}" for token in tokens)
    if not token_needles:
        return []
    suffixes = {".h", ".hh", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx"}
    skip_dirs = {
        ".git",
        "build",
        "cmake-build-debug",
        "cmake-build-release",
        "node_modules",
        "__pycache__",
    }
    lines: list[str] = []
    for dirpath, dirnames, filenames in os.walk(str(source_root)):
        dirnames[:] = [name for name in dirnames if name not in skip_dirs]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in suffixes:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "#define" in line and any(needle in line for needle in token_needles):
                            lines.append(line.rstrip("\n"))
            except OSError:
                continue
    return lines


def _nearby_source_files(source_file: Path | None, roots: list[Path]) -> list[Path]:
    if source_file is None:
        return []
    suffixes = {".h", ".hh", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx"}
    files: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            real = path.resolve()
        except OSError:
            return
        if real in seen or not real.is_file() or real.suffix.lower() not in suffixes:
            return
        seen.add(real)
        files.append(real)

    add(source_file)
    containing_roots = []
    for root in roots:
        try:
            source_file.relative_to(root)
        except ValueError:
            continue
        containing_roots.append(root)
    stop_root = max(containing_roots, key=lambda item: len(item.parts)) if containing_roots else source_file.parent
    current = source_file.parent
    while True:
        for glob in ("*.h", "*.hh", "*.hpp", "*.hxx"):
            try:
                for path in current.glob(glob):
                    add(path)
            except OSError:
                pass
        if current == stop_root or current == current.parent:
            break
        current = current.parent
    return files


def _parse_define_lines_from_files(paths: list[Path], tokens: set[str]) -> list[str]:
    token_needles = tuple(f"define {token}" for token in tokens)
    lines: list[str] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "#define" in line and any(needle in line for needle in token_needles):
                        lines.append(line.rstrip("\n"))
        except OSError:
            continue
    return lines


def _search_static_macros_once(
    source_root: Path,
    tokens: set[str],
    nearby_files: list[Path],
    allow_tree_scan: bool,
) -> dict[str, Macro]:
    lines = _parse_define_lines_from_files(nearby_files, tokens)
    macros = _parse_static_macro_lines(lines)
    remaining = tokens - set(macros)
    if remaining and allow_tree_scan:
        rg_lines = _rg_define_lines(source_root, remaining)
        macros.update(_parse_static_macro_lines(rg_lines))
        remaining = tokens - set(macros)
    if remaining and allow_tree_scan:
        walk_lines = _walk_define_lines(source_root, remaining)
        macros.update(_parse_static_macro_lines(walk_lines))
    return macros


def _find_static_macros_for_tokens(
    roots: list[Path],
    tokens: set[str],
    source_file: Path | None,
    static_cache: dict[Path, dict[str, Macro]],
    searched_cache: dict[Path, set[str]],
    recursive_dependencies: bool,
    allow_tree_scan: bool,
) -> dict[str, Macro]:
    needed = set(tokens)
    nearby_files = _nearby_source_files(source_file, roots)
    for _ in range(10):
        for root in roots:
            searched = searched_cache.setdefault(root, set())
            missing = needed - searched
            if not missing:
                continue
            static_cache.setdefault(root, {}).update(
                _search_static_macros_once(root, missing, nearby_files, allow_tree_scan)
            )
            searched.update(missing)
        macros: dict[str, Macro] = {}
        for root in roots:
            macros.update(static_cache.get(root, {}))
        if not recursive_dependencies:
            return macros
        dependencies: set[str] = set()
        for token in list(needed):
            macro = macros.get(token)
            if macro is None:
                continue
            dependencies.update(_candidate_macro_tokens(macro.value))
        new_dependencies = dependencies - needed
        if not new_dependencies:
            return macros
        needed.update(new_dependencies)
    macros = {}
    for root in roots:
        macros.update(static_cache.get(root, {}))
    return macros


def _merge_macros(primary: dict[str, Macro], fallback: dict[str, Macro]) -> dict[str, Macro]:
    merged = dict(fallback)
    merged.update(primary)
    return merged


def _git_stdout(args: list[str], cwd: Path | None = None, timeout: int = 30) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _checkout_matches_commit(path: Path, commit: str) -> bool:
    if not path.is_dir():
        return False
    if not commit:
        return True
    head = _git_stdout(["-C", str(path), "rev-parse", "HEAD"])
    if not head:
        return False
    expected = _git_stdout(["-C", str(path), "rev-parse", commit])
    if not expected:
        return head.startswith(commit) or commit.startswith(head)
    return head == expected


def _normalize_alias(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def _append_unique(items: list[str], value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = _normalize_alias(text)
    if not normalized:
        return False
    if any(_normalize_alias(existing) == normalized for existing in items):
        return False
    items.append(text)
    return True


def _expand_macros_once(expr: str, macros: dict[str, Macro], fully_expanded: bool) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(0)
        macro = macros.get(name)
        if macro is None:
            return name
        if not _macro_has_constant_value(macro, macros):
            return name
        value = expand_macro_value(macro.value, macros) if fully_expanded else macro.value
        return value

    return _IDENT_RE.sub(replace, expr)


def expand_macro_value(value: str, macros: dict[str, Macro], seen: set[str] | None = None) -> str:
    seen = set() if seen is None else set(seen)

    def replace(match: re.Match[str]) -> str:
        name = match.group(0)
        if name in seen:
            return name
        macro = macros.get(name)
        if macro is None:
            return name
        return expand_macro_value(macro.value, macros, seen | {name})

    previous = value
    for _ in range(20):
        current = _IDENT_RE.sub(replace, previous)
        if current == previous:
            return current.strip()
        previous = current
    return previous.strip()


def _is_constant_expr(expr: str) -> bool:
    text = _strip_comments(expr)
    if re.search(r"[A-Za-z_]", _C_INT_SUFFIX_RE.sub(r"\1", text)):
        return False
    return bool(re.fullmatch(r"[0-9A-Fa-fxX\s()+\-*/%<>=!&|^~?:.]+", text))


def _prepare_python_int_expr(expr: str) -> str:
    text = _strip_comments(expr)
    text = _C_INT_SUFFIX_RE.sub(r"\1", text)
    text = text.replace("&&", " and ").replace("||", " or ")
    # Convert C integer division to Python integer division after comments have
    # been removed.  GT aliases only need compile-time integer constants here.
    text = re.sub(r"(?<!/)/(?!/)", "//", text)
    return text


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_,
    ast.BitXor: operator.xor,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Invert: operator.invert,
    ast.Not: operator.not_,
}
_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _eval_ast(node: ast.AST) -> int | bool:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, bool)):
            return node.value
        raise ValueError("non-integer constant")
    if hasattr(ast, "Num") and isinstance(node, ast.Num):  # pragma: no cover - py<3.8
        return node.n
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError("unsupported binary operator")
        return op(int(_eval_ast(node.left)), int(_eval_ast(node.right)))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError("unsupported unary operator")
        return op(int(_eval_ast(node.operand)))
    if isinstance(node, ast.BoolOp):
        values = [bool(_eval_ast(value)) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise ValueError("unsupported bool operator")
    if isinstance(node, ast.Compare):
        left = _eval_ast(node.left)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise ValueError("unsupported compare operator")
            right = _eval_ast(comparator)
            if not op(int(left), int(right)):
                return False
            left = right
        return True
    raise ValueError("unsupported expression")


def evaluate_constant_expr(expr: str) -> str | None:
    if not _is_constant_expr(expr):
        return None
    try:
        parsed = ast.parse(_prepare_python_int_expr(expr), mode="eval")
        value = _eval_ast(parsed)
    except Exception:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(int(value))


def alias_variants_for_expr(expr: str, macros: dict[str, Macro]) -> list[str]:
    variants: list[str] = []
    _append_unique(variants, expr)
    if not any(token in macros for token in _candidate_macro_tokens(expr)):
        return variants

    once = _expand_macros_once(expr, macros, fully_expanded=False).strip()
    _append_unique(variants, once)
    full = _expand_macros_once(expr, macros, fully_expanded=True).strip()
    _append_unique(variants, full)
    evaluated = evaluate_constant_expr(full)
    if evaluated is not None:
        _append_unique(variants, evaluated)
    return variants


def _candidate_source_roots(result_dir: Path, explicit_roots: list[Path]) -> list[Path]:
    roots: list[Path] = []
    roots.extend(explicit_roots)
    info_path = result_dir / "sample_info.json"
    sample_info = _load_json(info_path) if info_path.is_file() else {}
    vulnerable_commit = str(
        sample_info.get("vulnerable_commit")
        or sample_info.get("vul_commit")
        or ""
    ).strip()
    for candidate in (
        result_dir / "source",
        result_dir / "_work" / "src",
        result_dir / "repo-vul" / "src-vul",
        result_dir / "workspace" / "repo-vul" / "src-vul",
    ):
        if candidate.is_dir() and (
            candidate in explicit_roots
            or candidate.name != "src"
            or candidate.parent.name != "_work"
            or _checkout_matches_commit(candidate, vulnerable_commit)
        ):
            roots.append(candidate)

    sample_id = str(sample_info.get("sample_id") or result_dir.name)
    project = str(sample_info.get("project") or "")
    if sample_id.startswith("arvo_"):
        arvo_id = sample_id.split("_", 1)[1]
        repo_root = Path.cwd()
        external = repo_root / "external" / "cybergym_data_subset" / "data" / "arvo" / arvo_id / "repo-vul" / "src-vul"
        if external.is_dir():
            if project and (external / project).is_dir():
                roots.append(external / project)
            roots.append(external)

    resolved: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            real = root.resolve()
        except OSError:
            continue
        if real in seen or not real.is_dir():
            continue
        seen.add(real)
        resolved.append(real)
    return resolved


def _source_cache_root() -> Path:
    configured = os.environ.get("GT_SOURCE_CACHE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "gt_generation" / "source_checkouts"


def _source_cache_path(sample_info: dict[str, Any], result_dir: Path) -> Path:
    sample_id = str(sample_info.get("sample_id") or result_dir.name)
    repo = str(sample_info.get("repo") or sample_info.get("repo_url") or "").strip()
    commit = str(
        sample_info.get("vulnerable_commit")
        or sample_info.get("vul_commit")
        or ""
    ).strip()
    digest = hashlib.sha1(f"{repo}\n{commit}".encode("utf-8")).hexdigest()[:12]
    safe_sample = re.sub(r"[^A-Za-z0-9_.-]+", "_", sample_id)
    return _source_cache_root() / f"{safe_sample}-{digest}"


def _ensure_vulnerable_source_checkout(sample_info: dict[str, Any], result_dir: Path) -> Path | None:
    sample_id = str(sample_info.get("sample_id") or result_dir.name)
    if sample_id.startswith("arvo_"):
        return None

    repo = str(sample_info.get("repo") or sample_info.get("repo_url") or "").strip()
    commit = str(
        sample_info.get("vulnerable_commit")
        or sample_info.get("vul_commit")
        or ""
    ).strip()
    if not repo or not commit or shutil.which("git") is None:
        return None

    cache_path = _source_cache_path(sample_info, result_dir)
    if _checkout_matches_commit(cache_path, commit):
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists() and not (cache_path / ".git").is_dir():
        shutil.rmtree(cache_path, ignore_errors=True)

    if not cache_path.exists():
        try:
            proc = subprocess.run(
                ["git", "clone", "--no-checkout", repo, str(cache_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,
            )
        except Exception:
            return None
        if proc.returncode != 0:
            shutil.rmtree(cache_path, ignore_errors=True)
            return None
    else:
        _git_stdout(["-C", str(cache_path), "fetch", "--all", "--tags", "--prune"], timeout=1800)

    if _git_stdout(["-C", str(cache_path), "checkout", "-q", commit], timeout=300) is None:
        return None
    if _checkout_matches_commit(cache_path, commit):
        return cache_path
    return None


def _resolve_source_file(
    rel_file: str | None,
    source_roots: list[Path],
    project: str | None,
) -> Path | None:
    if not rel_file:
        return None
    rel = Path(rel_file)
    for root in source_roots:
        direct = root / rel
        if direct.is_file():
            return direct

    matches: list[Path] = []
    suffix = Path(*rel.parts[-min(len(rel.parts), 4):])
    for root in source_roots:
        try:
            matches.extend(path for path in root.rglob(rel.name) if path.is_file())
        except OSError:
            continue
    if not matches:
        return None

    def score(path: Path) -> tuple[int, int]:
        text = path.as_posix()
        suffix_score = 1 if text.endswith(suffix.as_posix()) else 0
        project_score = 1 if project and project in path.parts else 0
        return (suffix_score + project_score, -len(path.parts))

    return sorted(matches, key=score, reverse=True)[0]


def _macros_for_source(
    source_file: Path | None,
    source_roots: list[Path],
    tokens: set[str],
    static_cache: dict[Path, dict[str, Macro]],
    searched_cache: dict[Path, set[str]],
    pp_cache: dict[Path, dict[str, Macro]],
    use_preprocessor: bool,
) -> dict[str, Macro]:
    scoped_roots = source_roots
    if source_file is not None:
        containing = []
        for root in source_roots:
            try:
                source_file.relative_to(root)
            except ValueError:
                continue
            containing.append(root)
        if containing:
            # Prefer the most specific per-sample checkout root.  For ARVO this
            # avoids scanning sibling AFL/libFuzzer trees under src-vul when the
            # actual vulnerable project root is known.
            scoped_roots = [max(containing, key=lambda item: len(item.parts))]

    primary: dict[str, Macro] = {}
    if source_file is not None and use_preprocessor and source_file not in pp_cache:
        try:
            pp_cache[source_file] = _run_preprocessor(source_file, source_roots)
        except Exception:
            pp_cache[source_file] = {}
    if source_file is not None and use_preprocessor:
        primary = pp_cache.get(source_file, {})
        if all(token in primary for token in tokens):
            return primary

    fallback = _find_static_macros_for_tokens(
        scoped_roots,
        tokens,
        source_file,
        static_cache,
        searched_cache,
        recursive_dependencies=False,
        allow_tree_scan=source_file is not None,
    )
    return _merge_macros(primary, fallback)


def augment_field_bindings(
    result_dir: Path,
    source_roots: list[Path] | None = None,
    dry_run: bool = False,
    use_preprocessor: bool = True,
) -> dict[str, Any]:
    result_dir = result_dir.resolve()
    field_path = result_dir / "field_bindings.json"
    locations_path = result_dir / "event_locations.json"
    field_doc = _load_json(field_path)
    locations_doc = _load_json(locations_path) if locations_path.is_file() else {}
    bindings = field_doc.get("bindings")
    if not isinstance(bindings, dict):
        raise ValueError(f"{field_path} has no object 'bindings'")
    locations = locations_doc.get("locations") if isinstance(locations_doc.get("locations"), dict) else {}

    info_path = result_dir / "sample_info.json"
    sample_info = _load_json(info_path) if info_path.is_file() else {}
    project = str(sample_info.get("project") or "")
    roots = _candidate_source_roots(result_dir, source_roots or [])
    static_cache: dict[Path, dict[str, Macro]] = {}
    searched_cache: dict[Path, set[str]] = {}
    pp_cache: dict[Path, dict[str, Macro]] = {}
    updates: list[BindingUpdate] = []
    cache_source_root: Path | None = None

    for key, raw_value in sorted(bindings.items()):
        key_text = str(key)
        if isinstance(raw_value, dict):
            expr = str(raw_value.get("expr") or raw_value.get("source") or "").strip()
            aliases = [
                str(item).strip()
                for item in raw_value.get("aliases", [])
                if str(item).strip()
            ] if isinstance(raw_value.get("aliases"), list) else []
            value_obj = raw_value
        else:
            expr = str(raw_value or "").strip()
            aliases = []
            value_obj = {"expr": expr}
            bindings[key] = value_obj

        candidate_tokens = _candidate_macro_tokens(expr)
        if not candidate_tokens:
            if aliases:
                value_obj["aliases"] = aliases
            continue

        event = key_text.rsplit(".", 1)[0] if "." in key_text else key_text
        location = locations.get(event) if isinstance(locations.get(event), dict) else {}
        source_file = _resolve_source_file(str(location.get("file") or ""), roots, project)
        if source_file is None and cache_source_root is None:
            cache_source_root = _ensure_vulnerable_source_checkout(sample_info, result_dir)
            if cache_source_root is not None:
                roots = _candidate_source_roots(result_dir, [*(source_roots or []), cache_source_root])
                source_file = _resolve_source_file(str(location.get("file") or ""), roots, project)
        elif source_file is None and cache_source_root is not None and cache_source_root not in roots:
            roots = _candidate_source_roots(result_dir, [*(source_roots or []), cache_source_root])
            source_file = _resolve_source_file(str(location.get("file") or ""), roots, project)
        macros = _macros_for_source(
            source_file,
            roots,
            candidate_tokens,
            static_cache,
            searched_cache,
            pp_cache,
            use_preprocessor,
        )

        before = list(aliases)
        for variant in alias_variants_for_expr(expr, macros):
            _append_unique(aliases, variant)
        if aliases != before:
            value_obj["aliases"] = aliases
            updates.append(
                BindingUpdate(
                    key=key_text,
                    source_file=str(source_file) if source_file else None,
                    added=[item for item in aliases if not any(_normalize_alias(item) == _normalize_alias(old) for old in before)],
                )
            )

    if updates and not dry_run:
        _write_json(field_path, field_doc)

    return {
        "result_dir": str(result_dir),
        "sample_id": field_doc.get("sample_id") or result_dir.name,
        "source_roots": [str(root) for root in roots],
        "cache_source_root": str(cache_source_root) if cache_source_root else None,
        "updated": len(updates),
        "dry_run": dry_run,
        "use_preprocessor": use_preprocessor,
        "updates": [
            {"key": item.key, "source_file": item.source_file, "added": item.added}
            for item in updates
        ],
    }


_COMPLETE_GT_FILES = {
    "ground_truth.json",
    "verified_invariants.json",
    "verified_assertions.json",
    "assertion_results.json",
    "field_bindings.json",
    "event_locations.json",
}


def _is_complete_result_dir(result_dir: Path) -> bool:
    if "repair-staging" in result_dir.name:
        return False
    return all((result_dir / name).is_file() for name in _COMPLETE_GT_FILES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--result-dir", type=Path)
    target.add_argument(
        "--gt-root",
        type=Path,
        help="Run over every immediate child directory with field_bindings.json.",
    )
    parser.add_argument(
        "--source-root",
        action="append",
        default=[],
        type=Path,
        help="Per-sample vulnerable source root. May be passed multiple times.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-preprocessor",
        action="store_true",
        help="Use only per-sample #define search; skip gcc/g++ -E -dM.",
    )
    parser.add_argument(
        "--complete-only",
        action="store_true",
        help="With --gt-root, skip repair-staging and incomplete result dirs.",
    )
    args = parser.parse_args(argv)
    use_preprocessor = not args.no_preprocessor

    if args.result_dir:
        report = augment_field_bindings(
            result_dir=args.result_dir,
            source_roots=args.source_root,
            dry_run=args.dry_run,
            use_preprocessor=use_preprocessor,
        )
    else:
        reports = []
        for field_path in sorted(args.gt_root.glob("*/field_bindings.json")):
            if args.complete_only and not _is_complete_result_dir(field_path.parent):
                continue
            try:
                reports.append(
                    augment_field_bindings(
                        result_dir=field_path.parent,
                        source_roots=args.source_root,
                        dry_run=args.dry_run,
                        use_preprocessor=use_preprocessor,
                    )
                )
            except Exception as exc:
                reports.append(
                    {
                        "result_dir": str(field_path.parent.resolve()),
                        "sample_id": field_path.parent.name,
                        "updated": 0,
                        "dry_run": args.dry_run,
                        "use_preprocessor": use_preprocessor,
                        "error": str(exc),
                    }
                )
        report = {
            "gt_root": str(args.gt_root.resolve()),
            "dry_run": args.dry_run,
            "processed": len(reports),
            "updated_dirs": sum(1 for item in reports if item.get("updated")),
            "errors": [item for item in reports if item.get("error")],
            "reports": reports,
        }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if isinstance(report, dict) and report.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
