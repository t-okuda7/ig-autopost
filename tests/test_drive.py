"""Google Drive に送るリクエストの形を、にせものの通信で確かめる。"""
import json

from googleapiclient.http import HttpMockSequence

from autopost.drive import DOC, FOLDER, Drive


def _ok(body):
    return ({"status": "200"}, json.dumps(body) if not isinstance(body, bytes) else body)


class Recorder(HttpMockSequence):
    def __init__(self, replies):
        super().__init__(replies)
        self.seen = []

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        self.seen.append((method, uri, body))
        return super().request(uri, method=method, body=body, headers=headers, **kwargs)


def test_children_reads_every_page_and_the_marks():
    http = Recorder([
        _ok({"nextPageToken": "t2", "files": [
            {"id": "a", "name": "原稿_A", "mimeType": DOC, "parents": ["p"],
             "appProperties": {"ig_status": "posted"}}]}),
        _ok({"files": [{"id": "b", "name": "店", "mimeType": FOLDER, "parents": ["p"]}]}),
    ])
    files = Drive(http=http).children("p")
    assert [(f.id, f.name, f.mime) for f in files] == [("a", "原稿_A", DOC), ("b", "店", FOLDER)]
    assert files[0].props == {"ig_status": "posted"} and files[1].props == {}
    first = http.seen[0][1]
    assert "%27p%27+in+parents" in first and "trashed+%3D+false" in first
    assert "appProperties" in first and "pageToken=t2" in http.seen[1][1]


def test_export_text_strips_the_bom():
    http = Recorder([_ok("﻿=== 投稿設定 ===\r\n状態: 承認".encode("utf-8"))])
    text = Drive(http=http).export_text("doc1")
    assert text.startswith("=== 投稿設定 ===")
    assert "/files/doc1/export" in http.seen[0][1] and "mimeType=text%2Fplain" in http.seen[0][1]


def test_update_sends_only_what_changed():
    http = Recorder([_ok({"id": "doc1"}), _ok({"id": "doc1"})])
    drive = Drive(http=http)
    drive.update("doc1", name="済_原稿_A", description="投稿済み", props={"ig_status": "posted"})
    method, uri, body = http.seen[0]
    assert method == "PATCH" and "/files/doc1" in uri
    assert json.loads(body) == {"name": "済_原稿_A", "description": "投稿済み",
                                "appProperties": {"ig_status": "posted"}}
    drive.update("doc1", props={"ig_used": ""})
    assert json.loads(http.seen[1][2]) == {"appProperties": {"ig_used": ""}}


def test_update_with_nothing_makes_no_call():
    http = Recorder([])
    Drive(http=http).update("doc1")
    assert http.seen == []


def test_move_swaps_the_parent():
    http = Recorder([_ok({"id": "f1"})])
    Drive(http=http).move("f1", "posted", "material")
    method, uri, _ = http.seen[0]
    assert method == "PATCH" and "addParents=posted" in uri and "removeParents=material" in uri


def test_download_writes_the_file(tmp_path):
    http = Recorder([({"status": "200", "content-range": "bytes 0-4/5"}, b"photo")])
    dest = Drive(http=http).download("f1", tmp_path / "original" / "01.heic")
    assert dest.read_bytes() == b"photo"
    assert "alt=media" in http.seen[0][1]
