"""Talking to the GitHub REST API as our GitHub App.

A GitHub App has two kinds of credentials:

1. Its private key. It signs a JWT that lives 10 minutes and says "I am app
   12345". That JWT can do almost nothing by itself, except:
2. mint an *installation token* for one installation (one account that
   installed the app). Installation tokens expire after an hour, and we ask
   for the smallest one that does the job: one repository, read its code,
   write pull request reviews (least privilege).
"""

import base64
from datetime import datetime
from http import HTTPStatus
from types import MappingProxyType
from typing import Any, Final

import httpx2
import jwt
from pydantic import BaseModel, Field, TypeAdapter

from .jobs import Job

TOKEN_PERMISSIONS: Final = MappingProxyType({"contents": "read", "pull_requests": "write"})
DIFF_MEDIA_TYPE: Final = "application/vnd.github.diff"
REVIEWS_PER_PAGE: Final = 100
MAX_REVIEW_PAGES: Final = 5
CLOCK_DRIFT_SECONDS: Final = 60  # GitHub's advice: backdate iat for clock drift
JWT_LIFETIME_SECONDS: Final = 600  # the most GitHub accepts


class GitHubError(Exception):
    """A GitHub API call failed. `retryable`: can trying again later help?"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class DiffTooLargeError(Exception):
    """The pull request is too large to review in one go."""


class InstallationToken(BaseModel):
    token: str = Field(min_length=1)


class Commit(BaseModel):
    sha: str


class PullRequest(BaseModel):
    number: int
    state: str
    draft: bool = False
    title: str = ""
    body: str | None = None
    head: Commit
    base: Commit


class TreeEntry(BaseModel):
    path: str
    type: str  # "blob" (a file), "tree" (a folder) or "commit" (a submodule)
    sha: str
    size: int | None = None  # only files have one


class Tree(BaseModel):
    tree: list[TreeEntry]
    truncated: bool = False  # GitHub's limit: 100,000 entries or 7 MB


class Blob(BaseModel):
    content: str
    encoding: str


class PostedReview(BaseModel):
    id: int
    body: str | None = None
    html_url: str = ""


REVIEW_LIST: Final = TypeAdapter(list[PostedReview])


def app_jwt(app_id: str, private_key: str, now: datetime) -> str:
    """The app's own credential, signed with its private key (RS256)."""
    issued_at = int(now.timestamp()) - CLOCK_DRIFT_SECONDS
    claims = {"iat": issued_at, "exp": issued_at + JWT_LIFETIME_SECONDS, "iss": app_id}
    return jwt.encode(claims, private_key, algorithm="RS256")


def check(response: httpx2.Response) -> None:
    """Raise GitHubError for a failed call, saying whether a retry can help."""
    if response.is_success:
        return
    code = response.status_code
    rate_limited = code == HTTPStatus.TOO_MANY_REQUESTS or (
        code == HTTPStatus.FORBIDDEN
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        )
    )
    request = response.request
    message = f"GitHub answered {code} to {request.method} {request.url.path}"
    raise GitHubError(message, retryable=rate_limited or code >= HTTPStatus.INTERNAL_SERVER_ERROR)


async def call(
    http: httpx2.AsyncClient, method: str, url: str, token: str, **options: Any
) -> httpx2.Response:
    try:
        response = await http.request(
            method, url, headers={"Authorization": f"Bearer {token}"}, **options
        )
    except httpx2.RequestError as error:
        raise GitHubError(
            f"could not reach GitHub ({type(error).__name__})", retryable=True
        ) from error
    check(response)
    return response


