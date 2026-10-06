"""Instagram API（Instagramログイン方式）の、投稿に必要な部分だけ。"""
from __future__ import annotations

import time
from urllib.parse import quote, quote_plus

import requests

BASE = "https://graph.instagram.com"

# 原稿のせいではなく、時間をおけば通るかもしれない失敗
#   1, 2      Instagram側の一時的な不調
#   4, 17, 32, 613  呼び出しが多すぎる
#   190       トークンが切れた・無効
#   9004, -2  写真をURLから取れなかった
RETRYABLE_CODES = {1, 2, 4, 17, 32, 613, 190, 9004, -2}
TOKEN_CODES = {190}


class InstagramError(Exception):
    """APIがはっきり「だめ」と返したとき。その呼び出しは実行されていない。"""

    def __init__(self, message: str, code: int | None = None, subcode: int | None = None,
                 http: int | None = None, retry: bool = False):
        super().__init__(message)
        self.code, self.subcode, self.http = code, subcode, http
        self._retry = retry

    @property
    def retryable(self) -> bool:
        return self._retry or self.code in RETRYABLE_CODES or self.http == 429

    @property
    def server_side(self) -> bool:
        """Instagram側の不調。「実行されなかった」と言い切れない種類の失敗。"""
        return self.code in (1, 2)

    @property
    def token_problem(self) -> bool:
        return self.code in TOKEN_CODES


class InstagramUnknown(Exception):
    """通信が途中で切れたなど、実行されたかどうか分からないとき。"""


class Instagram:
    def __init__(self, token: str, base: str = BASE, session: requests.Session | None = None,
                 timeout: int = 60):
        self._token = token
        self._base = base.rstrip("/")
        self._session = session or requests.Session()
        self._timeout = timeout

    # ---- 内部 ----
    def _clean(self, text: str) -> str:
        """エラー文にトークンが混ざっても外に出さない。"""
        if not self._token:
            return text
        for form in {self._token, quote(self._token, safe=""), quote_plus(self._token)}:
            text = text.replace(form, "***")
        return text

    def _call(self, method: str, path: str, *, token_in_query: bool = False, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        headers = {}
        if token_in_query:
            params["access_token"] = self._token
        else:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            if method == "GET":
                response = self._session.get(url, params=params, headers=headers,
                                             timeout=self._timeout)
            else:
                response = self._session.post(url, data=params, headers=headers,
                                              timeout=self._timeout)
        except requests.RequestException as exc:
            raise InstagramUnknown(self._clean(f"{method} {path}: {type(exc).__name__}")) from None

        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.status_code >= 500:
            raise InstagramUnknown(f"{method} {path}: HTTP {response.status_code}")
        if response.status_code >= 400 or "error" in body:
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            detail = error.get("error_user_msg") or error.get("message") or response.text[:300]
            code, sub = error.get("code"), error.get("error_subcode")
            raise InstagramError(self._clean(f"{detail}（code={code}, subcode={sub}）"),
                                 code=code, subcode=sub, http=response.status_code)
        return body

    # ---- 公開している操作 ----
    def me(self) -> dict:
        body = self._call("GET", "me", fields="user_id,username")
        # 応答が {"data": [{...}]} の形で返ることがあるので、どちらでも読めるようにする
        data = body.get("data")
        if isinstance(data, list) and data:
            return data[0]
        return body

    def user_id(self) -> str:
        me = self.me()
        return str(me.get("user_id") or me["id"])

    def recent_media(self, user_id: str, limit: int = 50, pages: int = 1) -> list[dict]:
        """最近の投稿。二重投稿を防ぐために、同じキャプションが無いか見るのに使う。

        一覧が読めなかったときは、空として扱わずに失敗にする（読めないまま投稿に進ませない）。
        """
        found: list[dict] = []
        after = None
        for _ in range(max(1, pages)):
            body = self._call("GET", f"{user_id}/media",
                              fields="id,caption,timestamp,permalink", limit=limit, after=after)
            data = body.get("data")
            if not isinstance(data, list):
                raise InstagramUnknown("最近の投稿の一覧を読めなかった")
            found.extend(item for item in data if isinstance(item, dict))
            paging = body.get("paging") if isinstance(body.get("paging"), dict) else {}
            cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
            after = cursors.get("after")
            if not after or not paging.get("next"):
                break
        return found

    def create_image(self, user_id: str, image_url: str, caption: str | None = None,
                     carousel_item: bool = False) -> str:
        body = self._call(
            "POST", f"{user_id}/media",
            image_url=image_url,
            caption=None if carousel_item else caption,
            is_carousel_item="true" if carousel_item else None,
        )
        return body["id"]

    def create_carousel(self, user_id: str, children: list[str], caption: str) -> str:
        body = self._call(
            "POST", f"{user_id}/media",
            media_type="CAROUSEL", children=",".join(children), caption=caption,
        )
        return body["id"]

    def status(self, container_id: str) -> str:
        return self._call("GET", container_id, fields="status_code").get("status_code", "")

    def wait_ready(self, container_id: str, timeout: int = 300, interval: int = 10) -> None:
        deadline = time.monotonic() + timeout
        while True:
            status = self.status(container_id)
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                # 写真のせいとは限らないので、次の実行でやり直せる失敗として扱う
                raise InstagramError(f"Instagram側で画像の処理に失敗した（{status}）", retry=True)
            if time.monotonic() > deadline:
                raise InstagramUnknown("Instagram側の画像処理が時間内に終わらなかった")
            time.sleep(interval)

    def publish(self, user_id: str, creation_id: str) -> dict:
        """公開する。{"id": ..., "config_issue": ...} をそのまま返す。"""
        return self._call("POST", f"{user_id}/media_publish", creation_id=creation_id)

    def permalink(self, media_id: str) -> str:
        try:
            return self._call("GET", media_id, fields="permalink").get("permalink", "")
        except (InstagramError, InstagramUnknown):
            return ""

    def refresh_token(self) -> dict:
        return self._call("GET", "refresh_access_token", token_in_query=True,
                          grant_type="ig_refresh_token")
