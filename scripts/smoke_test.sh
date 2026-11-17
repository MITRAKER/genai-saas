#!/usr/bin/env bash
# End-to-end check through the gateway: sign up, call a protected route,
# stream an AI review.
set -euo pipefail
BASE=${BASE:-http://localhost:8000}
EMAIL="smoke-$RANDOM@example.com"

TOKEN=$(curl -sf -X POST "$BASE/api/auth/signup" -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"smoke-pass-123\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
echo "signed up $EMAIL"

curl -sf "$BASE/api/users/me" -H "Authorization: Bearer $TOKEN"; echo
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/users/me")
[ "$code" = 401 ] && echo "no token -> 401 OK" || { echo "expected 401, got $code"; exit 1; }

# Stream a review (LLM_PROVIDER=fake in CI) and check it reaches the end.
DIFF='--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n'
OUT=$(curl -sfN -X POST "$BASE/api/reviews/stream" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d "{\"diff\":\"$DIFF\"}")
echo "$OUT" | grep -q '^event: done' && echo "review stream reached 'done' OK" \
  || { echo "review stream failed:"; echo "$OUT"; exit 1; }
echo "smoke test passed"
