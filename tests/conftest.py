"""テスト用のにせもの。本物のDriveにもInstagramにも触らない。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopost.drive import DOC, FOLDER, DriveFile  # noqa: E402
from autopost.instagram import InstagramError, InstagramUnknown  # noqa: E402

MATERIAL, DRAFTS, POSTED = "material", "drafts", "posted"

DRAFT_TEXT = """原稿 ── ミクロとくじら（幡ヶ谷）

=== 投稿設定 ===
状態: {status}
日時: {when}
形式: カルーセル
素材フォルダ: ミクロとくじら
写真: {photos}
=== 投稿設定ここまで ===

=== キャプション ===
幡ヶ谷の夜、赤いネオンの麺酒場。

白い暖簾の向こうに、ステンレスのカウンター。

店名｜大衆麺酒場 ミクロとくじら 幡ヶ谷
予算｜¥3,000〜¥4,000{mark}

Mikro to Kujira — a noodle izakaya in Hatagaya, Tokyo.

#幡ヶ谷グルメ #幡ヶ谷居酒屋 #麺酒場 #東京居酒屋 #tokyoizakaya
=== キャプションここまで ===

【埋まっていない項目】
・予算は【要確認】
"""


def draft_text(status="承認", when="2026-10-08 21:00", photos="IMG_4040, IMG_4036, IMG_4032",
               mark=""):
    return DRAFT_TEXT.format(status=status, when=when, photos=photos, mark=mark)


def jpeg_bytes(width, height, color=(180, 60, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeDrive:
    email = "robot@example.iam.gserviceaccount.com"

    def __init__(self):
        self.files: dict[str, DriveFile] = {}
        self.texts: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}
        self.log: list[tuple] = []
        self.download_fails = False

    def add(self, file_id, name, mime, parent, *, text=None, blob=None, props=None):
        self.files[file_id] = DriveFile(file_id, name, mime, [parent], "", dict(props or {}))
        if text is not None:
            self.texts[file_id] = text
        if blob is not None:
            self.blobs[file_id] = blob
        return self.files[file_id]

    def get(self, file_id):
        return self.files[file_id]

    def children(self, folder_id):
        return [f for f in self.files.values() if folder_id in f.parents]

    def export_text(self, file_id):
        return self.texts[file_id]

    def download(self, file_id, dest):
        if self.download_fails:
            raise ConnectionError("drive is having a moment")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.blobs[file_id])
        return dest

    def update(self, file_id, *, name=None, description=None, props=None):
        self.log.append(("update", file_id, name, props))
        file = self.files[file_id]
        if name is not None:
            file.name = name
        if description is not None:
            file.description = description
        if props is not None:
            file.props.update(props)

    def move(self, file_id, new_parent, old_parent):
        self.log.append(("move", file_id, new_parent))
        self.files[file_id].parents = [new_parent]


class FakeInstagram:
    """publish すると、その投稿が「最近の投稿」に載る。本物と同じく、二重投稿の確認に使える。"""

    def __init__(self, publish_error=None, config_issue=None, create_error=None,
                 lookup_error=None, lands_anyway=False):
        self.calls: list[tuple] = []
        self.publish_error = publish_error
        self.config_issue = config_issue
        self.create_error = create_error
        self.lookup_error = lookup_error
        self.lands_anyway = lands_anyway  # エラーに見えたが、実は投稿されていた場合
        self.feed: list[dict] = []
        self.lookups: list[int] = []
        self._captions: dict[str, str] = {}
        self._next = 0

    def _id(self):
        self._next += 1
        return f"c{self._next}"

    def me(self):
        return {"user_id": "999", "username": "tester"}

    def user_id(self):
        return "999"

    def recent_media(self, user_id, limit=50, pages=1):
        self.lookups.append(pages)
        if self.lookup_error:
            raise self.lookup_error
        return list(self.feed)

    def create_image(self, user_id, image_url, caption=None, carousel_item=False):
        if self.create_error:
            raise self.create_error
        self.calls.append(("image", image_url, caption, carousel_item))
        new_id = self._id()
        self._captions[new_id] = caption or ""
        return new_id

    def create_carousel(self, user_id, children, caption):
        self.calls.append(("carousel", tuple(children), caption))
        new_id = self._id()
        self._captions[new_id] = caption
        return new_id

    def _land(self, creation_id):
        self.feed.insert(0, {"id": "media123", "caption": self._captions.get(creation_id, ""),
                             "permalink": "https://www.instagram.com/p/XXXX/"})

    def wait_ready(self, container_id, **_):
        self.calls.append(("wait", container_id))

    def publish(self, user_id, creation_id):
        self.calls.append(("publish", creation_id))
        if self.publish_error and self.lands_anyway:
            self._land(creation_id)
        if self.publish_error == "rejected":
            raise InstagramError("だめ（code=100, subcode=1）", code=100, subcode=1, http=400)
        if self.publish_error == "busy":
            raise InstagramError("混んでいる（code=4）", code=4, http=400)
        if self.publish_error == "server":
            raise InstagramError("Instagram側の不調（code=2）", code=2, http=400)
        if self.publish_error == "unknown":
            raise InstagramUnknown("timeout")
        self._land(creation_id)
        result = {"id": "media123"}
        if self.config_issue:
            result["config_issue"] = self.config_issue
        return result

    def permalink(self, media_id):
        return "https://www.instagram.com/p/XXXX/"

    @property
    def published(self):
        return [c for c in self.calls if c[0] == "publish"]


class FakeHost:
    def __init__(self):
        self.pushed: list[list[str]] = []
        self.slugs: list[str] = []
        self.on_push = None  # 準備の途中で何かが起きる、を再現するための差し込み口

    def push(self, files, slug):
        if self.on_push:
            self.on_push()
        self.slugs.append(slug)
        self.pushed.append([f.name for f in files])
        return [f"https://raw.example/{slug}/{f.name}" for f in files]

    def wait_public(self, url):
        pass


@pytest.fixture
def world(tmp_path):
    """素材フォルダ1つと原稿1本が入った、にせもののDrive一式。"""
    from autopost.main import Config, Report

    drive = FakeDrive()
    drive.add("shop", "ミクロとくじら", FOLDER, MATERIAL)
    drive.add("p40", "IMG_4040.HEIC", "image/heif", "shop", blob=jpeg_bytes(3024, 4032))
    drive.add("p36", "IMG_4036.jpg", "image/jpeg", "shop", blob=jpeg_bytes(3024, 4032))
    drive.add("p32", "IMG_4032.JPG", "image/jpeg", "shop", blob=jpeg_bytes(4032, 3024))
    drive.add("memo", "撮影メモ", DOC, "shop", text="メモ")
    drive.add("doc", "原稿_ミクロとくじら", DOC, DRAFTS, text=draft_text())

    class World:
        pass

    w = World()
    w.drive, w.ig, w.host = drive, FakeInstagram(), FakeHost()
    w.cfg = Config(MATERIAL, DRAFTS, POSTED, preview_dir=tmp_path / "preview")
    w.report = Report()
    return w
