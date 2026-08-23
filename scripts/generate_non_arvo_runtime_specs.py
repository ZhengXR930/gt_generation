#!/usr/bin/env python3
"""Generate durable runtime specs for non-ARVO GT samples.

The generated files are evaluator-only metadata.  They reconstruct the
vulnerable binary from public source repo + vulnerable commit and run the
sample's PoC through the project entry point.  No compiled artifacts are
persisted in git.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parents[1]
GT_ROOT = ROOT / "gt_results"
IMAGE = "gt-memory-env:latest"


ASAN_ENV = {
    "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1",
    "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1",
}


def sh_script(body: str) -> str:
    """Normalize Python-indented shell snippets without breaking heredocs.

    Some recipes contain flush-left heredoc bodies (Ruby/C/Python fragments).
    `textwrap.dedent()` then sees a common indentation of zero and leaves the
    actual shell script indented, which makes heredoc terminators such as EOF or
    RUBY invalid.  Strip the indentation used by the first non-empty line from
    lines that have it, while leaving flush-left heredoc payloads untouched.
    """
    text = body.strip("\n")
    lines = text.splitlines()
    prefix = ""
    for line in lines:
        if line.strip():
            prefix = line[: len(line) - len(line.lstrip())]
            break
    if prefix:
        lines = [
            line[len(prefix) :] if line.startswith(prefix) else line
            for line in lines
        ]
    return dedent("\n".join(lines)).strip("\n") + "\n"


ONE_INPUT_MAIN = sh_script(
    r"""
    #include <stdint.h>
    #include <stdio.h>
    #include <stdlib.h>

    extern int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

    int main(int argc, char **argv) {
      if (argc < 2) return 2;
      FILE *fp = fopen(argv[1], "rb");
      if (!fp) return 3;
      if (fseek(fp, 0, SEEK_END) != 0) return 4;
      long size = ftell(fp);
      if (size < 0) return 5;
      rewind(fp);
      uint8_t *buf = (uint8_t *)malloc((size_t)size + 1);
      if (!buf) return 6;
      size_t got = fread(buf, 1, (size_t)size, fp);
      fclose(fp);
      int rc = LLVMFuzzerTestOneInput(buf, got);
      free(buf);
      return rc;
    }
    """
)


RECIPES: dict[str, dict[str, str | list[str]]] = {
    "libxml2": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            mkdir -p "$runtime"
            cat > "$runtime/one_input_main.c" <<'EOF'
            __ONE_INPUT_MAIN__
            EOF
            if [ ! -f Makefile ]; then
              if [ -x ./autogen.sh ]; then ./autogen.sh; elif [ -x ./autogen ]; then ./autogen; else autoreconf -fi; fi
              CC=clang CXX=clang++ CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                CXXFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                LDFLAGS="-fsanitize=address" \
                ./configure --without-python --disable-shared --enable-static
            fi
            make -j"${GT_BUILD_JOBS:-2}"
            for target in xml html schema xinclude xpath regexp uri valid; do
              if [ -f "fuzz/${target}.c" ]; then
                clang -O1 -g -fsanitize=address -fno-omit-frame-pointer \
                  -I. -Iinclude -Ifuzz "$runtime/one_input_main.c" "fuzz/${target}.c" fuzz/fuzz.c \
                  .libs/libxml2.a -lz -llzma -lm -o "$runtime/libxml2_${target}" || true
              fi
            done
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            poc="$1"
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            for t in xml html schema xinclude xpath regexp uri valid; do
              if [ -x "$runtime/libxml2_${t}" ]; then "$runtime/libxml2_${t}" "$poc"; fi
            done
            if [ -x /gt/_work/src/xmllint ]; then
              /gt/_work/src/xmllint "$poc" || true
              /gt/_work/src/xmllint --html "$poc" || true
              /gt/_work/src/xmllint --xinclude "$poc" || true
              /gt/_work/src/xmllint --stream "$poc" || true
            fi
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ).replace("__ONE_INPUT_MAIN__", ONE_INPUT_MAIN.rstrip()),
    },
    "mruby": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            mkdir -p "$runtime" build_config
            if [ -f build_config/clang-asan.rb ]; then
              config=build_config/clang-asan.rb
            else
              config=build_config/gt_asan.rb
              cat > "$config" <<'RUBY'
MRuby::Build.new do |conf|
  conf.toolchain :clang
  conf.gembox 'full-core'
  conf.enable_sanitizer "address,undefined"
  conf.enable_debug
end
RUBY
            fi
            rake clean MRUBY_CONFIG="$config" || true
            rake MRUBY_CONFIG="$config" -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            /gt/_work/src/build/host/bin/mruby "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "gpac": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            mkdir -p "$runtime"
            if [ ! -x bin/gcc/MP4Box ]; then
              ./configure --enable-debug --cc=clang --cxx=clang++ \
                --extra-cflags="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                --extra-ldflags="-fsanitize=address" || \
              ./configure --enable-debug CC=clang CXX=clang++ \
                CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                CXXFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                LDFLAGS="-fsanitize=address"
              make -j"${GT_BUILD_JOBS:-2}"
            fi
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            poc="$1"
            export LD_LIBRARY_PATH="/gt/_work/src/bin/gcc:${LD_LIBRARY_PATH:-}"
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            mp4box=/gt/_work/src/bin/gcc/MP4Box
            case "${GT_SAMPLE_ID}" in
              secbench_cve_gpac.cve-2021-32439) exec "$mp4box" -hint "$poc" -out /tmp/gpac-out.mp4 ;;
              secbench_cve_gpac.cve-2022-3178) exec "$mp4box" -diso "$poc" ;;
              secbench_cve_gpac.cve-2023-46001|secbench_cve_gpac.cve-2023-48011|secbench_cve_gpac.cve-2023-48013|secbench_cve_gpac.cve-2024-0321|secbench_cve_gpac.cve-2024-0322)
                exec "$mp4box" -dash 10000 "$poc" ;;
            esac
            "$mp4box" -add "$poc" -new /tmp/gpac-import.mp4 || true
            "$mp4box" -info "$poc" || true
            "$mp4box" -diso "$poc" || true
            "$mp4box" -dash 10000 "$poc" || true
            "$mp4box" -hint "$poc" -out /tmp/gpac-out.mp4
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "openexr": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            build=/gt/_work/build-openexr-asan
            mkdir -p "$runtime"
            cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=RelWithDebInfo \
              -DBUILD_TESTING=OFF -DOPENEXR_BUILD_TOOLS=ON \
              -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
              -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
            cmake --build "$build" --target exrcheck -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            poc="$1"
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            exrcheck=/gt/_work/build-openexr-asan/bin/exrcheck
            "$exrcheck" -c "$poc" || "$exrcheck" -s -c "$poc" || "$exrcheck" "$poc"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "upx": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            build=/gt/_work/build-upx-asan
            mkdir -p "$runtime"
            git submodule update --init --recursive || true
            cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=Debug \
              -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
              -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
            cmake --build "$build" -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            upx_bin=$(find /gt/_work/build-upx-asan /gt/_work/src -type f -name upx -perm -111 2>/dev/null | head -n 1)
            exec "$upx_bin" "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "php-src": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            mkdir -p "$runtime"
            if [ ! -x sapi/cli/php ]; then
              ./buildconf --force || true
              CC=clang CXX=clang++ CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                CXXFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                LDFLAGS="-fsanitize=address" \
                ./configure --disable-all --enable-cli --enable-debug --enable-address-sanitizer
              make -j"${GT_BUILD_JOBS:-2}" sapi/cli/php
            fi
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            exec /gt/_work/src/sapi/cli/php "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "libdwarf-code": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            build=/gt/_work/build-libdwarf-asan
            mkdir -p "$runtime"
            cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=Debug \
              -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
              -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
            cmake --build "$build" -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            dwarfdump=$(find /gt/_work/build-libdwarf-asan /gt/_work/src -type f -name dwarfdump -perm -111 2>/dev/null | head -n 1)
            exec "$dwarfdump" "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "md4c": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            build=/gt/_work/build-md4c-asan
            mkdir -p "$runtime"
            cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=RelWithDebInfo \
              -DBUILD_SHARED_LIBS=OFF -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
              -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
            cmake --build "$build" -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            bin=$(find /gt/_work/build-md4c-asan /gt/_work/src -type f \( -name md2html -o -name md4c-html -o -name fuzz-mdhtml \) -perm -111 2>/dev/null | head -n 1)
            exec "$bin" "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "openjpeg": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            build=/gt/_work/build-openjpeg-asan
            mkdir -p "$runtime"
            cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=Debug -DBUILD_CODEC=ON \
              -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
              -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
            cmake --build "$build" -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            opj=$(find /gt/_work/build-openjpeg-asan -type f -name opj_decompress -perm -111 2>/dev/null | head -n 1)
            exec "$opj" -i "$1" -o /tmp/openjpeg-out.pnm
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "faad2": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            mkdir -p "$runtime"
            if [ -x ./bootstrap ]; then ./bootstrap; else autoreconf -fi; fi
            CC=clang CXX=clang++ CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              CXXFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              LDFLAGS="-fsanitize=address" ./configure --disable-shared --enable-static
            make -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            faad=$(find /gt/_work/src -type f -name faad -perm -111 2>/dev/null | head -n 1)
            exec "$faad" -o /tmp/faad-out.wav "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "libarchive": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            mkdir -p "$runtime"
            if [ ! -f Makefile ]; then
              ./build/autogen.sh || autoreconf -fi
              CC=clang CXX=clang++ CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                CXXFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                LDFLAGS="-fsanitize=address" ./configure --disable-shared --enable-static
            fi
            make -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            bsdtar=$(find /gt/_work/src -type f -name bsdtar -perm -111 2>/dev/null | head -n 1)
            exec "$bsdtar" -tf "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "libjpeg-turbo": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            build=/gt/_work/build-libjpeg-turbo-asan
            mkdir -p "$runtime"
            cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=Debug \
              -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
              -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
            cmake --build "$build" -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            djpeg=$(find /gt/_work/build-libjpeg-turbo-asan -type f -name djpeg -perm -111 2>/dev/null | head -n 1)
            exec "$djpeg" "$1" >/tmp/djpeg-out.ppm
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "matio": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            mkdir -p "$runtime"
            if [ ! -f Makefile ]; then
              ./autogen.sh || autoreconf -fi
              CC=clang CXX=clang++ CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                CXXFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
                LDFLAGS="-fsanitize=address" ./configure --disable-shared --enable-static
            fi
            make -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            matdump=$(find /gt/_work/src -type f -name matdump -perm -111 2>/dev/null | head -n 1)
            exec "$matdump" "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "mupdf": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            mkdir -p "$runtime"
            git submodule update --init --recursive || true
            make -j"${GT_BUILD_JOBS:-2}" build=sanitize HAVE_X11=no HAVE_GLUT=no HAVE_CURL=no \
              USE_SYSTEM_FREETYPE=yes USE_SYSTEM_HARFBUZZ=yes USE_SYSTEM_JBIG2DEC=yes \
              USE_SYSTEM_LCMS2=yes USE_SYSTEM_LIBJPEG=yes USE_SYSTEM_OPENJPEG=yes USE_SYSTEM_ZLIB=yes \
              XCFLAGS="-DFZ_ENABLE_ICC=0"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            mutool=$(find /gt/_work/src/build -type f -name mutool -perm -111 2>/dev/null | head -n 1)
            exec "$mutool" draw -o /tmp/mupdf-out-%d.png "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "wasm3": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            build=/gt/_work/build-wasm3-asan
            mkdir -p "$runtime"
            cmake -S . -B "$build" -DCMAKE_BUILD_TYPE=Debug \
              -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
              -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address"
            cmake --build "$build" -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            wasm3=$(find /gt/_work/build-wasm3-asan /gt/_work/src -type f \( -name wasm3 -o -name m3 \) -perm -111 2>/dev/null | head -n 1)
            exec "$wasm3" "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "yara": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            mkdir -p "$runtime"
            ./bootstrap.sh || autoreconf -fi
            CC=clang CXX=clang++ CFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              CXXFLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              LDFLAGS="-fsanitize=address" ./configure --disable-shared --enable-static --without-crypto
            make -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            yara=$(find /gt/_work/src -type f -name yara -perm -111 2>/dev/null | head -n 1)
            exec "$yara" "$1" /tmp
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
    "wamr": {
        "build": sh_script(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            cd /gt/_work/src
            runtime=/gt/_work/runtime/${GT_SAMPLE_ID}
            build=/gt/_work/build-wamr-fast-asan
            mkdir -p "$runtime"
            cmake -S tests/fuzz/wasm-mutator-fuzz -B "$build" -G Ninja \
              -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
              -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_CXX_FLAGS="-O1 -g -fsanitize=address -fno-omit-frame-pointer" \
              -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address" \
              -DWAMR_BUILD_AOT=0 -DWAMR_BUILD_JIT=0 -DWAMR_BUILD_FAST_INTERP=1
            cmake --build "$build" --target wasm_mutator_fuzz -j"${GT_BUILD_JOBS:-2}"
            cat > "$runtime/run_poc.sh" <<'EOF'
            #!/usr/bin/env bash
            set -euo pipefail
            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=1:halt_on_error=1:exitcode=1:symbolize=1}"
            exec /gt/_work/build-wamr-fast-asan/wasm-mutator/wasm_mutator_fuzz -runs=1 "$1"
            EOF
            chmod +x "$runtime/run_poc.sh"
            """
        ),
    },
}


