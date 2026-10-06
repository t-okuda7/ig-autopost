"""原稿の状態ごとに、投稿する・待つ・止める、が正しく分かれるか。"""
import unicodedata
from datetime import datetime

import pytest

from autopost import main
from autopost.draft import JST
from autopost.drive import DOC, FOLDER, DriveFile
from autopost.instagram import InstagramError, InstagramUnknown

from conftest import DRAFTS, MATERIAL, POSTED, FakeInstagram, draft_text, jpeg_bytes

BEFORE = datetime(2026, 10, 8, 20, 52, tzinfo=JST)
AFTER = datetime(2026, 10, 8, 21, 7, tzinfo=JST)
LATER = datetime(2026, 10, 8, 21, 22, tzinfo=JST)
NEXT_DAY = datetime(2026, 10, 9, 21, 7, tzinfo=JST)


def go(w, mode="run", now=AFTER):
    w.report.lines.clear()
    w.report.failures = 0
    return main.run(mode, drive=w.drive, ig=w.ig, host=w.host, cfg=w.cfg, now=now,
                    report=w.report)


def doc(w, file_id="doc"):
    return w.drive.files[file_id]


def said(w, text):
    return any(text in line for line in w.report.lines)


def preview_dir(w, file_id="doc"):
    return w.cfg.preview_dir / main._preview_name(doc(w, file_id))


def other_shop(w, doc_id="doc2", folder_id="shop2", name="別の店", opening="別の店の夜"):
    """別の店の素材フォルダと、その承認済みの原稿を足す。"""
    text = draft_text(photos="A").replace("素材フォルダ: ミクロとくじら", f"素材フォルダ: {name}") \
        .replace("幡ヶ谷の夜", opening)
    w.drive.add(folder_id, name, FOLDER, MATERIAL)
    w.drive.add(folder_id + "-a", "A.jpg", "image/jpeg", folder_id, blob=jpeg_bytes(1200, 1500))
    w.drive.add(doc_id, f"原稿_{name}", DOC, DRAFTS, text=text)
    return text


# ------------------------------------------------------------ ふつうの流れ

def test_draft_is_left_alone(world):
    world.drive.texts["doc"] = draft_text(status="下書き")
    assert go(world) == 0
    assert world.ig.calls == [] and world.drive.log == []


@pytest.mark.parametrize("status", ["承認済み", "承認 ", "OK", "approved", "承認?", ""])
def test_only_the_exact_word_approves(world, status):
    world.drive.texts["doc"] = draft_text(status=status)
    go(world)
    published = len(world.ig.published)
    assert published == (1 if status.strip() == "承認" else 0)


def test_approved_but_not_yet_time(world):
    assert go(world, now=BEFORE) == 0
    assert world.ig.calls == []
    assert doc(world).props["ig_status"] == "scheduled"
    assert "予約済み" in doc(world).description
    assert doc(world).name == "原稿_ミクロとくじら"
    writes = len(world.drive.log)
    go(world, now=BEFORE)
    assert len(world.drive.log) == writes  # 2回目は書き換えない


def test_due_post_is_published_as_carousel(world):
    assert go(world) == 0
    kinds = [c[0] for c in world.ig.calls]
    assert kinds == ["image", "image", "image", "wait", "wait", "wait", "carousel", "wait",
                     "publish"]
    children = [c for c in world.ig.calls if c[0] == "image"]
    assert all(c[3] is True and c[2] is None for c in children)  # 子にはキャプションを付けない
    caption = next(c for c in world.ig.calls if c[0] == "carousel")[2]
    assert caption.startswith("幡ヶ谷の夜") and caption.endswith("#tokyoizakaya")
    assert world.host.pushed == [["01.jpg", "02.jpg", "03.jpg"]]

    assert doc(world).name == "済_原稿_ミクロとくじら"
    assert doc(world).props["ig_status"] == "posted"
    assert doc(world).props["ig_media_id"] == "media123"
    assert "instagram.com" in doc(world).description
    assert doc(world, "shop").parents == [POSTED]
    assert doc(world, "shop").props["ig_used"] == "doc"


def test_single_photo_is_a_plain_image_post(world):
    world.drive.texts["doc"] = draft_text(photos="IMG_4036")
    assert go(world) == 0
    assert [c[0] for c in world.ig.calls] == ["image", "wait", "publish"]
    assert world.ig.calls[0][2].startswith("幡ヶ谷の夜")


