#!/usr/bin/env bash
set -euo pipefail
echo "This sample uses the SEC-bench harness."
echo "Expected project layout: vulnerable checkout/build mounted at the paths used by SEC-bench."
echo "Original repro command:"
cat <<'EOF'
/src/libxml2/xmllint --huge --maxmem 50000 /testcase/poc
return $?
EOF
exit 2
