#!/usr/bin/env bash
set -euo pipefail
echo "This sample uses the SEC-bench harness."
echo "Expected project layout: vulnerable checkout/build mounted at the paths used by SEC-bench."
echo "Original repro command:"
cat <<'EOF'
/out/mupdf/mutool draw -F pbm -o /dev/null /testcase/poc 1
EOF
exit 2
