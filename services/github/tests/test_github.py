import base64
import json
from datetime import UTC, datetime

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

import fake_github
from app.github import (
    MAX_REVIEW_PAGES,
    REVIEWS_PER_PAGE,
    DiffTooLargeError,
    GitHubApp,
    GitHubError,
    RepositoryClient,
    app_jwt,
    check,
)
from app.review import review_marker
from tests.fakes import DEMO_JOB, answering, github_client, unreachable

pytestmark = pytest.mark.anyio

REPOSITORY = f"{fake_github.OWNER}/{fake_github.NAME}"
TOKEN = "ghs_test-token"


@pytest.fixture
def fake() -> fake_github.FakeGitHub:
    fake = fake_github.FakeGitHub()
    fake.tokens.add(TOKEN)
    return fake


def test_the_app_jwt_is_signed_with_the_private_key_and_short_lived(private_key):
    now = datetime.now(UTC)
    token = app_jwt("12345", private_key, now)
    public_key = serialization.load_pem_private_key(private_key.encode(), None).public_key()
    claims = jwt.decode(token, public_key, algorithms=["RS256"])
    issued = int(now.timestamp()) - 60  # backdated: the clocks may disagree a little
    assert claims == {"iss": "12345", "iat": issued, "exp": issued + 600}


async def test_installation_tokens_are_scoped_to_one_repository(fake, private_key):
    seen = []
    async with github_client(fake, seen) as http:
        app = GitHubApp(http, fake_github.APP_ID, private_key)
        repo = await app.repository(DEMO_JOB, datetime.now(UTC))
    [request] = seen
    assert request.url.path == "/app/installations/2002/access_tokens"
    assert request.headers["authorization"].startswith("Bearer ey")  # the app's JWT
    assert json.loads(request.content) == {
        "repository_ids": [fake_github.REPOSITORY_ID],
        "permissions": {"contents": "read", "pull_requests": "write"},
    }
    assert repo.token in fake.tokens
    assert repo.base == "/repos/demo-org/shop"


async def test_a_wrong_app_id_gets_no_token(fake, private_key):
    async with github_client(fake) as http:
        app = GitHubApp(http, "999", private_key)
        with pytest.raises(GitHubError, match="answered 401") as caught:
            await app.repository(DEMO_JOB, datetime.now(UTC))
    assert caught.value.retryable is False


async def test_a_github_app_needs_an_id_and_a_key():
    async with httpx2.AsyncClient() as http:
        with pytest.raises(ValueError, match="id and its private key"):
            GitHubApp(http, "", "key")
        with pytest.raises(ValueError, match="id and its private key"):
            GitHubApp(http, "12345", "")