def test_photos_are_posted_in_the_written_order(world):
    world.drive.texts["doc"] = draft_text(photos="IMG_4032, IMG_4040, IMG_4036")
    go(world)
    assert world.host.pushed == [["01.jpg", "02.jpg", "03.jpg"]]
    # 1枚目が横長なので、全体が横長の比率になる（順番が効いている証拠）
    world2_first = world.ig.calls[0][1]
    assert world2_first.endswith("/01.jpg")


# ------------------------------------------------------------ 二重投稿をしない

def test_published_post_is_never_posted_twice(world):
    go(world)
    go(world, now=NEXT_DAY)
    assert len(world.ig.published) == 1


def test_copied_draft_does_not_double_post_in_the_same_run(world):
    world.drive.add("copy", "原稿_ミクロとくじら のコピー", DOC, DRAFTS, text=draft_text())
    assert go(world) == 1
    assert world.ig.published == []  # どちらが本物か分からないので、どちらも止める
    assert doc(world).name.startswith("要対応_") and doc(world, "copy").name.startswith("要対応_")
    assert "同じ素材フォルダ" in doc(world).description


def test_copied_draft_appearing_later_is_stopped(world):
    go(world)
    world.drive.add("copy", "原稿_ミクロとくじら のコピー", DOC, DRAFTS, text=draft_text())
    assert go(world, now=LATER) == 1
    assert len(world.ig.published) == 1
    assert doc(world, "copy").name.startswith("要対応_")


def test_copy_does_not_post_even_if_the_folder_was_put_back(world):
    go(world)
    world.drive.files["shop"].parents = [MATERIAL]  # 手で 01_素材 に戻した
    world.drive.add("copy", "原稿_コピー", DOC, DRAFTS, text=draft_text())
    go(world, now=LATER)
    assert len(world.ig.published) == 1
    assert "すでに別の投稿に使われている" in doc(world, "copy").description


def test_same_caption_already_on_instagram_is_not_posted_again(world):
    caption = main.drafts.parse(draft_text()).caption
    world.ig.feed.append({"id": "old", "caption": caption.replace("\n", "\r\n") + " \n",
                          "permalink": "https://www.instagram.com/p/OLD/"})
    assert go(world) == 1
    assert world.ig.published == [] and world.host.pushed == []
    # この原稿が出したものとは限らないので、勝手に「済」にはせず、人に判断を求める
    assert doc(world).name.startswith("要対応_")
    assert "OLD" in doc(world).description and "済_" in doc(world).description
    assert doc(world, "shop").parents == [MATERIAL]
    assert "ig_used" not in doc(world, "shop").props


def test_a_different_post_with_the_same_opening_is_not_suppressed(world):
    opening = "あ" * 200  # 先頭200文字がまったく同じで、後ろだけ違う別の投稿
    world.ig.feed.append({"id": "old", "caption": opening + " 新宿の店の話。 #新宿グルメ",
                          "permalink": "https://www.instagram.com/p/OLD/"})
    world.drive.texts["doc"] = draft_text().replace("幡ヶ谷の夜、赤いネオンの麺酒場。", opening)
    assert go(world) == 0
    assert len(world.ig.published) == 1


def test_untried_drafts_check_one_page_and_retried_ones_look_further_back(world):
    go(world)
    assert world.ig.lookups == [1]
    world.ig = FakeInstagram(publish_error="unknown")
    other_shop(world)
    go(world, now=LATER)                       # 別の店：公開できたか分からないまま終わる
    go(world, now=LATER)                       # 要対応になる
    doc(world, "doc2").name = "原稿_別の店"
    world.ig.lookups.clear()
    go(world, now=LATER)
    assert world.ig.lookups == [4]


def test_copy_of_a_posted_draft_does_not_inherit_its_done_mark(world):
    go(world)
    inherited = dict(doc(world).props)  # Driveのコピーが印まで写したと仮定する
    world.drive.add("shop2", "別の店", FOLDER, MATERIAL)
    world.drive.add("q1", "A.jpg", "image/jpeg", "shop2", blob=jpeg_bytes(1200, 1500))
    text = draft_text(photos="A").replace("素材フォルダ: ミクロとくじら", "素材フォルダ: 別の店") \
        .replace("幡ヶ谷の夜、赤いネオンの麺酒場。", "まったく別の店の話。")
    world.drive.add("copy", "原稿_別の店", DOC, DRAFTS, text=text, props=inherited)
    assert go(world, now=LATER) == 0
    assert len(world.ig.published) == 2
    assert doc(world, "copy").props["ig_doc"] == "copy"


