#!/usr/bin/env bash
# End-to-end check through the gateway, the way a user would call it: sign up,
# call a protected route with and without a token, send an oversized body,
# and stream an AI review to the end.
set -euo pipefail

BASE=${BASE:-http://localhost:8000}
EMAIL="smoke-$RANDOM$RANDOM@example.com"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

TOKEN=$(curl -sf -X POST "$BASE/api/auth/signup" -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"smoke-pass-123\"}" |
  python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])')
echo "signed up $EMAIL"

curl -sf "$BASE/api/users/me" -H "Authorization: Bearer $TOKEN"
echo

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/users/me" || true)
if [ "$code" != 401 ]; then
  fail "a request without a token should get 401, got $code"
fi
echo "no token -> 401 OK"

# A public prefix must not lead to a protected route: /api/auth/.. climbs
# out of the public /api/auth into /users. --path-as-is sends the ".." as is.
code=$(curl -s -o /dev/null -w '%{http_code}' --path-as-is "$BASE/api/auth/../users/me" || true)
if [ "$code" != 400 ]; then
  fail "a path with '..' should get 400, got $code"
fi
echo "path traversal -> 400 OK"

# The gateway refuses bodies over MAX_BODY_BYTES (1 MiB by default) before
# they reach any service.
code=$(head -c 1100000 /dev/zero | tr '\0' x |
  curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/reviews/stream" \
    -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
    --data-binary @- || true)
if [ "$code" != 413 ]; then
  fail "a body over 1 MiB should get 413, got $code"
fi
echo "oversized body -> 413 OK"

# Stream a review (LLM_PROVIDER=fake in CI) and check it reaches the end.
DIFF='--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n'
OUT=$(curl -sfN -X POST "$BASE/api/reviews/stream" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d "{\"diff\":\"$DIFF\"}")
if ! grep -q '^event: done' <<<"$OUT"; then
  echo "$OUT" >&2
  fail "the review stream did not reach 'done'"
fi
echo "review stream reached 'done' OK"
echo "smoke test passed"