def answer(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    request = httpx2.Request("GET", "https://api.github.test/repos/a/b/pulls/1")
    return httpx2.Response(status, headers=headers, request=request)


@pytest.mark.parametrize(
    ("status", "headers", "retryable"),
    [
        (401, {}, False),
        (403, {}, False),  # no permission: asking again won't help
        (404, {}, False),
        (422, {}, False),
        (403, {"x-ratelimit-remaining": "0"}, True),  # out of requests for the hour
        (403, {"retry-after": "60"}, True),  # secondary rate limit
        (429, {}, True),
        (500, {}, True),
        (502, {}, True),
    ],
)
def test_failed_calls_say_whether_a_retry_can_help(status, headers, retryable):
    with pytest.raises(GitHubError) as caught:
        check(answer(status, headers))
    assert caught.value.retryable is retryable
    assert str(caught.value) == f"GitHub answered {status} to GET /repos/a/b/pulls/1"


@pytest.mark.parametrize("status", [200, 201, 204])
def test_successful_calls_pass(status):
    check(answer(status))


async def test_pull_request(fake):
    async with github_client(fake) as http:
        pull = await RepositoryClient(http, TOKEN, REPOSITORY).pull_request(7)
    assert (pull.number, pull.state, pull.draft) == (7, "open", False)
    assert pull.head.sha == fake_github.HEAD_SHA
    assert pull.base.sha == fake_github.BASE_SHA
    assert pull.title == "Members get a discount"


async def test_only_an_installation_token_can_read_the_repository(fake):
    async with github_client(fake) as http:
        with pytest.raises(GitHubError, match="answered 401"):
            await RepositoryClient(http, "not-a-token", REPOSITORY).pull_request(7)


async def test_diff(fake):
    seen = []
    async with github_client(fake, seen) as http:
        diff = await RepositoryClient(http, TOKEN, REPOSITORY).diff(7, max_chars=10_000)
    assert diff == fake_github.DIFF
    assert seen[0].headers["accept"] == "application/vnd.github.diff"


async def test_a_diff_is_read_only_up_to_the_limit(fake):
    async with github_client(fake) as http:
        with pytest.raises(DiffTooLargeError, match="over 100 characters"):
            await RepositoryClient(http, TOKEN, REPOSITORY).diff(7, max_chars=100)


async def test_a_diff_too_large_for_github():
    async with answering(lambda _: httpx2.Response(406)) as http:
        with pytest.raises(DiffTooLargeError, match="too large for GitHub"):
            await RepositoryClient(http, TOKEN, REPOSITORY).diff(7, max_chars=100)


async def test_a_diff_that_fails_can_be_retried():
    async with answering(lambda _: httpx2.Response(503)) as http:
        with pytest.raises(GitHubError, match="answered 503") as caught:
            await RepositoryClient(http, TOKEN, REPOSITORY).diff(7, max_chars=100)
    assert caught.value.retryable is True


@pytest.mark.parametrize("call", ["diff", "pull_request"])
async def test_github_unreachable(call):
    async with answering(unreachable) as http:
        repo = RepositoryClient(http, TOKEN, REPOSITORY)
        request = repo.diff(7, 100) if call == "diff" else repo.pull_request(7)
        with pytest.raises(GitHubError, match=r"could not reach GitHub \(ConnectError\)") as caught:
            await request
    assert caught.value.retryable is True


async def test_post_review_comments_and_finds_its_marker(fake):
    marker = review_marker(fake_github.HEAD_SHA)
    async with github_client(fake) as http:
        repo = RepositoryClient(http, TOKEN, REPOSITORY)
        assert await repo.has_review(7, marker) is False
        url = await repo.post_review(7, fake_github.HEAD_SHA, f"Looks good.\n\n{marker}")
        assert await repo.has_review(7, marker) is True
        assert await repo.has_review(7, review_marker("d" * 40)) is False
    assert url == "https://github.com/demo-org/shop/pull/7#pullrequestreview-1"
    # The fake accepts only event=COMMENT: the bot never approves or blocks.
    assert fake.reviews[0]["state"] == "COMMENTED"


def review_pages(marker_page: int | None, seen: list[int]):
    """Full pages of other reviews, with our marker on one page (or none)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params["page"])
        seen.append(page)
        body = "ours: MARKER" if page == marker_page else "someone else's"
        reviews = [{"id": page * 1000 + i, "body": body} for i in range(REVIEWS_PER_PAGE)]
        return httpx2.Response(200, json=reviews)

    return handler


async def test_has_review_reads_more_pages_when_needed():
    seen = []
    async with answering(review_pages(3, seen)) as http:
        assert await RepositoryClient(http, TOKEN, REPOSITORY).has_review(7, "MARKER")
    assert seen == [1, 2, 3]


async def test_has_review_reads_a_bounded_number_of_pages():
    seen = []
    async with answering(review_pages(None, seen)) as http:
        assert not await RepositoryClient(http, TOKEN, REPOSITORY).has_review(7, "MARKER")
    assert seen == list(range(1, MAX_REVIEW_PAGES + 1))


async def test_tree_and_blobs(fake):
    seen = []
    async with github_client(fake, seen) as http:
        repo = RepositoryClient(http, TOKEN, REPOSITORY)
        tree = await repo.tree(fake_github.BASE_SHA)
        files = {e.path: await repo.blob(e.sha) for e in tree.tree if e.type == "blob"}
    assert seen[0].url.params["recursive"] == "1"
    assert tree.truncated is False
    assert files == {path: text.encode() for path, text in fake_github.FILES.items()}


async def test_an_unknown_blob(fake):
    async with github_client(fake) as http:
        with pytest.raises(GitHubError, match="answered 404") as caught:
            await RepositoryClient(http, TOKEN, REPOSITORY).blob("f" * 40)
    assert caught.value.retryable is False


async def test_a_blob_in_an_unexpected_encoding():
    content = base64.b64encode(b"x = 1").decode()
    blob = {"sha": "f" * 40, "content": content, "encoding": "utf-8"}
    async with answering(lambda _: httpx2.Response(200, json=blob)) as http:
        with pytest.raises(GitHubError, match="unexpected blob encoding 'utf-8'"):
            await RepositoryClient(http, TOKEN, REPOSITORY).blob("f" * 40)
