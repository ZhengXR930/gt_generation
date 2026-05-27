#!/usr/bin/env bash
set -euo pipefail
echo "This sample uses the SEC-bench harness."
echo "Expected project layout: vulnerable checkout/build mounted at the paths used by SEC-bench."
echo "Original repro command:"
cat <<'EOF'
ASAN_OPTIONS=detect_leaks=0 /out/mupdf/mutool draw -o /dev/null /testcase/poc
EOF
exit 2