class GitHubApp:
    def __init__(self, http: httpx2.AsyncClient, app_id: str, private_key: str) -> None:
        if not app_id or not private_key:
            raise ValueError("a GitHub App needs its id and its private key")
        self.http = http
        self.app_id = app_id
        self.private_key = private_key

    async def repository(self, job: Job, now: datetime) -> "RepositoryClient":
        """A client for the job's repository, with a token scoped to just that."""
        response = await call(
            self.http,
            "POST",
            f"/app/installations/{job.installation_id}/access_tokens",
            app_jwt(self.app_id, self.private_key, now),
            json={"repository_ids": [job.repository_id], "permissions": dict(TOKEN_PERMISSIONS)},
        )
        token = InstallationToken.model_validate(response.json()).token
        return RepositoryClient(self.http, token, job.repository)


class RepositoryClient:
    """One repository, reached with one installation token."""

    def __init__(self, http: httpx2.AsyncClient, token: str, full_name: str) -> None:
        self.http = http
        self.token = token
        self.base = f"/repos/{full_name}"

    async def pull_request(self, number: int) -> PullRequest:
        response = await call(self.http, "GET", f"{self.base}/pulls/{number}", self.token)
        return PullRequest.model_validate(response.json())

    async def diff(self, number: int, max_chars: int) -> str:
        """The pull request's diff, read only up to max_chars (JPL rule 3)."""
        headers = {"Authorization": f"Bearer {self.token}", "Accept": DIFF_MEDIA_TYPE}
        url = f"{self.base}/pulls/{number}"
        try:
            async with self.http.stream("GET", url, headers=headers) as response:
                # GitHub's answer for a diff too big to produce at all.
                if response.status_code == HTTPStatus.NOT_ACCEPTABLE:
                    raise DiffTooLargeError("the diff is too large for GitHub to produce")
                check(response)
                return await read_text(response, max_chars)
        except httpx2.RequestError as error:
            message = f"could not reach GitHub ({type(error).__name__})"
            raise GitHubError(message, retryable=True) from error

    async def has_review(self, number: int, marker: str) -> bool:
        """Is there already a review with this marker in its body?

        The marker names the commit, so a job that runs twice (a worker died
        after posting, say) finds its own review instead of posting another.
        """
        for page in range(1, MAX_REVIEW_PAGES + 1):
            params = {"per_page": REVIEWS_PER_PAGE, "page": page}
            response = await call(
                self.http, "GET", f"{self.base}/pulls/{number}/reviews", self.token, params=params
            )
            reviews = REVIEW_LIST.validate_python(response.json())
            if any(marker in (review.body or "") for review in reviews):
                return True
            if len(reviews) < REVIEWS_PER_PAGE:
                return False
        return False

    async def post_review(self, number: int, commit_sha: str, body: str) -> str:
        """Post the review and return its URL.

        Always event=COMMENT. The bot must never approve or block a pull
        request, whatever a diff manages to talk the model into writing.
        """
        review = {"commit_id": commit_sha, "body": body, "event": "COMMENT"}
        url = f"{self.base}/pulls/{number}/reviews"
        response = await call(self.http, "POST", url, self.token, json=review)
        return PostedReview.model_validate(response.json()).html_url

    async def tree(self, commit_sha: str) -> Tree:
        """Every file and folder of the repository at this commit."""
        url = f"{self.base}/git/trees/{commit_sha}"
        response = await call(self.http, "GET", url, self.token, params={"recursive": "1"})
        return Tree.model_validate(response.json())

    async def blob(self, sha: str) -> bytes:
        """A file's content, by its blob sha (the sha of the content itself)."""
        response = await call(self.http, "GET", f"{self.base}/git/blobs/{sha}", self.token)
        blob = Blob.model_validate(response.json())
        if blob.encoding != "base64":
            raise GitHubError(f"unexpected blob encoding {blob.encoding!r}", retryable=False)
        return base64.b64decode(blob.content)


async def read_text(response: httpx2.Response, max_chars: int) -> str:
    parts: list[str] = []
    size = 0
    async for part in response.aiter_text():
        size += len(part)
        if size > max_chars:
            raise DiffTooLargeError(f"the diff is over {max_chars} characters")
        parts.append(part)
    return "".join(parts)
