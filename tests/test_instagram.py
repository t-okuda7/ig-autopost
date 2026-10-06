"""本物の代わりに、手元で立てた小さなサーバーに向けて通信の形を確かめる。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from autopost.instagram import Instagram, InstagramError, InstagramUnknown

TOKEN = "IGTOKEN-abc123"


class Handler(BaseHTTPRequestHandler):
    seen = []
    auth = []
    replies = {}

    def log_message(self, *args):
        pass

    def _reply(self, method):
        url = urlparse(self.path)
        if method == "POST":
            length = int(self.headers.get("content-length", 0))
            params = parse_qs(self.rfile.read(length).decode())
        else:
            params = parse_qs(url.query)
        params = {k: v[0] for k, v in params.items()}
        Handler.seen.append((method, url.path, params))
        Handler.auth.append(self.headers.get("authorization"))
        status, body = Handler.replies.get((method, url.path), (200, {"id": "42"}))
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply("GET")

    def do_POST(self):
        self._reply("POST")


@pytest.fixture
def api():
    Handler.seen, Handler.auth, Handler.replies = [], [], {}
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Instagram(TOKEN, base=f"http://127.0.0.1:{server.server_port}", timeout=5)
    server.shutdown()


def test_single_image_request(api):
    assert api.create_image("999", "https://x/01.jpg", caption="こんにちは\n#東京") == "42"
    method, path, params = Handler.seen[0]
    assert (method, path) == ("POST", "/999/media")
    assert params == {"image_url": "https://x/01.jpg", "caption": "こんにちは\n#東京"}
    assert Handler.auth == [f"Bearer {TOKEN}"]  # トークンはURLや本文に入れない


def test_carousel_requests(api):
    api.create_image("999", "https://x/01.jpg", caption="無視される", carousel_item=True)
    api.create_carousel("999", ["1", "2", "3"], "本文")
    child, parent = Handler.seen[0][2], Handler.seen[1][2]
    assert child["is_carousel_item"] == "true" and "caption" not in child
    assert parent == {"media_type": "CAROUSEL", "children": "1,2,3", "caption": "本文"}


def test_publish_returns_the_whole_reply(api):
    Handler.replies[("POST", "/999/media_publish")] = (
        200, {"id": "777", "config_issue": "CAPTION_NOT_ATTACHED"})
    assert api.publish("999", "42") == {"id": "777", "config_issue": "CAPTION_NOT_ATTACHED"}
    assert Handler.seen[0][2]["creation_id"] == "42"


def test_me_accepts_both_shapes(api):
    Handler.replies[("GET", "/me")] = (200, {"user_id": "999", "username": "a", "id": "1"})
    assert api.user_id() == "999"
    Handler.replies[("GET", "/me")] = (200, {"data": [{"user_id": "888", "username": "b"}]})
    assert api.user_id() == "888"
    assert Handler.seen[0][2]["fields"] == "user_id,username"


def test_wait_ready(api):
    Handler.replies[("GET", "/42")] = (200, {"status_code": "FINISHED"})
    api.wait_ready("42", timeout=1, interval=0)
    Handler.replies[("GET", "/42")] = (200, {"status_code": "ERROR"})
    with pytest.raises(InstagramError):
        api.wait_ready("42", timeout=1, interval=0)


def test_api_error_is_a_clear_refusal_without_the_token(api):
    Handler.replies[("POST", "/999/media")] = (400, {"error": {
        "message": f"Bad aspect ratio for token {TOKEN}", "code": 36003,
        "error_subcode": 2207009}})
    with pytest.raises(InstagramError) as error:
        api.create_image("999", "https://x/01.jpg", caption="x")
    assert "36003" in str(error.value) and "2207009" in str(error.value)
    assert TOKEN not in str(error.value)


def test_server_trouble_is_unknown_not_refusal(api):
    Handler.replies[("POST", "/999/media_publish")] = (502, {})
    with pytest.raises(InstagramUnknown):
        api.publish("999", "42")


def test_connection_failure_is_unknown_and_hides_the_token():
    api = Instagram(TOKEN, base="http://127.0.0.1:9", timeout=2)
    with pytest.raises(InstagramUnknown) as error:
        api.publish("999", "42")
    assert TOKEN not in str(error.value)


def test_refresh_request(api):
    Handler.replies[("GET", "/refresh_access_token")] = (
        200, {"access_token": "NEW", "token_type": "bearer", "expires_in": 5184000})
    assert api.refresh_token()["access_token"] == "NEW"
    assert Handler.seen[0][2] == {"grant_type": "ig_refresh_token", "access_token": TOKEN}


def test_recent_media(api):
    Handler.replies[("GET", "/999/media")] = (200, {"data": [{"id": "1", "caption": "a"}]})
    assert api.recent_media("999") == [{"id": "1", "caption": "a"}]
    assert Handler.seen[0][2]["fields"] == "id,caption,timestamp,permalink"
    assert Handler.seen[0][2]["limit"] == "50"


def test_recent_media_on_an_empty_account_is_an_empty_list(api):
    Handler.replies[("GET", "/999/media")] = (200, {"data": []})
    assert api.recent_media("999") == []


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": "oops"}, ["not", "a", "dict"]])
def test_unreadable_media_list_is_a_failure_not_an_empty_feed(api, body):
    Handler.replies[("GET", "/999/media")] = (200, body)
    with pytest.raises(InstagramUnknown):
        api.recent_media("999")


def test_recent_media_follows_pages_when_asked(api):
    Handler.replies[("GET", "/999/media")] = (200, {
        "data": [{"id": "1"}], "paging": {"cursors": {"after": "CUR"}, "next": "https://x"}})
    assert len(api.recent_media("999", pages=3)) == 3
    assert [seen[2].get("after") for seen in Handler.seen] == [None, "CUR", "CUR"]
    Handler.seen.clear()
    assert len(api.recent_media("999")) == 1 and len(Handler.seen) == 1


def test_container_error_can_be_retried(api):
    Handler.replies[("GET", "/42")] = (200, {"status_code": "ERROR"})
    with pytest.raises(InstagramError) as error:
        api.wait_ready("42", timeout=1, interval=0)
    assert error.value.retryable


def test_server_side_codes_are_marked(api):
    Handler.replies[("POST", "/999/media_publish")] = (
        400, {"error": {"message": "unexpected", "code": 2}})
    with pytest.raises(InstagramError) as error:
        api.publish("999", "42")
    assert error.value.server_side


def test_errors_are_sorted_into_retry_or_not(api):
    Handler.replies[("POST", "/999/media")] = (400, {"error": {"message": "limit", "code": 4}})
    with pytest.raises(InstagramError) as busy:
        api.create_image("999", "https://x/01.jpg")
    assert busy.value.retryable and not busy.value.token_problem

    Handler.replies[("POST", "/999/media")] = (400, {"error": {"message": "expired", "code": 190}})
    with pytest.raises(InstagramError) as expired:
        api.create_image("999", "https://x/01.jpg")
    assert expired.value.retryable and expired.value.token_problem

    Handler.replies[("POST", "/999/media")] = (400, {"error": {"message": "ratio", "code": 36003}})
    with pytest.raises(InstagramError) as ratio:
        api.create_image("999", "https://x/01.jpg")
    assert not ratio.value.retryable


def test_token_with_special_characters_is_still_hidden():
    odd = "IG|tok+en/=="
    api = Instagram(odd, base="http://127.0.0.1:9", timeout=2)
    assert odd not in api._clean(f"GET /refresh_access_token?access_token={odd}")
    assert "IG%7Ctok%2Ben%2F%3D%3D" not in api._clean(
        "https://x/refresh_access_token?access_token=IG%7Ctok%2Ben%2F%3D%3D")
