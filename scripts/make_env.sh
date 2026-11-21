#!/usr/bin/env bash
# Create .env from .env.example, with fresh random secrets. Safe to run again:
# an existing .env is kept, and only the secrets it lacks are added (say, the
# webhook secret, which an .env from an earlier milestone doesn't have).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -e .env ]; then
  cp .env.example .env
  echo "created .env from .env.example"
fi

for key in JWT_SECRET GITHUB_WEBHOOK_SECRET; do
  if grep -q "^$key=." .env; then
    continue # already set: keep it
  fi
  secret=$(openssl rand -hex 32)
  # Fill in "KEY=" where it stands, or add the line if .env has none. The
  # secret reaches awk through the environment, never on a command line.
  NEW_SECRET=$secret awk -v key="$key" '
    $0 == key "=" { print key "=" ENVIRON["NEW_SECRET"]; done = 1; next }
    { print }
    END { if (!done) print key "=" ENVIRON["NEW_SECRET"] }' .env >.env.new
  mv .env.new .env
  echo "set a new $key in .env"
done
chmod 600 .env # it holds secrets: readable by you only

# The GitHub App's private key goes in secrets/ (ignored by git). Creating the
# folder now keeps Docker from creating it as root when the stack starts.
mkdir -p secrets
