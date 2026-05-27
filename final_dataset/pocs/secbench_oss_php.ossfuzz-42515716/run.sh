#!/usr/bin/env bash
set -euo pipefail
echo "This sample uses the SEC-bench harness."
echo "Expected project layout: vulnerable checkout/build mounted at the paths used by SEC-bench."
echo "Original repro command:"
cat <<'EOF'
USE_ZEND_ALLOC=0 /src/php-src/sapi/cli/php /testcase/poc
EOF
exit 2
