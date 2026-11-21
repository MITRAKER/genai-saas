#!/usr/bin/env bash
# End-to-end check of the GitHub integration, the way GitHub would use it.
# It needs the stack started with the fake GitHub (`make demo` does both):
#   COMPOSE_FILE=docker-compose.yml:docker-compose.e2e.yml docker compose up -d --wait
#
# A signed webhook goes through the gateway to the github service, which
# queues a job. The worker fetches the pull request from the fake GitHub,
# indexes the repository's code, asks the agent for a review and posts it.
# Then this script reads that review back from the fake.
set -euo pipefail

BASE=${BASE:-http://localhost:8000}
FAKE_GITHUB=${FAKE_GITHUB:-http://localhost:9000}
HOOK="$BASE/api/github/webhooks"
ENV_FILE="$(dirname "$0")/../.env"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# The secret GitHub signs webhooks with: from the environment, or from .env
# like the stack itself.
SECRET=${GITHUB_WEBHOOK_SECRET:-$(sed -n 's/^GITHUB_WEBHOOK_SECRET=//p' "$ENV_FILE" 2>/dev/null || true)}
if [ -z "$SECRET" ]; then
  fail "GITHUB_WEBHOOK_SECRET is not set; 'make env' writes one to .env"
fi

# sign: the X-Hub-Signature-256 header GitHub sends for the body on stdin, an
# HMAC-SHA256 with the shared secret. The secret goes to Python through the
# environment, because command-line arguments are visible to other users.
sign() {
  WEBHOOK_SECRET=$SECRET python3 -c '
import hashlib, hmac, os, sys
key = os.environ["WEBHOOK_SECRET"].encode()
print("sha256=" + hmac.new(key, sys.stdin.buffer.read(), hashlib.sha256).hexdigest())'
}

# deliver EVENT BODY [SIGNATURE]: send a webhook, print the answer and its status.
deliver() {
  local signature
  signature=${3:-$(printf '%s' "$2" | sign)}
  curl -sS -X POST "$HOOK" -H 'content-type: application/json' \
    -H "X-GitHub-Event: $1" -H "X-Hub-Signature-256: $signature" --data-binary "$2" \
    -w ' (HTTP %{http_code})'
}

# must_contain TEXT PART: fail unless the text contains that part.
must_contain() {
  if ! grep -qF -- "$2" <<<"$1"; then
    fail "expected '$2' in: $1"
  fi
}

# Anyone can send a request to the webhook URL, so a delivery without the
# right signature is refused before its body is even read.
must_contain "$(deliver ping '{"zen":"hi"}' 'sha256=0')" '(HTTP 401)'
echo "wrong signature -> 401 OK"
must_contain "$(deliver ping '{"zen":"changed"}' "$(printf '%s' '{"zen":"hi"}' | sign)")" '(HTTP 401)'
echo "body changed after signing -> 401 OK"
must_contain "$(deliver ping '{"zen":"hi"}')" '"status":"pong"'
echo "signed ping -> pong OK"

# The pull request the fake serves (see scripts/fake_github.py).
HEAD_SHA=c0ffeec0ffeec0ffeec0ffeec0ffeec0ffeec0de
BASE_SHA=ba5eba5eba5eba5eba5eba5eba5eba5eba5eba5e
OPENED=$(
  cat <<JSON
{"action": "opened",
 "pull_request": {"number": 7, "draft": false,
                  "head": {"sha": "$HEAD_SHA"}, "base": {"sha": "$BASE_SHA"}},
 "repository": {"id": 1001, "full_name": "demo-org/shop", "owner": {"login": "demo-org"}},
 "installation": {"id": 2002}}
JSON
)

must_contain "$(deliver pull_request "$OPENED")" '"status":"queued"'
echo "pull request opened -> queued OK"
# GitHub may deliver the same event twice; it must not cost a second review.
must_contain "$(deliver pull_request "$OPENED")" '"status":"duplicate"'
echo "same event again -> duplicate OK"

echo "waiting for the worker to post its review..."
REVIEWS='[]'
for _ in $(seq 120); do
  REVIEWS=$(curl -sSf "$FAKE_GITHUB/_fake/reviews")
  if [ "$REVIEWS" != '[]' ]; then
    break
  fi
  sleep 0.5
done
BODY=$(python3 -c 'import json, sys; [review] = json.load(sys.stdin); print(review["body"])' \
  <<<"$REVIEWS") || fail "expected one review on the fake GitHub, found: $REVIEWS"

must_contain "$BODY" "<!-- genai-saas review of $HEAD_SHA -->"
must_contain "$BODY" "Members get a discount" # the pull request's title reached the agent
if ! grep -Eq 'with [1-9][0-9]* related code snippet' <<<"$BODY"; then
  fail "the review used no code from the repository as context: $BODY"
fi
echo "review posted, with related code as context OK"
echo "----- the review -----"
echo "$BODY"
echo "----------------------"

# Uninstalling the app deletes the code we indexed for that installation.
ANSWER=$(deliver installation '{"action":"deleted","installation":{"id":2002}}')
if ! grep -Eq '"reason":"[1-9][0-9]* indexed code chunks deleted"' <<<"$ANSWER"; then
  fail "uninstalling the app should delete its indexed code: $ANSWER"
fi
echo "app uninstalled -> its code deleted OK"
echo "GitHub end-to-end test passed"
