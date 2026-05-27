#!/usr/bin/env bash
set -euo pipefail
echo "This sample uses the SEC-bench harness."
echo "Expected project layout: vulnerable checkout/build mounted at the paths used by SEC-bench."
echo "Original repro command:"
cat <<'EOF'
/out/mupdf/mutool draw -o /tmp/out.png /testcase/poc
EOF
exit 2
