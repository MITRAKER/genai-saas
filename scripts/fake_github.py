#!/usr/bin/env python3
"""A fake GitHub for the GitHub integration's end-to-end test and `make demo`.

    python3 scripts/fake_github.py          # listens on port 9000

It serves just enough of the REST API for one repository, demo-org/shop,
with one open pull request, and keeps the reviews the worker posts so a test
can read them back from GET /_fake/reviews. No GitHub account, no network,
no cost. The unit tests of services/github use the same fake, in memory.

Like the real GitHub, it checks credentials: the app's JWT can only mint an
installation token, and only such a token can read the repository. It checks
the JWT's claims but not its signature, because the standard library has no
RSA. Standard library only, so any Python 3.12 runs it: the demo stack
(docker-compose.e2e.yml) runs it in the github service's image.
"""

import argparse
import base64
import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Final, TypeGuard, cast
from urllib.parse import parse_qs, urlsplit

APP_ID: Final = "12345"
INSTALLATION_ID: Final = 2002
REPOSITORY_ID: Final = 1001
OWNER: Final = "demo-org"
NAME: Final = "shop"
REPO_PATH: Final = f"/repos/{OWNER}/{NAME}"
PULL_NUMBER: Final = 7
BASE_SHA: Final = "ba5e" * 10  # any 40 hex digits will do
HEAD_SHA: Final = "c0ffee" * 6 + "c0de"
PULL_URL: Final = f"https://github.com/{OWNER}/{NAME}/pull/{PULL_NUMBER}"
API_VERSIONS: Final = frozenset({"2022-11-28", "2026-03-10"})
PERMISSIONS: Final = {"contents": "read", "pull_requests": "write"}
JWT_LIFETIME_SECONDS: Final = 600
DIFF_MEDIA_TYPE: Final = "application/vnd.github.diff"
MAX_PER_PAGE: Final = 100
MAX_BODY_BYTES: Final = 1_048_576
MAX_REQUESTS: Final = 1_000_000  # JPL rule 2: even a server's loop has a bound
TOKEN_PATH: Final = re.compile(r"/app/installations/([0-9]{1,19})/access_tokens")
DIGITS: Final = re.compile(r"[0-9]{1,9}")
JWT_PARTS: Final = 3  # header.claims.signature

# The repository at the pull request's base commit.
FILES: Final = {
    "README.md": "# Shop\n\nA tiny shop, for demos.\n",
    "shop/__init__.py": "",
    "shop/pricing.py": (
        "TAX_RATE = 0.2\n"
        "\n"
        "\n"
        "def price_with_tax(price):\n"
        '    """The price a customer pays, tax included."""\n'
        "    return round(price * (1 + TAX_RATE), 2)\n"
    ),
    "shop/cart.py": (
        "from shop.pricing import price_with_tax\n"
        "\n"
        "\n"
        "def cart_total(items):\n"
        '    """What the whole cart costs, tax included."""\n'
        "    return sum(price_with_tax(item.price) * item.quantity for item in items)\n"
    ),
}

# The pull request's change, with a bug for a real model to find: the
# discount is subtracted from the price instead of multiplied with it.
# (A list of lines, so the blank context lines keep their leading space.)
DIFF: Final = "\n".join(
    [
        "diff --git a/shop/pricing.py b/shop/pricing.py",
        "--- a/shop/pricing.py",
        "+++ b/shop/pricing.py",
        "@@ -1,6 +1,9 @@",
        " TAX_RATE = 0.2",
        "+DISCOUNT = 0.1",
        " ",
        " ",
        "-def price_with_tax(price):",
        "+def price_with_tax(price, member=False):",
        '     """The price a customer pays, tax included."""',
        "-    return round(price * (1 + TAX_RATE), 2)",
        "+    if member:",
        "+        price = price * 1 - DISCOUNT",
        "+    return round(price * (1 + TAX_RATE), 2)",
        "",
    ]
)

PULL_REQUEST: Final = {
    "number": PULL_NUMBER,
    "state": "open",
    "draft": False,
    "title": "Members get a discount",
    "body": "Members pay 10% less.",
    "html_url": PULL_URL,
    "head": {"ref": "member-discount", "sha": HEAD_SHA},
    "base": {"ref": "main", "sha": BASE_SHA},
}

type Headers = Mapping[str, str]  # with lowercase names
type Query = dict[str, list[str]]


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


def json_reply(status: HTTPStatus, data: object) -> Reply:
    return Reply(status, json.dumps(data).encode())