# ------------------------------------------------------------ 公開できたか分からないとき

def test_unknown_result_is_handed_to_a_human(world):
    world.ig = FakeInstagram(publish_error="unknown")
    assert go(world) == 1
    assert doc(world).props["ig_status"] == "publishing"
    assert doc(world).name == "原稿_ミクロとくじら"

    world.ig = FakeInstagram()
    assert go(world, now=LATER) == 1  # 次の実行では投稿せず、人に確認を求める
    assert world.ig.calls == []
    assert doc(world).name.startswith("要対応_")
    assert doc(world).props["ig_status"] == "unconfirmed"
    assert "Instagramを開いて" in doc(world).description


def test_rename_back_after_unknown_does_not_double_post_if_it_had_landed(world):
    world.ig = FakeInstagram(publish_error="unknown", lands_anyway=True)
    go(world)
    go(world, now=LATER)                      # 要対応になる
    doc(world).name = "原稿_ミクロとくじら"     # 確かめずに「要対応_」を消してしまった
    world.ig.publish_error = None
    go(world, now=LATER)
    assert len(world.ig.published) == 1       # 最初の1回だけ
    assert doc(world).name.startswith("済_")


def test_rename_back_after_unknown_posts_if_it_had_not_landed(world):
    world.ig = FakeInstagram(publish_error="unknown")
    go(world)
    go(world, now=LATER)
    doc(world).name = "原稿_ミクロとくじら"
    world.ig.publish_error = None
    assert go(world, now=LATER) == 0
    assert len(world.ig.published) == 2 and len(world.ig.feed) == 1
    assert doc(world).name.startswith("済_")


def test_rejection_that_actually_landed_is_caught_on_retry(world):
    world.ig = FakeInstagram(publish_error="rejected", lands_anyway=True)
    go(world)
    assert doc(world).name.startswith("要対応_")
    doc(world).name = "原稿_ミクロとくじら"
    world.ig.publish_error = None
    go(world, now=LATER)
    assert len(world.ig.published) == 1
    assert doc(world).name.startswith("済_")


# ------------------------------------------------------------ 原稿に問題があるとき

def test_unchecked_caption_is_stopped(world):
    world.drive.texts["doc"] = draft_text(mark="【要確認】")
    assert go(world, now=BEFORE) == 1  # 時刻を待たずに、承認した時点で知らせる
    assert world.ig.calls == []
    assert doc(world).name == "要対応_原稿_ミクロとくじら"
    assert "要確認" in doc(world).description


def test_fixing_and_renaming_back_publishes(world):
    world.drive.texts["doc"] = draft_text(mark="【要確認】")
    go(world)
    go(world)  # 要対応のあいだは何度動いても触らない
    assert world.ig.calls == []
    world.drive.texts["doc"] = draft_text()
    doc(world).name = "原稿_ミクロとくじら"
    assert go(world) == 0
    assert len(world.ig.published) == 1


def test_missing_photo_is_stopped(world):
    world.drive.texts["doc"] = draft_text(photos="IMG_4040, IMG_9999")
    assert go(world) == 1
    assert world.ig.calls == []
    assert "IMG_9999" in doc(world).description


def test_missing_folder_is_stopped(world):
    world.drive.texts["doc"] = draft_text().replace("素材フォルダ: ミクロとくじら",
                                                    "素材フォルダ: どこかの店")
    assert go(world) == 1
    assert "どこかの店" in doc(world).description


def test_broken_settings_block_is_reported(world):
    world.drive.texts["doc"] = draft_text().replace("=== 投稿設定ここまで ===", "")
    assert go(world) == 1
    assert world.ig.calls == []
    assert doc(world).name.startswith("要対応_")


def test_old_draft_without_settings_is_mentioned_but_not_a_failure(world):
    world.drive.texts["doc"] = "昔の形式の原稿。投稿設定のブロックが無い。"
    assert go(world) == 0
    assert said(world, "対象外") and world.drive.log == []


