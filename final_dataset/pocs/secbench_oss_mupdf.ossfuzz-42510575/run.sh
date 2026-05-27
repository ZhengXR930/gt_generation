#!/usr/bin/env bash
set -euo pipefail
echo "This sample uses the SEC-bench harness."
echo "Expected project layout: vulnerable checkout/build mounted at the paths used by SEC-bench."
echo "Original repro command:"
cat <<'EOF'
MSAN_OPTIONS=halt_on_error=1 /out/mupdf/mutool draw -o /dev/null /testcase/poc
return 1
EOF
exit 2
