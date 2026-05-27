#!/usr/bin/env bash
set -euo pipefail
echo "This sample uses the SEC-bench harness."
echo "Expected project layout: vulnerable checkout/build mounted at the paths used by SEC-bench."
echo "Original repro command:"
cat <<'EOF'
/src/libxml2/xmllint --html --encode KOI8-R --output /nonexistent/libxml2/out.html /testcase/poc
EOF
exit 2
