import base64
import http.client
import json
import socket
import threading
import time

import pytest

import fake_github
from fake_github import BASE_SHA, FILES, HEAD_SHA, FakeGitHub

CLIENT = {"user-agent": "tests", "x-github-api-version": "2026-03-10"}
REPO = "/repos/demo-org/shop"
TOKEN_REQUEST = {
    "repository_ids": [fake_github.REPOSITORY_ID],
    "permissions": {"contents": "read", "pull_requests": "write"},
}


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def app_jwt(alg: str = "RS256", **changes) -> str:
    """A JWT with the claims the fake checks. The fake can't check signatures."""
    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 540, "iss": fake_github.APP_ID, **changes}
    return f"{b64({'alg': alg, 'typ': 'JWT'})}.{b64(claims)}.signature"


def call(fake, method, path, body=b"", token=None, **headers):
    headers = {**CLIENT, **headers}
    if token:
        headers["authorization"] = f"Bearer {token}"
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return fake.handle(method, path, headers, raw)


def mint(fake, jwt=None, installation=fake_github.INSTALLATION_ID, body=None):
    path = f"/app/installations/{installation}/access_tokens"
    return call(fake, "POST", path, TOKEN_REQUEST if body is None else body, jwt or app_jwt())


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def token(fake):
    reply = mint(fake)
    assert reply.status == 201
    return json.loads(reply.body)["token"]


def test_git_blob_sha_is_what_git_computes():
    # `git hash-object` of an empty file, and of "hello\n".
    assert fake_github.git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert fake_github.git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_health_and_inspection(fake):
    assert json.loads(fake.handle("GET", "/_fake/health", {}, b"").body) == {"status": "ok"}
    assert json.loads(fake.handle("GET", "/_fake/reviews", {}, b"").body) == []
    assert fake.handle("GET", "/_fake/other", {}, b"").status == 404


def test_requests_need_a_user_agent_and_a_known_api_version(fake, token):
    assert fake.handle("GET", f"{REPO}/pulls/7", {}, b"").status == 403
    assert call(fake, "GET", f"{REPO}/pulls/7", token=token).status == 200
    old = call(
        fake, "GET", f"{REPO}/pulls/7", token=token, **{"x-github-api-version": "2022-11-28"}
    )
    assert old.status == 200
    unknown = call(fake, "GET", f"{REPO}/pulls/7", token=token, **{"x-github-api-version": "1999"})
    assert unknown.status == 400


def test_the_app_jwt_mints_a_token_for_one_repository(fake):
    reply = mint(fake)
    assert reply.status == 201
    assert json.loads(reply.body)["token"] in fake.tokens


@pytest.mark.parametrize(
    "jwt",
    [
        app_jwt(exp=int(time.time()) - 1),  # expired
        app_jwt(iat=int(time.time()) + 300),  # not valid yet
        app_jwt(exp=int(time.time()) + 3600),  # lives longer than 10 minutes
        app_jwt(iss="999"),  # another app
        app_jwt(iat="yesterday"),
        app_jwt(alg="HS256"),
        "not-a-jwt",
        "a.b.c",  # not base64 JSON
        f"{b64({'alg': 'RS256'})}.{base64.urlsafe_b64encode(b'[1]').decode()}.sig",
    ],
)
def test_a_bad_app_jwt_mints_nothing(fake, jwt):
    assert mint(fake, jwt=jwt).status == 401
    assert fake.tokens == set()


def test_tokens_are_only_for_our_installation_and_least_privilege(fake):
    assert mint(fake, installation=1).status == 404
    everything = {"permissions": {"contents": "write", "pull_requests": "write"}}
    assert mint(fake, body=everything).status == 422
    assert mint(fake, body=b"not json").status == 422


def test_the_repository_needs_an_installation_token(fake, token):
    assert call(fake, "GET", f"{REPO}/pulls/7").status == 401
    assert call(fake, "GET", f"{REPO}/pulls/7", token=app_jwt()).status == 401
    assert call(fake, "GET", "/repos/someone/else/pulls/7", token=token).status == 404
    assert call(fake, "GET", f"{REPO}/issues", token=token).status == 404