def error(status: HTTPStatus) -> Reply:
    return json_reply(status, {"message": status.phrase})


def git_blob_sha(content: bytes) -> str:
    """The id git gives a file's content: SHA-1 of a short header and the bytes."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def build_tree() -> dict[str, Any]:
    entries: list[dict[str, Any]] = [
        {"path": "shop", "mode": "040000", "type": "tree", "sha": git_blob_sha(b"shop")}
    ]
    for path, text in sorted(FILES.items()):
        content = text.encode()
        entries.append(
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": git_blob_sha(content),
                "size": len(content),
            }
        )
    return {"sha": BASE_SHA, "tree": entries, "truncated": False}


TREE: Final = build_tree()
BLOBS: Final = {git_blob_sha(text.encode()): text.encode() for text in FILES.values()}


def parse_json(body: bytes) -> object:
    try:
        return json.loads(body)
    except ValueError:
        return None


def jwt_claims(token: str) -> dict[str, Any] | None:
    """The claims of an RS256 JWT, or None. The signature is not checked."""
    parts = token.split(".")
    if len(parts) != JWT_PARTS:
        return None
    try:
        header, claims = (
            json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            for part in parts[:2]
        )
    except ValueError:  # bad base64, bad UTF-8 or bad JSON
        return None
    if not isinstance(header, dict) or header.get("alg") != "RS256":
        return None
    return claims if isinstance(claims, dict) else None


def app_jwt_is_valid(token: str, now: float) -> bool:
    """Signed by our app (iss), valid now, and lives at most 10 minutes."""
    claims = jwt_claims(token) or {}
    issued, expires = claims.get("iat"), claims.get("exp")
    return (
        str(claims.get("iss")) == APP_ID
        and isinstance(issued, int)
        and isinstance(expires, int)
        and issued <= now < expires
        and expires - issued <= JWT_LIFETIME_SECONDS
    )


def token_request_problem(installation_id: int, app_jwt: str, body: bytes) -> HTTPStatus | None:
    if not app_jwt_is_valid(app_jwt, time.time()):
        return HTTPStatus.UNAUTHORIZED
    if installation_id != INSTALLATION_ID:
        return HTTPStatus.NOT_FOUND
    # The worker must ask for the least it needs: one repository, two permissions.
    wanted = {"repository_ids": [REPOSITORY_ID], "permissions": PERMISSIONS}
    if parse_json(body) != wanted:
        return HTTPStatus.UNPROCESSABLE_ENTITY
    return None


def client_problem(headers: Headers) -> Reply | None:
    """GitHub refuses requests without a User-Agent, and unknown API versions."""
    if not headers.get("user-agent"):
        return error(HTTPStatus.FORBIDDEN)
    version = headers.get("x-github-api-version")
    if version is not None and version not in API_VERSIONS:
        return error(HTTPStatus.BAD_REQUEST)
    return None


def to_int(value: str, default: int) -> int:
    """A small whole number from a request, or the default."""
    return int(value) if DIGITS.fullmatch(value) else default


def may_post(review: object) -> TypeGuard[dict[str, Any]]:
    """The worker may only ever comment, on the head commit: approving or
    blocking a pull request is for people."""
    return (
        isinstance(review, dict)
        and review.get("event") == "COMMENT"
        and review.get("commit_id") == HEAD_SHA
        and isinstance(review.get("body"), str)
        and bool(review["body"])
    )


class FakeGitHub:
    """The fake's state: the tokens it issued and the reviews it received."""

    def __init__(self) -> None:
        self.tokens: set[str] = set()
        self.reviews: list[dict[str, Any]] = []
        self.lock = threading.Lock()  # the HTTP server may answer from threads

    def handle(self, method: str, target: str, headers: Headers, body: bytes) -> Reply:
        """Answer one request. `target` is the path with its query string."""
        url = urlsplit(target)
        if url.path.startswith("/_fake/"):
            return self.inspect(url.path)
        problem = client_problem(headers)
        if problem is not None:
            return problem
        return self.api(method, url.path, parse_qs(url.query), headers, body)

    def inspect(self, path: str) -> Reply:
        """For tests: is the fake up, and which reviews were posted?"""
        if path == "/_fake/health":
            return json_reply(HTTPStatus.OK, {"status": "ok"})
        if path == "/_fake/reviews":
            with self.lock:
                return json_reply(HTTPStatus.OK, list(self.reviews))
        return error(HTTPStatus.NOT_FOUND)

    def api(self, method: str, path: str, query: Query, headers: Headers, body: bytes) -> Reply:
        token = headers.get("authorization", "").removeprefix("Bearer ")
        if method == "POST" and (match := TOKEN_PATH.fullmatch(path)):
            return self.mint_token(int(match[1]), token, body)
        if not path.startswith(REPO_PATH + "/"):
            return error(HTTPStatus.NOT_FOUND)
        with self.lock:
            issued = token in self.tokens
        if not issued:  # the app's JWT can't read code; only an installation token can
            return error(HTTPStatus.UNAUTHORIZED)
        return self.repository(method, path.removeprefix(REPO_PATH), query, headers, body)

    def mint_token(self, installation_id: int, app_jwt: str, body: bytes) -> Reply:
        problem = token_request_problem(installation_id, app_jwt, body)
        if problem is not None:
            return error(problem)
        token = "ghs_" + secrets.token_hex(18)
        with self.lock:
            self.tokens.add(token)
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        return json_reply(HTTPStatus.CREATED, {"token": token, "expires_at": expires_at})

    def repository(
        self, method: str, path: str, query: Query, headers: Headers, body: bytes
    ) -> Reply:
        if method == "GET" and path.startswith("/git/blobs/"):
            return blob(path.removeprefix("/git/blobs/"))
        pull = f"/pulls/{PULL_NUMBER}"
        routes: dict[tuple[str, str], Callable[[], Reply]] = {
            ("GET", pull): lambda: pull_request(headers),
            ("GET", pull + "/reviews"): lambda: self.list_reviews(query),
            ("POST", pull + "/reviews"): lambda: self.post_review(body),
            ("GET", f"/git/trees/{BASE_SHA}"): lambda: json_reply(HTTPStatus.OK, TREE),
        }
        route = routes.get((method, path))
        return route() if route else error(HTTPStatus.NOT_FOUND)

    def list_reviews(self, query: Query) -> Reply:
        per_page = min(to_int(query.get("per_page", [""])[0], 30), MAX_PER_PAGE)
        start = (max(to_int(query.get("page", [""])[0], 1), 1) - 1) * per_page
        with self.lock:
            return json_reply(HTTPStatus.OK, self.reviews[start : start + per_page])

    def post_review(self, body: bytes) -> Reply:
        review = parse_json(body)
        if not may_post(review):
            return error(HTTPStatus.UNPROCESSABLE_ENTITY)
        with self.lock:
            review_id = len(self.reviews) + 1
            posted = {
                "id": review_id,
                "user": {"login": "genai-saas[bot]", "type": "Bot"},
                "body": review["body"],
                "state": "COMMENTED",
                "commit_id": HEAD_SHA,
                "html_url": f"{PULL_URL}#pullrequestreview-{review_id}",
            }
            self.reviews.append(posted)
        return json_reply(HTTPStatus.OK, posted)