PROJECT_ALIASES = {
    "php-src": "php-src",
    "libdwarf-code": "libdwarf-code",
    "wasm-micro-runtime": "wamr",
}


def runtime_spec(sample_id: str, info: dict, project: str) -> dict:
    environment = dict(ASAN_ENV)
    environment["GT_SAMPLE_ID"] = sample_id
    environment["GT_PROJECT"] = project
    return {
        "sample_id": sample_id,
        "backend": "local_workspace",
        "image": IMAGE,
        "workdir": "/gt/_work/src",
        "executable": f"/gt/_work/runtime/{sample_id}/run_poc.sh",
        "arguments": ["{poc}"],
        "environment": environment,
        "input_placeholder": "{poc}",
        "source": "generated_non_arvo_runtime_specs.py",
        "build_commands": [
            f"GT_SAMPLE_ID={sample_id} GT_PROJECT={project} bash /gt/runtime_support/build_runtime.sh"
        ],
        "build_workdir": "/gt/_work/src",
        "source_repo": info.get("repo") or "",
        "source_commit": info.get("vulnerable_commit") or "",
        "run_timeout": 60,
    }


def write_sample(sample_id: str, *, overwrite_existing: bool) -> str:
    gt_dir = GT_ROOT / sample_id
    info = json.loads((gt_dir / "sample_info.json").read_text())
    existing_spec_path = gt_dir / "runtime_spec.json"
    if existing_spec_path.exists():
        if not overwrite_existing:
            return "kept_existing"
        try:
            existing_source = str(json.loads(existing_spec_path.read_text()).get("source") or "")
        except Exception:
            existing_source = ""
        if existing_source and existing_source != "generated_non_arvo_runtime_specs.py":
            return f"kept_curated:{existing_source}"
    project = PROJECT_ALIASES.get(info.get("project"), info.get("project"))
    if project not in RECIPES:
        return f"missing_recipe:{project}"
    support = gt_dir / "runtime_support"
    support.mkdir(exist_ok=True)
    (support / "build_runtime.sh").write_text(RECIPES[project]["build"], encoding="utf-8")
    (support / "build_runtime.sh").chmod(0o755)
    (gt_dir / "runtime_spec.json").write_text(
        json.dumps(runtime_spec(sample_id, info, project), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return "written"


def main() -> int:
    overwrite_existing = "--overwrite-existing" in sys.argv[1:]
    valid = json.loads((GT_ROOT / "valid_gt.json").read_text())["samples"]
    statuses: dict[str, list[str]] = {}
    for sample_id in valid:
        if sample_id.startswith("arvo_"):
            continue
        status = write_sample(sample_id, overwrite_existing=overwrite_existing)
        statuses.setdefault(status, []).append(sample_id)
    for status, samples in sorted(statuses.items()):
        print(status, len(samples))
        for sample in samples[:20]:
            print(" ", sample)
        if len(samples) > 20:
            print("  ...")
    failed = [
        status
        for status in statuses
        if status not in {"written", "kept_existing"} and not status.startswith("kept_curated:")
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
