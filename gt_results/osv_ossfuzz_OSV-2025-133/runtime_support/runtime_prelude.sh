#!/usr/bin/env bash
set -euo pipefail

cd "${SRC:?}"

# The upstream OSS-Fuzz recipe checks out this project under $SRC/net-snmp.
# Our runtime restores the vulnerable repo directly at $SRC, so recreate the
# same layout without copying the source tree.
ln -sfn . net-snmp