def test_overdue_post_is_not_published_late(world):
    assert go(world, now=NEXT_DAY) == 1
    assert world.ig.calls == []
    assert doc(world).name.startswith("要対応_")


# ------------------------------------------------------------ あいまいな名前は勝手に選ばない

def test_two_files_with_the_same_name_stop_the_post(world):
    world.drive.add("p36b", "IMG_4036.jpg", "image/jpeg", "shop", blob=jpeg_bytes(100, 125))
    assert go(world) == 1
    assert world.ig.calls == []
    assert "当てはまるファイルが2つ" in doc(world).description


def test_same_stem_different_extension_needs_the_extension(world):
    world.drive.add("cover_png", "表紙.png", "image/png", "shop", blob=jpeg_bytes(1200, 1500))
    world.drive.add("cover_jpg", "表紙.jpg", "image/jpeg", "shop", blob=jpeg_bytes(1200, 1500))
    world.drive.texts["doc"] = draft_text(photos="表紙, IMG_4036")
    assert go(world) == 1 and world.ig.calls == []

    doc(world).name = "原稿_ミクロとくじら"
    world.drive.texts["doc"] = draft_text(photos="表紙.png, IMG_4036")
    found, problems = main.match_photos(["表紙.png"], world.drive.children("shop"))
    assert [f.id for f in found] == ["cover_png"] and problems == []
    assert go(world) == 0


def test_written_extension_is_respected(world):
    files = [DriveFile("a", "IMG_1.HEIC", "image/heif"), DriveFile("b", "IMG_1.jpg", "image/jpeg")]
    assert [f.id for f in main.match_photos(["IMG_1.heic"], files)[0]] == ["a"]
    assert [f.id for f in main.match_photos(["img_1.JPG"], files)[0]] == ["b"]
    found, problems = main.match_photos(["IMG_1.png"], files)
    assert found == [] and "見つからない" in problems[0]


def test_same_photo_named_two_ways_is_caught(world):
    world.drive.texts["doc"] = draft_text(photos="IMG_4036, IMG_4036.jpg")
    assert go(world) == 1 and world.ig.calls == []
    assert "2回" in doc(world).description


def test_two_folders_with_the_same_name_stop_the_post(world):
    world.drive.add("shop_dup", "ミクロとくじら", FOLDER, MATERIAL)
    assert go(world) == 1 and world.ig.calls == []
    assert "2つある" in doc(world).description


def test_folder_named_on_a_mac_still_matches(world):
    doc(world, "shop").name = unicodedata.normalize("NFD", "ミクロとくじら")
    assert doc(world, "shop").name != "ミクロとくじら"
    assert go(world) == 0 and len(world.ig.published) == 1


def test_non_image_files_are_never_picked(world):
    world.drive.add("vid", "IMG_5000.MOV", "video/quicktime", "shop", blob=b"x")
    world.drive.texts["doc"] = draft_text(photos="IMG_5000")
    assert go(world) == 1
    assert "見つからない" in doc(world).description


# ------------------------------------------------------------ 一時的な不調は原稿のせいにしない

def test_drive_hiccup_is_retried_not_blamed_on_the_draft(world):
    world.drive.download_fails = True
    assert go(world) == 1
    assert doc(world).name == "原稿_ミクロとくじら" and world.ig.calls == []
    world.drive.download_fails = False
    assert go(world, now=LATER) == 0 and len(world.ig.published) == 1


def test_expired_token_is_retried_and_explained(world):
    world.ig = FakeInstagram(lookup_error=InstagramError("expired", code=190, http=400))
    assert go(world) == 1
    assert doc(world).name == "原稿_ミクロとくじら"
    assert said(world, "トークン")


def test_lookup_failure_never_leads_to_a_blind_publish(world):
    world.ig = FakeInstagram(lookup_error=InstagramUnknown("timeout"))
    assert go(world) == 1
    assert world.ig.calls == [] and world.host.pushed == []


def test_busy_instagram_at_publish_is_retried(world):
    world.ig = FakeInstagram(publish_error="busy")
    assert go(world) == 1
    assert doc(world).name == "原稿_ミクロとくじら"
    assert doc(world).props["ig_status"] == "scheduled"
    world.ig.publish_error = None
    assert go(world, now=LATER) == 0
    assert doc(world).name.startswith("済_")


