"""Reviewing one pull request: everything the worker does for one job.

installation token ─► pull request still open, same commit? ─► daily limit
    ─► already reviewed? ─► diff ─► related code (embeddings) ─► agent
    ─► post the review on GitHub
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from .agent import AgentClient, AgentReview, Snippet
from .codeindex import CodeIndex
from .config import Settings
from .db import Sessions
from .embeddings import EmbeddingError
from .github import DiffTooLargeError, GitHubApp, GitHubError, PullRequest, RepositoryClient
from .jobs import Job, Result, reviews_posted_since
from .models import JobStatus

logger = logging.getLogger("worker")

MAX_REVIEW_CHARS: Final = 60_000  # GitHub's limit for a review body is 65,536
MAX_NOTES_CHARS: Final = 2_000  # the agent's limit for author notes
# Markdown code: fenced blocks and `inline spans`. Mentions inside code are
# not mentions, so they are left alone.
CODE: Final = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
MENTION: Final = re.compile(r"(?<![\w@`/])@([A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9._-]+)?)")


class SkipJobError(Exception):
    """Nothing to do for this job. The message says why and is saved with it."""


def review_marker(head_sha: str) -> str:
    # An HTML comment: invisible on GitHub, but there in the review's text.
    return f"<!-- genai-saas review of {head_sha} -->"


def escape_mentions(markdown: str) -> str:
    """Turn "@name" into "`@name`" outside code.

    The model's text is untrusted output: a diff could talk it into writing
    "@someone", and GitHub would notify that person in our app's name. In
    code formatting a mention is inert.
    """
    parts = CODE.split(markdown)  # odd items are the code
    return "".join(part if i % 2 else MENTION.sub(r"`@\1`", part) for i, part in enumerate(parts))


def render(review: AgentReview, head_sha: str, snippets: int) -> str:
    text = escape_mentions(review.text.strip())
    if len(text) > MAX_REVIEW_CHARS:
        text = text[:MAX_REVIEW_CHARS] + "\n\n*(Cut here: GitHub limits the size of a review.)*"
    notes = (
        ["*The review reached its length limit, so it may stop early.*"] if review.truncated else []
    )
    footer = (
        f"<sub>AI review of {head_sha[:7]} by {review.model}, with {snippets} related "
        "code snippet(s) from this repository as context. It can be wrong: check "
        "before acting on it.</sub>"
    )
    return "\n\n".join([text, *notes, "---", footer, review_marker(head_sha)])


def author_notes(pull: PullRequest) -> str:
    """The title and description tell the reviewer what the change is for."""
    return f"{pull.title}\n\n{pull.body or ''}".strip()[:MAX_NOTES_CHARS]


def check_still_current(pull: PullRequest, job: Job) -> None:
    if pull.state != "open":
        raise SkipJobError("the pull request is closed")
    if pull.draft:
        raise SkipJobError("the pull request is a draft now")
    if pull.head.sha != job.head_sha:
        raise SkipJobError("the pull request has newer commits, which get their own review")


@dataclass(frozen=True)
class Reviewer:
    github: GitHubApp
    agent: AgentClient
    index: CodeIndex
    sessions: Sessions
    settings: Settings

    async def run(self, job: Job, now: datetime) -> Result:
        repo = await self.github.repository(job, now)
        pull = await repo.pull_request(job.pull_number)
        check_still_current(pull, job)
        await self.check_daily_limit(job, now)
        if await repo.has_review(job.pull_number, review_marker(job.head_sha)):
            raise SkipJobError("this commit already has our review")
        try:
            diff = await repo.diff(job.pull_number, self.settings.max_diff_chars)
        except DiffTooLargeError as error:
            raise SkipJobError(str(error)) from error
        snippets = await self.related_code(repo, job, diff)
        review = await self.agent.review(diff, author_notes(pull), snippets, job.installation_id)
        body = render(review, job.head_sha, len(snippets))
        url = await repo.post_review(job.pull_number, job.head_sha, body)
        return Result(
            JobStatus.POSTED,
            f"review posted with {len(snippets)} related code snippet(s)",
            review_url=url,
            input_tokens=review.input_tokens,
            output_tokens=review.output_tokens,
        )

    async def check_daily_limit(self, job: Job, now: datetime) -> None:
        limit = self.settings.max_reviews_per_day
        posted = await reviews_posted_since(
            self.sessions, job.installation_id, now - timedelta(days=1)
        )
        if posted >= limit:
            raise SkipJobError(
                f"the daily limit of {limit} reviews for this installation is reached"
            )

    async def related_code(self, repo: RepositoryClient, job: Job, diff: str) -> list[Snippet]:
        """Context makes a review better, but a review without it beats no
        review: if indexing fails, carry on without context."""
        if self.settings.context_max_snippets == 0:
            return []
        try:
            report = await self.index.update(repo, job)
            snippets = await self.index.search(job, diff)
        except (GitHubError, EmbeddingError) as error:
            logger.warning("job %d: reviewing without code context: %s", job.id, error)
            return []
        logger.info(
            "job %d: index has %d of %d files (%d new, %d new chunks%s), %d related snippets",
            job.id,
            report.files,
            report.candidates,
            report.new_files,
            report.new_chunks,
            ", tree truncated by GitHub" if report.tree_truncated else "",
            len(snippets),
        )
        return snippets
