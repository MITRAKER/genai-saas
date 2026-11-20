"""GitHub webhooks: check the signature, read the event, queue the work.

GitHub signs every delivery with the secret we share with it: the header
X-Hub-Signature-256 is an HMAC-SHA256 of the raw request body. Anyone on the
internet can send a request to our webhook URL, so nothing in a delivery is
trusted, or even parsed, before that signature checks out.
"""

import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, Field

from .codeindex import forget_installation, forget_repositories
from .config import settings
from .db import Sessions
from .jobs import NewJob, enqueue

logger = logging.getLogger("webhooks")

SIGNATURE_PREFIX: Final = "sha256="
SHA: Final = r"^[0-9a-f]{40}([0-9a-f]{24})?$"  # SHA-1, or SHA-256 repositories
# "owner/name", as GitHub allows them. The name needs one character that is
# not a dot: it becomes part of API paths, where "." and ".." mean folders.
REPOSITORY_NAME: Final = (
    r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{0,99}[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$"
)
MAX_REPOSITORY_NAME: Final = 140  # 39 for the owner, "/", 100 for the name
# Pull request actions that put new code up for review.
REVIEW_ACTIONS: Final = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})


def signature_is_valid(secret: str, body: bytes, header: str | None) -> bool:
    if header is None or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # compare_digest takes as long for a near miss as for a wild guess, so the
    # response time can't tell an attacker how much of a forgery was right.
    return hmac.compare_digest(expected.encode(), header.removeprefix(SIGNATURE_PREFIX).encode())


class Delivery(BaseModel):
    """Our answer to GitHub, shown in the app's "Recent Deliveries" page."""

    status: Literal["pong", "queued", "duplicate", "ignored", "deleted"]
    reason: str = ""


# Only the fields we use. Pydantic ignores the rest and checks these (JPL rule 5).
class Account(BaseModel):
    login: str = Field(min_length=1, max_length=100)


class Repository(BaseModel):
    id: int = Field(gt=0)
    full_name: str = Field(max_length=MAX_REPOSITORY_NAME, pattern=REPOSITORY_NAME)
    owner: Account


class Installation(BaseModel):
    id: int = Field(gt=0)


class Commit(BaseModel):
    sha: str = Field(pattern=SHA)


class PullRequest(BaseModel):
    number: int = Field(gt=0)
    draft: bool = False
    head: Commit
    base: Commit


class PullRequestEvent(BaseModel):
    action: str
    pull_request: PullRequest
    repository: Repository
    installation: Installation


class InstallationEvent(BaseModel):
    action: str
    installation: Installation


class RemovedRepository(BaseModel):
    id: int = Field(gt=0)


class InstallationRepositoriesEvent(BaseModel):
    action: str
    installation: Installation
    repositories_removed: list[RemovedRepository] = Field(default=[], max_length=10_000)


def ignore_reason(event: PullRequestEvent, allowed: frozenset[str]) -> str | None:
    if event.action not in REVIEW_ACTIONS:
        return "this action adds no code to review"
    if event.pull_request.draft:
        return "draft pull requests are reviewed once they are ready"
    if event.repository.owner.login.lower() not in allowed:
        return "this account is not in GITHUB_ALLOWED_ACCOUNTS"
    return None


async def on_pull_request(sessions: Sessions, body: bytes, now: datetime) -> Delivery:
    event = PullRequestEvent.model_validate_json(body)
    reason = ignore_reason(event, settings.allowed_accounts())
    if reason is not None:
        return Delivery(status="ignored", reason=reason)
    pull, repository = event.pull_request, event.repository
    job = NewJob(
        installation_id=event.installation.id,
        repository_id=repository.id,
        repository=repository.full_name,
        pull_number=pull.number,
        head_sha=pull.head.sha,
        base_sha=pull.base.sha,
    )
    if not await enqueue(sessions, job, now):
        return Delivery(status="duplicate", reason="this commit is already queued or reviewed")
    # Only numbers in the log: text from a request could forge log lines.
    logger.info("queued a review of pull request %d in repository %d", pull.number, repository.id)
    return Delivery(status="queued")


async def on_installation(sessions: Sessions, body: bytes, _now: datetime) -> Delivery:
    event = InstallationEvent.model_validate_json(body)
    if event.action != "deleted":
        return Delivery(status="ignored", reason="only uninstalls need us to act")
    removed = await forget_installation(sessions, event.installation.id)
    return Delivery(status="deleted", reason=f"{removed} indexed code chunks deleted")


async def on_installation_repositories(sessions: Sessions, body: bytes, _now: datetime) -> Delivery:
    event = InstallationRepositoriesEvent.model_validate_json(body)
    if event.action != "removed":
        return Delivery(status="ignored", reason="only removed repositories need us to act")
    ids = [repository.id for repository in event.repositories_removed]
    removed = await forget_repositories(sessions, ids)
    return Delivery(status="deleted", reason=f"{removed} indexed code chunks deleted")


type Handler = Callable[[Sessions, bytes, datetime], Awaitable[Delivery]]

# Read-only: nothing can add an event handler at run time (JPL rule 6).
HANDLERS: Final[MappingProxyType[str, Handler]] = MappingProxyType(
    {
        "pull_request": on_pull_request,
        "installation": on_installation,
        "installation_repositories": on_installation_repositories,
    }
)
