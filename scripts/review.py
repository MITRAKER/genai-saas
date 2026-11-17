#!/usr/bin/env python3
"""Stream an AI code review of a diff from your terminal.

    git diff | python3 scripts/review.py
    git diff main | python3 scripts/review.py --notes "focus on security"

Signs up a throwaway demo account unless you set EMAIL and PASSWORD (or
TOKEN). Uses only the standard library, so there is nothing to install.
"""

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "http://localhost:8000")
DIM, RESET = "\033[2m", "\033[0m"


def post(path: str, payload: dict, token: str | None = None):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, json.dumps(payload).encode(), headers)
    return urllib.request.urlopen(req)


def get_token() -> str:
    if os.environ.get("TOKEN"):
        return os.environ["TOKEN"]
    if os.environ.get("EMAIL"):
        creds = {"email": os.environ["EMAIL"], "password": os.environ["PASSWORD"]}
        return json.load(post("/api/auth/login", creds))["access_token"]
    creds = {"email": f"demo-{secrets.token_hex(4)}@example.com",
             "password": secrets.token_urlsafe(12)}
    return json.load(post("/api/auth/signup", creds))["access_token"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--notes", help="what the reviewer should focus on")
    args = parser.parse_args()

    diff = sys.stdin.read()
    if not diff.strip():
        print("No diff on stdin. Try: git diff | python3 scripts/review.py", file=sys.stderr)
        return 1

    try:
        resp = post("/api/reviews/stream", {"diff": diff, "notes": args.notes}, get_token())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        return 1

    # Read the SSE stream line by line as it arrives.
    event, thinking = None, False
    for raw in resp:
        line = raw.decode().rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
            if event == "thinking":
                if not thinking:
                    print(f"{DIM}[thinking] ", end="", file=sys.stderr)
                    thinking = True
                print(f"{DIM}{data['text']}{RESET}", end="", file=sys.stderr, flush=True)
            elif event == "text":
                if thinking:
                    print(RESET, file=sys.stderr)
                    thinking = False
                print(data["text"], end="", flush=True)
            elif event == "done":
                usage = data["usage"]
                print(f"\n{DIM}[{data['model']}: {usage['input_tokens']} tokens in, "
                      f"{usage['output_tokens']} out"
                      f"{', TRUNCATED' if data['truncated'] else ''}]{RESET}", file=sys.stderr)
            elif event == "error":
                print(f"\nError ({data['code']}): {data['message']}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
