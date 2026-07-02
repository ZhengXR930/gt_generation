#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare enhancement_runner.json for an ARVO/CyberGym sample."
    )
    parser.add_argument("sample", help="Sample id, e.g. arvo_12466 or arvo:12466.")
    parser.add_argument("--build-image", action="store_true", help="Build the per-sample runner image with gdb.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing enhancement_runner.json.")
    parser.add_argument("--image-tag", default="", help="Override generated runner image tag.")
    args = parser.parse_args()

    sample_id, arvo_id = _normalize_sample(args.sample)
    sample_dir = ROOT / "gt_results" / sample_id
    if not sample_dir.exists():
        raise SystemExit(f"sample directory not found: {sample_dir}")
    out_path = sample_dir / "enhancement_runner.json"
    if out_path.exists() and not args.force:
        raise SystemExit(f"exists: {out_path}; pass --force to overwrite")

    trigger = _load_json(sample_dir / "trigger.json")
    state = _load_json(sample_dir / "sample_state.json")
    source_image = str(trigger.get("container_image") or f"n132/arvo:{arvo_id}-vul")
    target_command = str(
        trigger.get("target_command")
        or (state.get("reproduction") or {}).get("target_command")
        or ""
    )
    if not target_command:
        raise SystemExit(f"target command unavailable for {sample_id}")

    runner_image = args.image_tag or f"gt-enhance-arvo-{arvo_id}:latest"
    if args.build_image:
        _build_runner_image(source_image=source_image, runner_image=runner_image)

    debug_command = _docker_gdb_wrapper_command(
        runner_image=runner_image,
        target_command=target_command,
    )
    coverage_command = _docker_coverage_command(
        runner_image=runner_image,
        target_command=target_command,
    )
    sanitizer_command = _docker_sanitizer_command(
        runner_image=runner_image,
        target_command=target_command,
    )
    manifest = {
        "schema_version": 1,
        "sample_id": sample_id,
        "arvo_id": arvo_id,
        "source_image": source_image,
        "runner_image": runner_image,
        "runner_image_built": bool(args.build_image),
        "target_command": target_command,
        "debug_command": debug_command,
        "coverage_command": coverage_command,
        "sanitizer_command": sanitizer_command,
        "notes": [
            "debug_command is a wrapper executed directly by candidate_synthesis_core because it contains breakpoints_path/hits_dir/gdb_script placeholders.",
            "coverage_command is used for H1-H4 on hosts where GDB cannot run under amd64 emulation.",
            "Breakpoints are generated dynamically from the agent's recorded hypothesis, not from GT.",
            "The runner image must contain gdb, python3, llvm-profdata, llvm-cov, and /out_cov/libarchive_fuzzer when coverage_command is used.",
        ],
    }
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"written": str(out_path), "runner_image": runner_image}, indent=2))


def _normalize_sample(value: str) -> tuple[str, str]:
    if value.startswith("arvo:"):
        arvo_id = value.split(":", 1)[1]
        return f"arvo_{arvo_id}", arvo_id
    if value.startswith("arvo_"):
        return value, value.removeprefix("arvo_")
    if value.isdigit():
        return f"arvo_{value}", value
    raise SystemExit(f"unsupported sample id: {value}")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    return data if isinstance(data, dict) else {}