def test_bad_image_is_the_drafts_problem(world):
    world.drive.blobs["p36"] = b"this is not a photo"
    assert go(world) == 1
    assert doc(world).name.startswith("要対応_")
    assert "変換できない" in doc(world).description


def test_refused_container_is_the_drafts_problem(world):
    world.ig = FakeInstagram(create_error=InstagramError("ratio", code=36003, http=400))
    assert go(world) == 1
    assert doc(world).name.startswith("要対応_")
    assert world.ig.published == []


def test_rejected_at_publish(world):
    world.ig = FakeInstagram(publish_error="rejected")
    assert go(world) == 1
    assert doc(world).name.startswith("要対応_")
    assert doc(world, "shop").parents == [MATERIAL]


def test_folder_mark_from_a_failed_attempt_does_not_block_a_replacement_draft(world):
    world.ig = FakeInstagram(publish_error="rejected")
    go(world)                                   # 素材フォルダに印が残る
    assert doc(world, "shop").props["ig_used"] == "doc"
    del world.drive.files["doc"]                # 原稿を作り直すことにして、古いほうを消した
    world.drive.add("new", "原稿_作り直し", DOC, DRAFTS,
                    text=draft_text().replace("幡ヶ谷の夜", "幡ヶ谷の夜、もう一度"))
    world.ig = FakeInstagram()
    assert go(world, now=LATER) == 0
    assert len(world.ig.published) == 1


def test_instagram_side_trouble_at_publish_is_not_assumed_to_have_failed(world):
    world.ig = FakeInstagram(publish_error="server", lands_anyway=True)
    assert go(world) == 1
    assert doc(world).props["ig_status"] == "publishing"   # 自動ではやり直さない
    world.ig.publish_error = None
    go(world, now=LATER)
    assert doc(world).name.startswith("要対応_") and len(world.ig.published) == 1


def test_published_without_caption_is_flagged(world):
    world.ig = FakeInstagram(config_issue="CAPTION_NOT_ATTACHED")
    assert go(world) == 1
    assert doc(world).name.startswith("済_")
    assert said(world, "CAPTION_NOT_ATTACHED")


def test_one_unreadable_draft_does_not_block_the_others(world):
    other_shop(world)
    del world.drive.texts["doc"]  # 1本目は読み出しに失敗する
    assert go(world) == 1
    assert len(world.ig.published) == 1
    assert doc(world, "doc2").name.startswith("済_")
    assert not said(world, "KeyError: 'doc'")  # エラーの中身（IDなど）は公開ログに出さない


def test_two_different_posts_due_together_both_go_out(world):
    other_shop(world)
    assert go(world) == 0
    assert len(world.ig.published) == 2 and len(world.host.pushed) == 2
    assert len(set(world.host.slugs)) == 2  # 同じ秒でも写真の置き場所がぶつからない


def test_repeated_transient_failure_is_reported_once_not_every_15_minutes(world):
    world.drive.download_fails = True
    assert go(world) == 1                       # 1回目は失敗として知らせる
    assert go(world, now=LATER) == 0            # 15分後の同じ失敗は、記録だけ
    assert said(world, "次の実行でやり直す")
    seven_hours = datetime(2026, 10, 9, 4, 30, tzinfo=JST)
    assert go(world, now=seven_hours) == 1      # 6時間たっても直っていなければ、もう一度知らせる


def test_draft_changed_during_preparation_is_not_published(world):
    def owner_takes_it_back():
        world.drive.texts["doc"] = draft_text(status="下書き")

    world.host.on_push = owner_takes_it_back
    assert go(world) == 0
    assert world.ig.published == []
    assert doc(world).name == "原稿_ミクロとくじら" and "ig_tried" not in doc(world).props


def test_caption_edited_during_preparation_is_not_published_in_its_old_form(world):
    def owner_edits():
        world.drive.texts["doc"] = draft_text().replace("幡ヶ谷の夜", "幡ヶ谷の深夜")

    world.host.on_push = owner_edits
    go(world)
    assert world.ig.published == []
    world.host.on_push = None
    go(world, now=LATER)
    caption = next(c for c in world.ig.calls if c[0] == "carousel" and "深夜" in c[2])[2]
    assert caption.startswith("幡ヶ谷の深夜") and len(world.ig.published) == 1