def pull_request(headers: Headers) -> Reply:
    if headers.get("accept") == DIFF_MEDIA_TYPE:
        return Reply(HTTPStatus.OK, DIFF.encode(), f"{DIFF_MEDIA_TYPE}; charset=utf-8")
    return json_reply(HTTPStatus.OK, PULL_REQUEST)


def blob(sha: str) -> Reply:
    content = BLOBS.get(sha)
    if content is None:
        return error(HTTPStatus.NOT_FOUND)
    # Like GitHub: base64 with line breaks in it.
    data = {"sha": sha, "size": len(content), "encoding": "base64"}
    return json_reply(HTTPStatus.OK, {**data, "content": base64.encodebytes(content).decode()})


class FakeServer(HTTPServer):
    def __init__(self, address: tuple[str, int], fake: FakeGitHub) -> None:
        super().__init__(address, Handler)
        self.fake = fake


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # the names http.server looks for
        self.answer()

    def do_POST(self) -> None:
        self.answer()

    def answer(self) -> None:
        length = to_int(self.headers.get("content-length", ""), 0)
        body = self.rfile.read(min(length, MAX_BODY_BYTES))
        headers = {name.lower(): value for name, value in self.headers.items()}
        reply = cast(FakeServer, self.server).fake.handle(self.command, self.path, headers, body)
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        self.wfile.write(reply.body)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="A fake GitHub for tests and demos.")
    # In a container the fake must listen on every interface to be reachable.
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - see above
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args(argv)
    with FakeServer((args.host, args.port), FakeGitHub()) as server:
        print(f"fake GitHub listening on {args.host}:{args.port}", flush=True)
        for _ in range(MAX_REQUESTS):
            server.handle_request()


if __name__ == "__main__":
    main()
