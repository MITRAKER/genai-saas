import io
import json
import urllib.error
from email.message import Message

import pytest

import review

DONE = {"model": "fake", "truncated": False, "usage": {"input_tokens": 3, "output_tokens": 5}}


def sse(*events):
    """Encode events the way the agent service streams them, one line at a time."""
    lines = []
    for event, data in events:
        lines += [f"event: {event}\n".encode(), f"data: {json.dumps(data)}\n".encode(), b"\n"]
    return lines


def stream(*events):
    return io.BytesIO(b"".join(sse(*events)))


@pytest.fixture(autouse=True)
def gateway(monkeypatch):
    """Every test talks to a made-up gateway and starts with no credentials."""
    monkeypatch.setattr(review, "BASE", "http://gateway.test")
    for name in ("TOKEN", "EMAIL", "PASSWORD"):
        monkeypatch.delenv(name, raising=False)


# --- Reading the stream -------------------------------------------------------


def test_sse_events_skips_keepalives():
    lines = [b": keepalive\n", b"\n", *sse(("text", {"text": "hi"}), ("done", DONE))]
    assert list(review.sse_events(lines)) == [("text", {"text": "hi"}), ("done", DONE)]


def test_sse_events_accepts_crlf_line_endings():
    lines = [b"event: text\r\n", b'data: {"text": "a"}\r\n']
    assert list(review.sse_events(lines)) == [("text", {"text": "a"})]


def test_sse_events_reads_at_most_max_lines(monkeypatch):
    monkeypatch.setattr(review, "MAX_LINES", 2)
    lines = sse(("text", {"text": "a"}), ("text", {"text": "b"}))
    assert list(review.sse_events(lines)) == [("text", {"text": "a"})]


def test_display_prints_thinking_review_and_usage(capsys):
    display = review.Display()
    assert display.show("thinking", {"text": "Reading. "}) is None
    assert display.show("thinking", {"text": "Checking."}) is None
    assert display.show("text", {"text": "## Summary"}) is None
    assert display.show("done", {**DONE, "truncated": True}) == 0
    out, err = capsys.readouterr()
    assert out == "## Summary"  # only the review goes to stdout, so it can be piped
    assert err.count("[thinking]") == 1
    assert "Reading. " in err
    assert "3 tokens in, 5 out, TRUNCATED" in err


def test_display_error_event_fails(capsys):
    assert review.Display().show("error", {"code": "timeout", "message": "Too slow."}) == 1
    assert "Error (timeout): Too slow." in capsys.readouterr().err


def test_display_ignores_unknown_events():
    assert review.Display().show("ping", {}) is None


# --- Talking to the API -------------------------------------------------------


def test_post_sends_json_with_bearer_token(monkeypatch):
    seen = {}

    def fake_urlopen(request):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    review.post("/api/x", {"a": 1}, token="t0k")
    assert seen["url"] == "http://gateway.test/api/x"
    assert seen["headers"]["Authorization"] == "Bearer t0k"
    assert seen["body"] == {"a": 1}


def test_post_without_token_sends_no_authorization(monkeypatch):
    seen = {}

    def fake_urlopen(request):
        seen["headers"] = dict(request.header_items())
        return io.BytesIO(b"{}")

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)
    review.post("/api/auth/login", {})
    assert "Authorization" not in seen["headers"]


def test_post_never_opens_non_web_urls(monkeypatch):
    monkeypatch.setattr(review, "BASE", "file:///etc")
    with pytest.raises(review.CliError, match="http:// or https://"):
        review.post("/passwd", {})


def answer_with(body, calls):
    def fake_post(path, payload, token=None):
        calls.append((path, payload))
        return io.BytesIO(body)

    return fake_post


def test_token_from_environment():
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TOKEN", "abc")
        assert review.get_token() == "abc"


def test_login_with_email_and_password(monkeypatch):
    monkeypatch.setenv("EMAIL", "dev@example.com")
    monkeypatch.setenv("PASSWORD", "correct horse")
    calls = []
    monkeypatch.setattr(review, "post", answer_with(b'{"access_token": "jwt"}', calls))
    assert review.get_token() == "jwt"
    assert calls == [("/api/auth/login", {"email": "dev@example.com", "password": "correct horse"})]


def test_demo_account_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(review, "post", answer_with(b'{"access_token": "jwt"}', calls))
    assert review.get_token() == "jwt"
    [(path, credentials)] = calls
    assert path == "/api/auth/signup"
    assert credentials["email"].endswith("@example.com")
    assert len(credentials["password"]) >= 8


def test_response_without_token_is_an_error(monkeypatch):
    monkeypatch.setattr(review, "post", answer_with(b'{"detail": "nope"}', []))
    with pytest.raises(review.CliError, match="no access token"):
        review.access_token("/api/auth/login", {})


def test_http_error_becomes_one_line(monkeypatch):
    def rate_limited(path, payload, token=None):
        body = io.BytesIO(b'{"detail": "Rate limit exceeded"}')
        raise urllib.error.HTTPError(path, 429, "Too Many Requests", Message(), body)

    monkeypatch.setenv("TOKEN", "t")
    monkeypatch.setattr(review, "post", rate_limited)
    with pytest.raises(review.CliError, match=r"HTTP 429: .*Rate limit exceeded"):
        review.open_review("diff", None)


def test_unreachable_gateway_becomes_one_line(monkeypatch):
    def refused(path, payload, token=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setenv("TOKEN", "t")
    monkeypatch.setattr(review, "post", refused)
    with pytest.raises(review.CliError, match=r"could not reach http://gateway\.test"):
        review.open_review("diff", None)


# --- The whole command --------------------------------------------------------


def run_main(monkeypatch, stdin, response=None, argv=("review.py",)):
    monkeypatch.setattr("sys.argv", list(argv))
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    monkeypatch.setenv("TOKEN", "t")
    sent = []

    def fake_post(path, payload, token=None):
        sent.append((path, payload, token))
        return response

    monkeypatch.setattr(review, "post", fake_post)
    return review.main(), sent


def test_main_streams_a_review(monkeypatch, capsys):
    response = stream(("text", {"text": "Looks good."}), ("done", DONE))
    argv = ("review.py", "--notes", "focus on tests")
    code, sent = run_main(monkeypatch, "+x = 1\n", response, argv)
    assert code == 0
    assert capsys.readouterr().out == "Looks good."
    assert sent == [("/api/reviews/stream", {"diff": "+x = 1\n", "notes": "focus on tests"}, "t")]


def test_main_fails_when_the_review_fails(monkeypatch):
    response = stream(("error", {"code": "refused", "message": "Declined."}))
    code, _ = run_main(monkeypatch, "+x = 1\n", response)
    assert code == 1


def test_main_fails_when_the_stream_stops_early(monkeypatch, capsys):
    code, _ = run_main(monkeypatch, "+x = 1\n", stream(("text", {"text": "partial"})))
    assert code == 1
    assert "Error: the stream ended without a result" in capsys.readouterr().err


def test_main_needs_a_diff(monkeypatch, capsys):
    code, sent = run_main(monkeypatch, "  \n")
    assert code == 1
    assert sent == []
    assert "Error: no diff on stdin" in capsys.readouterr().err
