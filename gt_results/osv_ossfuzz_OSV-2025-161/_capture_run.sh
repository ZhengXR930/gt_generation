#!/usr/bin/env bash
set -o pipefail

trace_file="$1"
shift

"$@" >"$trace_file" 2>&1
status=$?
printf '%s' "$status" >"${trace_file}.exitcode"
exit 0
