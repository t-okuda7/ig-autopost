from datetime import datetime, timedelta

import pytest

from autopost import draft as drafts
from autopost.draft import JST

from conftest import draft_text


def test_reads_settings_and_caption():
    d = drafts.parse(draft_text())
    assert d.approved
    assert d.scheduled == datetime(2026, 10, 8, 21, 0, tzinfo=JST)
    assert d.folder == "ミクロとくじら"
    assert d.photos == ["IMG_4040", "IMG_4036", "IMG_4032"]
    lines = d.caption.split("\n")
    assert lines[0] == "幡ヶ谷の夜、赤いネオンの麺酒場。"
    assert lines[1] == ""  # 空行は残る
    assert lines[-1].startswith("#幡ヶ谷グルメ")
    assert "埋まっていない項目" not in d.caption  # キャプションの外は入らない


def test_google_docs_export_quirks():
    text = "﻿" + draft_text().replace("\n", "\r\n").replace("状態: 承認", "状態：承認　")
    d = drafts.parse(text)
    assert d is not None and d.approved
    assert "\r" not in d.caption


def test_no_settings_block_is_ignored():
    assert drafts.parse("ただのメモ\nキャプションだけ") is None


def test_draft_status_is_not_approved():
    assert not drafts.parse(draft_text(status="下書き")).approved
    assert not drafts.parse(draft_text(status="")).approved


def test_valid_draft_has_no_problems():
    assert drafts.validate(drafts.parse(draft_text())) == []


@pytest.mark.parametrize("mark", ["【要確認】", "要確認", "（要確認）", "【 要確認 】", "[要 確認]"])
def test_any_spelling_of_the_check_mark_blocks_posting(mark):
    problems = drafts.validate(drafts.parse(draft_text(mark=mark)))
    assert any("要確認" in p for p in problems)


@pytest.mark.parametrize("blank", ["¥___", "¥__", "¥＿"])
def test_blank_placeholder_blocks_posting(blank):
    d = drafts.parse(draft_text().replace("¥3,000〜¥4,000", blank))
    assert any("下線" in p for p in drafts.validate(d))


def test_single_underscore_in_a_handle_is_fine():
    d = drafts.parse(draft_text().replace("Mikro to Kujira", "@some_food_account"))
    assert drafts.validate(d) == []


def test_too_many_hashtags():
    d = drafts.parse(draft_text().replace("#tokyoizakaya", "#tokyoizakaya #一人飲み"))
    assert drafts.count_hashtags(d.caption) == 6
    assert any("ハッシュタグ" in p for p in drafts.validate(d))


def test_hashtags_are_counted_generously():
    # 数えすぎても止まるだけ。数え漏らすと上限を超えて出てしまうので、多めに数える
    assert drafts.count_hashtags("#a#b#c#d#e#f") == 6
    assert drafts.count_hashtags("。#a、#b ＃c") == 3
    assert drafts.count_hashtags("# だけ、や ## は数えない") == 0


def test_bad_date_and_reel_are_reported():
    d = drafts.parse(draft_text(when="10/8 夜").replace("形式: カルーセル", "形式: リール"))
    problems = drafts.validate(d)
    assert any("日時" in p for p in problems)
    assert any("リール" in p for p in problems)


def test_photo_limits():
    many = ", ".join(f"IMG_{i}" for i in range(11))
    assert any("上限" in p for p in drafts.validate(drafts.parse(draft_text(photos=many))))
    assert any("1枚も" in p for p in drafts.validate(drafts.parse(draft_text(photos=""))))
    assert any("2回" in p for p in drafts.validate(drafts.parse(draft_text(photos="A, a"))))


def test_due_and_expired():
    d = drafts.parse(draft_text())
    at = d.scheduled
    assert not d.is_due(at - timedelta(minutes=1))
    assert d.is_due(at) and not d.is_expired(at + timedelta(hours=11))
    assert d.is_expired(at + timedelta(hours=13))


def test_photo_names_may_contain_spaces():
    d = drafts.parse(draft_text(photos="IMG_4036 (1).jpg, 表紙 加工済み"))
    assert d.photos == ["IMG_4036 (1).jpg", "表紙 加工済み"]


def test_photo_list_broken_across_lines_is_reported_not_truncated():
    text = draft_text(photos="IMG_4040, IMG_4036,\nIMG_4032")
    d = drafts.parse(text)
    assert d.photos == ["IMG_4040", "IMG_4036"]
    assert any("読めない行" in p and "IMG_4032" in p for p in drafts.validate(d))


def test_unknown_setting_line_is_reported():
    d = drafts.parse(draft_text().replace("形式: カルーセル", "写 真の順番: 逆"))
    assert any("読めない行" in p for p in drafts.validate(d))


def test_full_width_markers_are_accepted():
    text = draft_text().replace("=== 投稿設定 ===", "＝＝＝ 投稿設定 ＝＝＝")
    assert drafts.parse(text).approved


def test_missing_end_marker_is_an_error_not_silence():
    with pytest.raises(drafts.DraftError):
        drafts.parse(draft_text().replace("=== 投稿設定ここまで ===", ""))
    with pytest.raises(drafts.DraftError):
        drafts.parse(draft_text().replace("=== キャプションここまで ===", ""))


def test_names_typed_on_a_mac_match():
    import unicodedata

    decomposed = unicodedata.normalize("NFD", draft_text())
    assert decomposed != draft_text()
    assert drafts.parse(decomposed).folder == "ミクロとくじら"
