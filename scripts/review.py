#!/usr/bin/env python3
"""Stream an AI code review of a diff from your terminal.

    git diff | python3 scripts/review.py
    git diff main | python3 scripts/review.py --notes "focus on security"

Signs up a throwaway demo account unless you set EMAIL and PASSWORD (or
TOKEN). Set BASE to use a gateway other than http://localhost:8000.
Uses only the standard library, so there is nothing to install.
"""

import argparse
import itertools
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import IO, Any

BASE = os.environ.get("BASE", "http://localhost:8000")
SCHEMES = ("http://", "https://")
DIM, RESET = "\033[2m", "\033[0m"
MAX_LINES = 1_000_000  # JPL rule 2: even reading a stream has a fixed upper bound


class CliError(Exception):
    """A problem worth one line on the terminal instead of a traceback."""


def post(path: str, payload: dict[str, Any], token: str | None = None) -> IO[bytes]:
    url = BASE + path
    # urllib also opens file:// and other schemes, so only ever give it a web URL.
    if not url.startswith(SCHEMES):
        raise CliError(f"BASE must start with http:// or https://, not {BASE!r}")
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, body, headers)  # noqa: S310 - scheme checked above
    response: IO[bytes] = urllib.request.urlopen(request)  # noqa: S310 - scheme checked above
    return response


def access_token(path: str, credentials: dict[str, str]) -> str:
    token = json.load(post(path, credentials)).get("access_token")
    if not isinstance(token, str):
        raise CliError(f"no access token in the response from {path}")
    return token


def get_token() -> str:
    if os.environ.get("TOKEN"):
        return os.environ["TOKEN"]
    if os.environ.get("EMAIL"):
        creds = {"email": os.environ["EMAIL"], "password": os.environ.get("PASSWORD", "")}
        return access_token("/api/auth/login", creds)
    creds = {
        "email": f"demo-{secrets.token_hex(4)}@example.com",
        "password": secrets.token_urlsafe(12),
    }
    return access_token("/api/auth/signup", creds)


def read_diff() -> str:
    diff = sys.stdin.read()
    if not diff.strip():
        raise CliError("no diff on stdin. Try: git diff | python3 scripts/review.py")
    return diff


def open_review(diff: str, notes: str | None) -> IO[bytes]:
    """Start the review and return the open event stream."""
    try:
        return post("/api/reviews/stream", {"diff": diff, "notes": notes}, get_token())
    except urllib.error.HTTPError as error:  # the API answered with an error status
        raise CliError(f"HTTP {error.code}: {error.read().decode()}") from error
    except urllib.error.URLError as error:  # nothing answered at all
        raise CliError(f"could not reach {BASE} ({error.reason}). Is it running?") from error


def sse_events(lines: Iterable[bytes]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Turn raw SSE lines into (event name, data) pairs as they arrive."""
    event = "message"
    for raw in itertools.islice(lines, MAX_LINES):
        line = raw.decode().rstrip("\r\n")
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            yield event, json.loads(line.removeprefix("data: "))


@dataclass
class Display:
    """Prints the review as it streams: thinking dimmed, the review normal."""

    thinking: bool = False

    def show_thinking(self, text: str) -> None:
        if not self.thinking:
            print(f"{DIM}[thinking] ", end="", file=sys.stderr)
            self.thinking = True
        print(f"{DIM}{text}{RESET}", end="", file=sys.stderr, flush=True)

    def show_text(self, text: str) -> None:
        if self.thinking:
            print(RESET, file=sys.stderr)
            self.thinking = False
        print(text, end="", flush=True)

    def show(self, event: str, data: dict[str, Any]) -> int | None:
        """Print one event. Returns an exit code once the review is over."""
        if event == "thinking":
            self.show_thinking(data["text"])
        elif event == "text":
            self.show_text(data["text"])
        elif event == "done":
            usage = data["usage"]
            truncated = ", TRUNCATED" if data["truncated"] else ""
            print(
                f"\n{DIM}[{data['model']}: {usage['input_tokens']} tokens in, "
                f"{usage['output_tokens']} out{truncated}]{RESET}",
                file=sys.stderr,
            )
            return 0
        elif event == "error":
            print(f"\nError ({data['code']}): {data['message']}", file=sys.stderr)
            return 1
        return None


def show_review(stream: Iterable[bytes]) -> int:
    """Print every event until the review ends; return the exit code."""
    display = Display()
    for event, data in sse_events(stream):
        exit_code = display.show(event, data)
        if exit_code is not None:
            return exit_code
    raise CliError("the stream ended without a result")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stream an AI code review of a diff.")
    parser.add_argument("--notes", help="what the reviewer should focus on")
    args = parser.parse_args()
    try:
        exit_code = show_review(open_review(read_diff(), args.notes))
    except CliError as error:
        print(f"\nError: {error}", file=sys.stderr)
        return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