def test_unreadable_status_line_is_reported_instead_of_waiting_forever(world):
    world.drive.texts["doc"] = draft_text().replace("状態: 承認", "状態 承認")  # コロンを忘れた
    assert go(world) == 1
    assert world.ig.calls == []
    assert doc(world).name.startswith("要対応_") and "状態" in doc(world).description


def test_renaming_a_posted_draft_back_is_undone_and_said(world):
    go(world)
    doc(world).name = "原稿_ミクロとくじら"
    assert go(world, now=LATER) == 0
    assert doc(world).name.startswith("済_") and said(world, "すでに投稿済み")
    assert len(world.ig.published) == 1


# ------------------------------------------------------------ dry-run

def test_dry_run_changes_nothing_and_writes_preview(world):
    world.drive.texts["doc"] = draft_text(status="下書き")
    assert go(world, mode="dry-run", now=BEFORE) == 0
    assert world.drive.log == [] and world.ig.calls == [] and world.host.pushed == []
    names = sorted(p.name for p in preview_dir(world).iterdir())
    assert names == ["01.jpg", "02.jpg", "03.jpg", "caption.txt", "原稿名.txt"]
    assert said(world, "切られる")  # 横長の写真の警告


def test_dry_run_on_a_normal_unfinished_draft_is_not_an_error(world):
    world.drive.texts["doc"] = draft_text(status="下書き", mark="【要確認】")
    assert go(world, mode="dry-run") == 0
    assert said(world, "直すところ") and said(world, "要確認")
    assert (preview_dir(world) / "01.jpg").exists()  # 写真は見られる
    assert doc(world).name == "原稿_ミクロとくじら"


def test_dry_run_flags_an_approved_draft_that_would_not_post(world):
    world.drive.texts["doc"] = draft_text(mark="【要確認】")
    assert go(world, mode="dry-run") == 1
    assert world.drive.log == []


def test_dry_run_survives_missing_photos(world):
    world.drive.texts["doc"] = draft_text(status="下書き", photos="IMG_9999")
    assert go(world, mode="dry-run") == 0
    assert said(world, "IMG_9999")


# ------------------------------------------------------------ check

def test_check_reports_each_connection(world):
    world.drive.add(MATERIAL, "01_素材", FOLDER, "root")
    world.drive.add(DRAFTS, "02_原稿", FOLDER, "root")
    world.drive.add(POSTED, "03_投稿済み", FOLDER, "root")
    world.host.pushed.clear()
    code = main.check(drive=world.drive, ig=world.ig, host=world.host, cfg=world.cfg,
                      report=world.report)
    assert code == 0 and sum("✓" in line for line in world.report.lines) == 5
    assert "robot@" not in "\n".join(world.report.lines)  # 公開ログにメールアドレスを出さない


def test_check_fails_loudly(world):
    world.ig = FakeInstagram(lookup_error=InstagramError("bad token", code=190, http=400))
    code = main.check(drive=world.drive, ig=world.ig, host=world.host, cfg=world.cfg,
                      report=world.report)
    assert code == 1
    assert sum("✗" in line for line in world.report.lines) == 4  # Drive3つ + Instagram


def test_preview_folder_name_is_safe_for_any_draft_name(world):
    doc(world).name = "原稿_" + "とても長い店の名前" * 30 + "/../../外に出ようとする名前"
    world.drive.texts["doc"] = draft_text(status="下書き")
    assert go(world, mode="dry-run") == 0
    inside = [p for p in world.cfg.preview_dir.iterdir()]
    assert len(inside) == 1 and inside[0].parent == world.cfg.preview_dir
    assert (inside[0] / "原稿名.txt").read_text(encoding="utf-8") == doc(world).name
    assert not (world.cfg.preview_dir.parent / "外に出ようとする名前").exists()


def test_drive_error_text_is_not_copied_into_the_public_log(world):
    class Leaky(Exception):
        pass

    def boom(file_id, dest):
        raise Leaky("https://www.googleapis.com/drive/v3/files/SECRET-FILE-ID?alt=media")

    world.drive.download = boom
    go(world)
    assert said(world, "Leaky") and not said(world, "SECRET-FILE-ID")
