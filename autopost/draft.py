"""原稿（Googleドキュメントをテキストで書き出したもの）を読んで、投稿できる状態か確かめる。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

APPROVED = "承認"
MAX_CAPTION = 2200
MAX_HASHTAGS = 5
MAX_PHOTOS = 10
EXPIRE_AFTER = timedelta(hours=12)
KNOWN_KEYS = ("状態", "日時", "形式", "素材フォルダ", "写真")

# 原稿に残っていたら投稿しない印。【要確認】の括弧違いや、下線の穴埋めも拾う
_PLACEHOLDERS = (
    (re.compile(r"要\s*確認"), "要確認"),
    (re.compile(r"_{2,}|＿"), "___（穴埋めの下線）"),
)

_SETTINGS = re.compile(r"^[=＝]+\s*投稿設定\s*[=＝]+$")
_SETTINGS_END = re.compile(r"^[=＝]+\s*投稿設定ここまで\s*[=＝]+$")
_CAPTION = re.compile(r"^[=＝]+\s*キャプション\s*[=＝]+$")
_CAPTION_END = re.compile(r"^[=＝]+\s*キャプションここまで\s*[=＝]+$")
_KEY_VALUE = re.compile(r"^\s*([^:：]+?)\s*[:：]\s*(.*?)\s*$")
_HASHTAG = re.compile(r"[#＃](?=[^\s#＃])")
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M")


class DraftError(Exception):
    """原稿の形が崩れていて、読めないとき。"""


def nfc(text: str) -> str:
    """見た目が同じなら同じ文字列になるようにそろえる（Macで付けた名前の濁点など）。"""
    return unicodedata.normalize("NFC", text)


@dataclass
class Draft:
    status: str
    scheduled_raw: str
    scheduled: datetime | None
    format: str
    folder: str
    photos: list[str]
    caption: str
    unparsed: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.status == APPROVED

    def is_due(self, now: datetime) -> bool:
        return self.scheduled is not None and self.scheduled <= now

    def is_expired(self, now: datetime) -> bool:
        return self.scheduled is not None and now - self.scheduled > EXPIRE_AFTER


def _lines(text: str) -> list[str]:
    text = nfc(text.replace("﻿", "")).replace("\r\n", "\n").replace("\r", "\n")
    # Googleドキュメントの書き出しに混ざることがある特殊な空白と縦タブをそろえる
    text = text.replace(" ", " ").replace("\u000b", "\n")
    return [line.rstrip() for line in text.split("\n")]


def _between(lines: list[str], start: re.Pattern, end: re.Pattern, label: str) -> list[str] | None:
    begin = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if begin is None and start.match(stripped):
            begin = i + 1
        elif begin is not None and end.match(stripped):
            return lines[begin:i]
    if begin is not None:
        raise DraftError(f"「=== {label}ここまで ===」の行が見つからない")
    return None


def _parse_datetime(value: str) -> datetime | None:
    value = value.replace("　", " ").replace("：", ":").strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def parse(text: str) -> Draft | None:
    """投稿設定ブロックが無い原稿は None（対象外）。ブロックが崩れていれば DraftError。"""
    lines = _lines(text)
    settings = _between(lines, _SETTINGS, _SETTINGS_END, "投稿設定")
    if settings is None:
        return None

    values: dict[str, str] = {}
    unparsed: list[str] = []
    for line in settings:
        if not line.strip():
            continue
        match = _KEY_VALUE.match(line)
        if match and match.group(1) in KNOWN_KEYS and match.group(1) not in values:
            values[match.group(1)] = match.group(2)
        else:
            unparsed.append(line.strip())

    caption_lines = _between(lines, _CAPTION, _CAPTION_END, "キャプション") or []
    while caption_lines and not caption_lines[0].strip():
        caption_lines.pop(0)
    while caption_lines and not caption_lines[-1].strip():
        caption_lines.pop()

    photos = [p.strip() for p in re.split(r"[,、，]", values.get("写真", "")) if p.strip()]
    scheduled_raw = values.get("日時", "")

    return Draft(
        status=values.get("状態", "").strip(),
        scheduled_raw=scheduled_raw,
        scheduled=_parse_datetime(scheduled_raw),
        format=values.get("形式", "").strip(),
        folder=values.get("素材フォルダ", "").strip(),
        photos=photos,
        caption="\n".join(caption_lines),
        unparsed=unparsed,
    )


def count_hashtags(caption: str) -> int:
    """# の数を数える。数えすぎる分には止まるだけなので、多めに数える側に倒している。"""
    return len(_HASHTAG.findall(caption))


def find_placeholders(caption: str) -> list[str]:
    return [label for pattern, label in _PLACEHOLDERS if pattern.search(caption)]


def validate(draft: Draft) -> list[str]:
    """直してほしい点を日本語で返す。空なら投稿できる。"""
    problems: list[str] = []

    for line in draft.unparsed:
        problems.append(
            f"投稿設定の中に読めない行がある：「{line}」。項目は1行ずつ、写真は1行にまとめて書く"
        )
    if draft.scheduled is None:
        problems.append(
            f"日時が読めない（{draft.scheduled_raw or '空欄'}）。「2026-10-08 21:00」の形で書く"
        )
    if "リール" in draft.format or "動画" in draft.format:
        problems.append("リールはまだ自動投稿に対応していない。写真かカルーセルだけ")
    if not draft.folder:
        problems.append("素材フォルダが空欄")
    if not draft.photos:
        problems.append("写真が1枚も指定されていない")
    elif len(draft.photos) > MAX_PHOTOS:
        problems.append(f"写真が{len(draft.photos)}枚ある。上限は{MAX_PHOTOS}枚")
    if len({p.lower() for p in draft.photos}) != len(draft.photos):
        problems.append("同じ写真が2回指定されている")

    if not draft.caption.strip():
        problems.append("キャプションが空")
    if len(draft.caption) > MAX_CAPTION:
        problems.append(f"キャプションが{len(draft.caption)}文字ある。上限は{MAX_CAPTION}文字")
    for label in find_placeholders(draft.caption):
        problems.append(f"キャプションに「{label}」が残っている。確認して消す")
    tags = count_hashtags(draft.caption)
    if tags > MAX_HASHTAGS:
        problems.append(f"キャプションに # が{tags}個ある。ハッシュタグの上限は{MAX_HASHTAGS}個")

    return problems