def _build_runner_image(*, source_image: str, runner_image: str) -> None:
    dockerfile = f"""
FROM --platform=linux/amd64 {source_image}
USER root
	RUN if ! command -v gdb >/dev/null 2>&1; then \\
      if [ -f /etc/apt/sources.list ]; then \\
        printf '%s\\n' \\
          'deb http://archive.ubuntu.com/ubuntu/ xenial main restricted universe multiverse' \\
          'deb http://archive.ubuntu.com/ubuntu/ xenial-updates main restricted universe multiverse' \\
          > /etc/apt/sources.list; \\
      fi; \\
      apt-get -o Acquire::Check-Valid-Until=false update && \\
	      apt-get install -y --no-install-recommends gdb python3 && \\
	      rm -rf /var/lib/apt/lists/*; \\
	    fi
	RUN if [ ! -f /usr/lib/libFuzzingEngine.a ]; then \\
	      printf '%s\\n' \\
	        '#include <stdint.h>' \\
	        '#include <stddef.h>' \\
	        '#include <stdio.h>' \\
	        '#include <stdlib.h>' \\
	        '#include <vector>' \\
	        'extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);' \\
	        'int main(int argc, char **argv) {{' \\
	        '  FILE *f = stdin;' \\
	        '  if (argc > 1) {{' \\
	        '    f = fopen(argv[1], "rb");' \\
	        '    if (!f) return 2;' \\
	        '  }}' \\
	        '  std::vector<uint8_t> data;' \\
	        '  uint8_t buf[4096];' \\
	        '  while (true) {{' \\
	        '    size_t n = fread(buf, 1, sizeof(buf), f);' \\
	        '    if (n) data.insert(data.end(), buf, buf + n);' \\
	        '    if (n < sizeof(buf)) break;' \\
	        '  }}' \\
	        '  if (f != stdin) fclose(f);' \\
	        '  return LLVMFuzzerTestOneInput(data.data(), data.size());' \\
	        '}}' \\
	        > /tmp/StandaloneFuzzTargetMain.cpp && \\
	      clang++ -c /tmp/StandaloneFuzzTargetMain.cpp -o /tmp/StandaloneFuzzTargetMain.o && \\
	      ar rcs /usr/lib/libFuzzingEngine.a /tmp/StandaloneFuzzTargetMain.o; \\
	    fi
	RUN if [ ! -x /out_cov/libarchive_fuzzer ]; then \\
	      mkdir -p /out_cov && \\
	      sed -i 's/-Wl,-Bdynamic/-Wl,-Bdynamic -ldl/g' /src/build.sh && \\
	      cd /src/libarchive && \\
	      make clean >/dev/null 2>&1 || true && \\
	      CC=clang CXX=clang++ \\
	      CFLAGS="-g -O0 -fprofile-instr-generate -fcoverage-mapping" \\
	      CXXFLAGS="-g -O0 -fprofile-instr-generate -fcoverage-mapping" \\
	      LDFLAGS="-fprofile-instr-generate -fcoverage-mapping" \\
	      SRC=/src OUT=/out_cov /bin/bash /src/build.sh; \\
	    fi
	"""
    with tempfile.TemporaryDirectory() as tmp:
        dockerfile_path = Path(tmp) / "Dockerfile"
        dockerfile_path.write_text(dockerfile, encoding="utf-8")
        subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "-t", runner_image, tmp],
            check=True,
        )


def _docker_gdb_wrapper_command(*, runner_image: str, target_command: str) -> str:
    argv = shlex.split(target_command)
    if not argv:
        raise SystemExit("empty target command")
    binary = argv[0]
    extra_args = " ".join(shlex.quote(arg) for arg in argv[1:])
    inner = (
        "REACHABILITY_BREAKPOINTS=/tmp/reachability_breakpoints.json "
        "REACHABILITY_OUTPUT=/tmp/reachability_out/reachability_hits.json "
        "gdb --batch -q -x /tmp/gdb_reachability.py --args "
        f"{shlex.quote(binary)} {extra_args}"
    ).strip()
    return (
        "docker run --rm --platform linux/amd64 "
        "-v {candidate_path}:/tmp/poc:ro "
        "-v {breakpoints_path}:/tmp/reachability_breakpoints.json:ro "
        "-v {hits_dir}:/tmp/reachability_out "
        "-v {gdb_script}:/tmp/gdb_reachability.py:ro "
        f"{shlex.quote(runner_image)} /bin/bash -lc {shlex.quote(inner)}"
    )


def _docker_coverage_command(*, runner_image: str, target_command: str) -> str:
    argv = shlex.split(target_command)
    if not argv:
        raise SystemExit("empty target command")
    binary = "/out_cov/" + Path(argv[0]).name
    extra_args = " ".join(shlex.quote(arg) for arg in argv[1:])
    target = f"{shlex.quote(binary)} {extra_args}".strip()
    inner = (
        "rm -f /tmp/coverage_out/default.profraw /tmp/coverage_out/default.profdata "
        "/tmp/coverage_out/coverage.json; "
        "LLVM_PROFILE_FILE=/tmp/coverage_out/default.profraw "
        f"{target} >/tmp/coverage_out/coverage_run_stdout.txt 2>/tmp/coverage_out/coverage_run_stderr.txt || true; "
        "test -f /tmp/coverage_out/default.profraw && "
        "llvm-profdata merge -sparse /tmp/coverage_out/default.profraw -o /tmp/coverage_out/default.profdata && "
        f"llvm-cov export {shlex.quote(binary)} -instr-profile=/tmp/coverage_out/default.profdata "
        "> /tmp/coverage_out/coverage.json"
    )
    return (
        "docker run --rm --platform linux/amd64 "
        "-v {candidate_path}:/tmp/poc:ro "
        "-v {coverage_dir}:/tmp/coverage_out "
        f"{shlex.quote(runner_image)} /bin/bash -lc {shlex.quote(inner)}"
    )


def _docker_sanitizer_command(*, runner_image: str, target_command: str) -> str:
    return (
        "docker run --rm --platform linux/amd64 "
        "-v {candidate_path}:/tmp/poc:ro "
        f"{shlex.quote(runner_image)} /bin/bash -lc {shlex.quote(target_command)}"
    )


if __name__ == "__main__":
    main()