def test_pull_request_and_its_diff(fake, token):
    pull = json.loads(call(fake, "GET", f"{REPO}/pulls/7", token=token).body)
    assert (pull["head"]["sha"], pull["base"]["sha"]) == (HEAD_SHA, BASE_SHA)
    diff = call(fake, "GET", f"{REPO}/pulls/7", token=token, accept=fake_github.DIFF_MEDIA_TYPE)
    assert diff.body.decode() == fake_github.DIFF
    assert diff.content_type.startswith(fake_github.DIFF_MEDIA_TYPE)


def test_tree_and_blobs_hold_the_files(fake, token):
    tree = json.loads(
        call(fake, "GET", f"{REPO}/git/trees/{BASE_SHA}?recursive=1", token=token).body
    )
    files = {}
    for entry in tree["tree"]:
        if entry["type"] == "blob":
            blob = json.loads(
                call(fake, "GET", f"{REPO}/git/blobs/{entry['sha']}", token=token).body
            )
            files[entry["path"]] = base64.b64decode(blob["content"]).decode()
    assert files == FILES
    assert call(fake, "GET", f"{REPO}/git/blobs/{'f' * 40}", token=token).status == 404


@pytest.mark.parametrize(
    "review",
    [
        {"event": "APPROVE", "commit_id": HEAD_SHA, "body": "LGTM"},
        {"event": "REQUEST_CHANGES", "commit_id": HEAD_SHA, "body": "No."},
        {"event": "COMMENT", "commit_id": BASE_SHA, "body": "Old commit."},
        {"event": "COMMENT", "commit_id": HEAD_SHA, "body": ""},
        ["not", "a", "review"],
    ],
)
def test_only_comments_on_the_head_commit_are_accepted(fake, token, review):
    assert call(fake, "POST", f"{REPO}/pulls/7/reviews", review, token=token).status == 422
    assert fake.reviews == []


def test_posted_reviews_are_listed_in_pages(fake, token):
    for n in range(3):
        review = {"event": "COMMENT", "commit_id": HEAD_SHA, "body": f"Review {n}"}
        reply = call(fake, "POST", f"{REPO}/pulls/7/reviews", review, token=token)
        assert reply.status == 200
    posted = json.loads(reply.body)
    assert posted["html_url"] == f"{fake_github.PULL_URL}#pullrequestreview-3"

    def page(query):
        reply = call(fake, "GET", f"{REPO}/pulls/7/reviews?{query}", token=token)
        return [review["body"] for review in json.loads(reply.body)]

    assert page("per_page=2&page=1") == ["Review 0", "Review 1"]
    assert page("per_page=2&page=2") == ["Review 2"]
    assert page("per_page=junk&page=0") == ["Review 0", "Review 1", "Review 2"]
    assert len(json.loads(fake.handle("GET", "/_fake/reviews", {}, b"").body)) == 3


@pytest.mark.parametrize(
    ("value", "number"), [("12", 12), ("", 30), ("-1", 30), ("²", 30), ("9" * 20, 30)]
)
def test_numbers_from_requests(value, number):
    assert fake_github.to_int(value, 30) == number


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def health(port: int) -> http.client.HTTPResponse:
    """GET /_fake/health, retrying until the server listens."""
    for _ in range(100):
        # A new connection each time: one whose connect failed can't be reused.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/_fake/health")
            return connection.getresponse()
        except ConnectionRefusedError:
            time.sleep(0.05)
    raise AssertionError("the fake GitHub did not start")


def test_main_serves_the_fake_over_http(monkeypatch, capsys):
    monkeypatch.setattr(fake_github, "MAX_REQUESTS", 2)
    port = free_port()
    argv = ["--host", "127.0.0.1", "--port", str(port)]
    # A daemon thread can't keep the test run alive if something goes wrong.
    server = threading.Thread(target=fake_github.main, args=(argv,), daemon=True)
    server.start()
    assert json.loads(health(port).read()) == {"status": "ok"}

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("POST", f"{REPO}/pulls/7/reviews", body=b"{}", headers={"User-Agent": "t"})
    response = connection.getresponse()
    assert response.status == 401
    assert response.getheader("Content-Type") == "application/json; charset=utf-8"
    server.join(timeout=5)
    assert not server.is_alive()  # it stopped after MAX_REQUESTS
    assert f"fake GitHub listening on 127.0.0.1:{port}" in capsys.readouterr().out
