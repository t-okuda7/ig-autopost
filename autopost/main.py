"""承認された原稿をInstagramに投稿する。

使い方:  python -m autopost.main [run|dry-run|check]

  run      承認済みで、予約の時刻を過ぎた原稿を投稿する（定期実行はこれ）
  dry-run  何も投稿せず、何も書き換えない。原稿の点検結果と、切り抜き後の写真を出す
  check    Drive・Instagram・写真の置き場につながるかだけを確かめる
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import draft as drafts
from . import hosting, images
from .draft import JST, nfc
from .drive import DOC, FOLDER, DriveFile
from .instagram import Instagram, InstagramError, InstagramUnknown

DRAFT_PREFIX = "原稿_"
DONE_PREFIX = "済_"
FIX_PREFIX = "要対応_"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".heic", ".heif")
QUIET_FOR = timedelta(hours=6)       # 同じ原稿の「やり直し中」を失敗として知らせる間隔
ACTIVE_STATES = ("publishing", "unconfirmed", "posted")


@dataclass
class Config:
    material_id: str
    draft_id: str
    posted_id: str
    preview_dir: Path = Path("preview")


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    failures: int = 0

    def add(self, line: str) -> None:
        self.lines.append(line)
        print(line, flush=True)

    def fail(self, line: str) -> None:
        self.failures += 1
        self.add(f"✗ {line}")

    def write_summary(self, title: str) -> None:
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"## {title}\n\n" + "\n".join(f"- {line}" for line in self.lines) + "\n")


class GitHubHost:
    """写真をこのリポジトリの media ブランチに置く。"""

    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def push(self, files: list[Path], slug: str) -> list[str]:
        return hosting.push(files, self.repo, self.token, slug)

    def wait_public(self, url: str) -> None:
        hosting.wait_public(url)


def _describe(exc: Exception) -> str:
    """公開されるログに出してよい形にする。

    自分で組み立てた文（Instagram・写真の置き場）はそのまま出す。それ以外（Driveなど）の
    エラー文にはファイルのIDやURLが入るので、種類の名前だけにする。
    """
    if isinstance(exc, (InstagramError, InstagramUnknown, hosting.HostingError,
                        images.ImageProblem, drafts.DraftError)):
        return str(exc)
    return type(exc).__name__


# ---------------------------------------------------------------- 名前の照合
# あいまいなときは勝手に選ばない。違う写真を公開するくらいなら止める。

def find_folder(name: str, folders: list[DriveFile]) -> tuple[DriveFile | None, str | None]:
    matches = [f for f in folders if nfc(f.name).strip() == nfc(name).strip()]
    if not matches:
        return None, f"素材フォルダ「{name}」が 01_素材 に見つからない"
    if len(matches) > 1:
        return None, f"「{name}」という素材フォルダが{len(matches)}つある。名前を変えて1つにする"
    return matches[0], None


def match_photos(wanted: list[str], files: list[DriveFile]) -> tuple[list[DriveFile], list[str]]:
    """原稿に書かれた写真名を、素材フォルダの中のファイルに対応させる。"""
    by_name: dict[str, list[DriveFile]] = defaultdict(list)
    by_stem: dict[str, list[DriveFile]] = defaultdict(list)
    for file in files:
        name = nfc(file.name).strip().lower()
        if file.mime == FOLDER or Path(name).suffix not in IMAGE_EXTENSIONS:
            continue
        by_name[name].append(file)
        by_stem[Path(name).stem].append(file)

    found: list[DriveFile] = []
    problems: list[str] = []
    for raw in wanted:
        key = nfc(raw).strip().lower()
        # 拡張子まで書いてあれば、その名前のファイルだけを見る
        candidates = by_name.get(key, []) if Path(key).suffix in IMAGE_EXTENSIONS \
            else by_stem.get(key, [])
        if not candidates:
            problems.append(f"写真「{raw}」が素材フォルダに見つからない")
        elif len(candidates) > 1:
            names = "、".join(sorted(c.name for c in candidates))
            problems.append(
                f"写真「{raw}」に当てはまるファイルが{len(candidates)}つある（{names}）。"
                "拡張子まで書くか、使わないほうを消す"
            )
        else:
            found.append(candidates[0])
    if len({f.id for f in found}) != len(found):
        problems.append("同じ写真が2回指定されている")
    return found, problems


def _flat(text: str) -> str:
    return " ".join(nfc(text).split())


def same_caption(a: str, b: str, loose: bool = False) -> bool:
    """同じキャプションか。空白や改行の違いは無視する。

    loose は「この原稿は一度公開を試みた」と分かっているときだけ使う。
    Instagram側で末尾が変わっていても拾えるよう、先頭150文字の一致でも同じとみなす。
    """
    a, b = _flat(a), _flat(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return loose and len(a) >= 60 and len(b) >= 60 and a[:150] == b[:150]


def _own_props(doc: DriveFile) -> dict:
    """このファイル自身に付けた印だけを信じる。コピーに引き継がれた印は無視する。"""
    props = doc.props or {}
    return props if props.get("ig_doc", doc.id) == doc.id else {}


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _preview_name(doc: DriveFile) -> str:
    """点検結果を置くフォルダ名。原稿名から、パスに使えない文字と長すぎる分を落とす。"""
    safe = re.sub(r'[\\/:*?"<>|\s.]+', "_", nfc(doc.name)).strip("_")[:40] or "draft"
    return f"{safe}-{re.sub(r'[^A-Za-z0-9_-]', '', doc.id)[:8]}"


# ---------------------------------------------------------------- 状態の書き込み

def _needs_fix(drive, doc: DriveFile, reason: str, report: Report, mode: str,
               how: str | None = None) -> None:
    report.fail(f"{doc.name}：{reason}")
    if mode != "run":
        return
    how = how or f"直したら、ファイル名の先頭の「{FIX_PREFIX}」を消す"
    drive.update(
        doc.id,
        name=FIX_PREFIX + doc.name,
        description=f"要対応：{reason}\n{how}",
        props={"ig_status": "needs_fix", "ig_doc": doc.id},
    )


def _unconfirmed(drive, doc: DriveFile, report: Report) -> None:
    report.fail(f"{doc.name}：前回の投稿が最後まで終わったか分からない。Instagramを見て確認する")
    drive.update(
        doc.id,
        name=FIX_PREFIX + doc.name,
        description=(
            "要確認：前回の投稿処理が途中で止まった。Instagramを開いて、この投稿が出ているか見る。\n"
            f"・出ていた → ファイル名を「{DONE_PREFIX}…」に変える\n"
            f"・出ていなかった → 原稿の「日時」をこれからの時刻に書き直し、"
            f"ファイル名の先頭の「{FIX_PREFIX}」を消す\n"
            "（同じキャプションの投稿が見つかったときは、投稿せずに「済_」にする）"
        ),
        props={"ig_status": "unconfirmed", "ig_doc": doc.id},
    )


def _retry_later(drive, doc: DriveFile, exc: Exception, report: Report, now: datetime) -> str:
    """原稿のせいではない失敗。原稿はそのままにして、次の実行でやり直す。

    15分ごとに同じ失敗を知らせ続けないよう、失敗として数えるのは6時間に1回にする。
    """
    hint = ""
    if isinstance(exc, InstagramError) and exc.token_problem:
        hint = " トークンが切れているか無効。作り直して IG_ACCESS_TOKEN に入れ直す"
    message = f"{doc.name}：まだ公開していない。次の実行でやり直す（{_describe(exc)}）{hint}"

    last = _own_props(doc).get("ig_retry_noted", "")
    try:
        recently = bool(last) and now - datetime.fromisoformat(last) < QUIET_FOR
    except ValueError:
        recently = False
    if recently:
        report.add(f"・{message}")
        return "やり直し"
    report.fail(message)
    try:
        drive.update(doc.id, props={"ig_retry_noted": now.isoformat(), "ig_doc": doc.id})
    except Exception:
        pass
    return "やり直し"


def _mark_posted(drive, doc: DriveFile, folder: DriveFile, cfg: Config, now: datetime,
                 media_id: str, link: str, report: Report) -> None:
    try:
        drive.update(
            doc.id,
            name=DONE_PREFIX + doc.name,
            description=f"投稿済み：{now.strftime('%Y-%m-%d %H:%M')}\n{link}",
            props={"ig_status": "posted", "ig_media_id": media_id, "ig_doc": doc.id},
        )
    except Exception as exc:
        report.fail(f"{doc.name}：投稿はできたが、Driveに記録できなかった（{_describe(exc)}）")
    try:
        drive.update(folder.id, props={"ig_used": doc.id})
        drive.move(folder.id, cfg.posted_id, cfg.material_id)
    except Exception as exc:
        report.add(f"  ⚠ 素材フォルダを 03_投稿済み に移せなかった。手で移す（{_describe(exc)}）")


# ---------------------------------------------------------------- 点検

def _folder_taken(drive, folder: DriveFile, doc: DriveFile) -> bool:
    """この素材フォルダを、別の原稿が投稿に使った（使っている最中）か。"""
    used_by = (folder.props or {}).get("ig_used", "")
    if not used_by or used_by == doc.id:
        return False
    try:
        other = drive.get(used_by)
    except Exception:
        return False  # 印を付けた原稿がもう無い。印は古いものとして扱う
    return _own_props(other).get("ig_status", "") in ACTIVE_STATES


def _inspect(doc: DriveFile, draft: drafts.Draft, *, drive, material_folders: list[DriveFile],
             shared: set[str], now: datetime, mode: str):
    problems = drafts.validate(draft)
    folder, photos = None, []
    if draft.folder:
        folder, problem = find_folder(draft.folder, material_folders)
        if problem:
            problems.append(problem)
    if folder is not None:
        if _folder_taken(drive, folder, doc):
            problems.append(
                "この素材フォルダは、すでに別の投稿に使われている。"
                "もう一度使うなら、新しい素材フォルダを作って写真を入れ直す"
            )
        photos, photo_problems = match_photos(draft.photos, drive.children(folder.id))
        problems.extend(photo_problems)
    if draft.approved and draft.folder and nfc(draft.folder).strip() in shared:
        problems.append(
            "同じ素材フォルダを指している承認済みの原稿がほかにもある。片方を下書きに戻すか消す"
        )
    if mode == "run" and not problems and draft.is_expired(now):
        problems.append("予約の日時から12時間以上過ぎている。日時を書き直す")
    return _unique(problems), folder, photos


def _build_containers(ig, user_id: str, urls: list[str], caption: str) -> str:
    """Instagram側に写真を取り込ませて、公開の直前まで進める。"""
    if len(urls) == 1:
        creation = ig.create_image(user_id, urls[0], caption=caption)
        ig.wait_ready(creation)
        return creation
    children = [ig.create_image(user_id, url, carousel_item=True) for url in urls]
    for child in children:
        ig.wait_ready(child)
    creation = ig.create_carousel(user_id, children, caption)
    ig.wait_ready(creation)
    return creation


# ---------------------------------------------------------------- 1本ずつ処理

def process(doc: DriveFile, draft: drafts.Draft | None, error: Exception | None, *, mode: str,
            drive, ig, host, cfg: Config, now: datetime, report: Report,
            material_folders: list[DriveFile], shared: set[str]) -> str:
    if error is not None:
        _needs_fix(drive, doc, f"原稿の形が崩れている：{_describe(error)}", report, mode)
        return "要対応"
    if draft is None:
        report.add(f"・{doc.name}：投稿設定のブロックが無いので対象外")
        return "対象外"

    props = _own_props(doc)
    state = props.get("ig_status", "")

    if mode == "run":
        if not draft.approved:
            if draft.unparsed and not draft.status:
                # 「状態」の行が読めていない。承認したつもりで黙って止まり続けるのを防ぐ
                _needs_fix(drive, doc, "投稿設定の「状態」が読み取れない。読めない行：「"
                           + "」「".join(draft.unparsed) + "」", report, mode)
                return "要対応"
            return "下書き"
        if state == "posted":
            drive.update(doc.id, name=DONE_PREFIX + doc.name)
            report.add(f"・{doc.name}：すでに投稿済みなので、ファイル名を「済_」に戻した")
            return "投稿済み"
        if state == "publishing":
            _unconfirmed(drive, doc, report)
            return "要確認"

    problems, folder, photos = _inspect(doc, draft, drive=drive, material_folders=material_folders,
                                        shared=shared, now=now, mode=mode)

    if mode == "run":
        if problems:
            _needs_fix(drive, doc, " ／ ".join(problems), report, mode)
            return "要対応"
        if not draft.is_due(now):
            if state != "scheduled" or props.get("ig_when") != draft.scheduled_raw:
                drive.update(
                    doc.id,
                    description=f"予約済み：{draft.scheduled_raw} を過ぎた最初の実行で投稿される",
                    props={"ig_status": "scheduled", "ig_when": draft.scheduled_raw,
                           "ig_doc": doc.id},
                )
            report.add(f"・{doc.name}：予約済み（{draft.scheduled_raw}）")
            return "予約済み"

    photos_ready = folder is not None and photos and len(photos) == len(draft.photos)
    if not photos_ready:
        if mode == "run":  # 点検を通っていればここには来ない。来たら止める
            _needs_fix(drive, doc, "写真を特定できなかった", report, mode)
            return "要対応"
        return _dry_run_result(doc, draft, problems, None, report)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        try:
            originals = [
                drive.download(photo.id, work / "original" /
                               f"{index:02d}{Path(photo.name).suffix.lower()}")
                for index, photo in enumerate(photos, start=1)
            ]
        except Exception as exc:  # Drive側の一時的な不調。原稿のせいではない
            if mode == "run":
                return _retry_later(drive, doc, exc, report, now)
            return _dry_run_result(
                doc, draft, problems + [f"写真をDriveから取れなかった（{_describe(exc)}）"],
                None, report)
        try:
            normalized = images.normalize_set(originals, work / "jpeg")
        except images.ImageProblem as exc:
            if mode == "run":
                _needs_fix(drive, doc, f"写真を変換できない：{exc}", report, mode)
                return "要対応"
            return _dry_run_result(doc, draft, problems + [f"写真を変換できない：{exc}"], None,
                                   report)
        for item, photo in zip(normalized, photos):
            item.source = photo.name

        if mode == "dry-run":
            target = cfg.preview_dir / _preview_name(doc)
            target.mkdir(parents=True, exist_ok=True)
            for item in normalized:
                shutil.copy2(item.path, target / item.path.name)
            (target / "caption.txt").write_text(draft.caption, encoding="utf-8")
            (target / "原稿名.txt").write_text(doc.name, encoding="utf-8")
            return _dry_run_result(doc, draft, problems, normalized, report)

        return _publish(doc, draft, folder, normalized, drive=drive, ig=ig, host=host, cfg=cfg,
                        now=now, report=report)


def _dry_run_result(doc: DriveFile, draft: drafts.Draft, problems: list[str],
                    normalized: list[images.Normalized] | None, report: Report) -> str:
    facts = f"状態={draft.status or '空欄'}、日時={draft.scheduled_raw or '空欄'}"
    if normalized:
        facts += (f"、写真{len(normalized)}枚（{normalized[0].width}×{normalized[0].height}）、"
                  f"キャプション{len(draft.caption)}文字")
    if not problems:
        report.add(f"✓ {doc.name}：点検OK。{facts}")
    elif draft.approved:
        report.fail(f"{doc.name}：承認済みだが、このままでは投稿されない。{facts}")
    else:
        report.add(f"・{doc.name}：下書き。{facts}")
    for problem in problems:
        report.add(f"    直すところ：{problem}")
    for item in normalized or []:
        if item.heavily_cropped:
            report.add(f"    ⚠ {item.source} は1枚目の比率に合わせて"
                       f"{item.cropped_fraction:.0%}切られる")
    if not problems:
        return "点検OK"
    return "直すところあり" if draft.approved else "下書き"


def _publish(doc: DriveFile, draft: drafts.Draft, folder: DriveFile,
             normalized: list[images.Normalized], *, drive, ig, host, cfg: Config, now: datetime,
             report: Report) -> str:
    tried_before = bool(_own_props(doc).get("ig_tried"))

    # 1. 同じキャプションがすでに出ていないか。一覧が読めなければ先へ進まない
    try:
        user_id = ig.user_id()
        recent = ig.recent_media(user_id, pages=4 if tried_before else 1)
    except (InstagramError, InstagramUnknown) as exc:
        return _retry_later(drive, doc, exc, report, now)
    twin = next((m for m in recent
                 if same_caption(m.get("caption") or "", draft.caption, loose=tried_before)), None)
    if twin is not None:
        link = twin.get("permalink", "")
        if tried_before:
            # この原稿は前に公開を試みている。そのときの投稿が出ていたと判断する
            _mark_posted(drive, doc, folder, cfg, now, str(twin.get("id", "")), link, report)
            report.fail(f"{doc.name}：前回の投稿がInstagramに出ていた。"
                        f"もう一度は投稿せず「済_」にした {link}")
            return "投稿済み（確認）"
        _needs_fix(
            drive, doc, f"まったく同じキャプションの投稿がすでにある {link}", report, "run",
            how=(f"すでに出ているなら、ファイル名を「{DONE_PREFIX}…」に変える。"
                 f"別の投稿として出すなら、キャプションを変えてから先頭の「{FIX_PREFIX}」を消す"),
        )
        return "要対応"

    # 2. 写真を公開URLに置いて、Instagramに取り込ませる。ここまではまだ何も公開されない
    try:
        slug = f"{now.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^A-Za-z0-9_-]', '', doc.id)[:8]}"
        urls = host.push([item.path for item in normalized], slug)
        for url in urls:
            host.wait_public(url)
        creation = _build_containers(ig, user_id, urls, draft.caption)
    except InstagramError as exc:
        if exc.retryable:
            return _retry_later(drive, doc, exc, report, now)
        _needs_fix(drive, doc, f"Instagramに受け付けられなかった：{exc}", report, "run")
        return "要対応"
    except (InstagramUnknown, hosting.HostingError) as exc:
        return _retry_later(drive, doc, exc, report, now)

    # 3. 準備のあいだに原稿が書き換えられていないか、公開の直前にもう一度読む
    try:
        fresh = drafts.parse(drive.export_text(doc.id))
    except Exception as exc:
        return _retry_later(drive, doc, exc, report, now)
    if fresh != draft:
        report.add(f"・{doc.name}：準備のあいだに原稿が書き換えられた。公開せず、次の実行で読み直す")
        return "読み直し"

    # 4. 公開の直前に印を付ける。これが書けないなら公開しない
    try:
        drive.update(folder.id, props={"ig_used": doc.id})
        drive.update(doc.id, props={"ig_status": "publishing", "ig_when": draft.scheduled_raw,
                                    "ig_tried": now.isoformat(), "ig_doc": doc.id})
    except Exception as exc:
        return _retry_later(drive, doc, exc, report, now)

    # 5. 公開
    try:
        result = ig.publish(user_id, creation)
    except InstagramError as exc:
        if exc.server_side:
            # Instagram側の不調。公開されなかったとは言い切れないので、人に確認を求める流れに乗せる
            report.fail(f"{doc.name}：公開できたかどうか分からない（{exc}）。"
                        "次の実行で「要対応」になるので、Instagramを見て確認する")
            return "不明"
        if exc.retryable:
            drive.update(doc.id, props={"ig_status": "scheduled", "ig_doc": doc.id})
            return _retry_later(drive, doc, exc, report, now)
        _needs_fix(drive, doc, f"Instagramが公開を拒否した：{exc}", report, "run")
        return "要対応"
    except InstagramUnknown as exc:
        report.fail(f"{doc.name}：公開できたかどうか分からない（{exc}）。"
                    "次の実行で「要対応」になるので、Instagramを見て確認する")
        return "不明"

    # 6. 記録
    media_id = str(result.get("id", ""))
    link = ig.permalink(media_id) if media_id else ""
    _mark_posted(drive, doc, folder, cfg, now, media_id, link, report)
    issue = result.get("config_issue")
    if issue:
        report.fail(f"{doc.name}：投稿はされたが、Instagramから注意が返ってきた（{issue}）。"
                    f"キャプションが付いているか確認する {link}")
    else:
        report.add(f"✓ {doc.name}：投稿した {link}")
    return "投稿した"


# ---------------------------------------------------------------- 全体

def run(mode: str, *, drive, ig, host, cfg: Config, now: datetime, report: Report) -> int:
    docs = sorted((f for f in drive.children(cfg.draft_id)
                   if f.mime == DOC and f.name.startswith(DRAFT_PREFIX)), key=lambda f: f.name)
    material_folders = [f for f in drive.children(cfg.material_id) if f.mime == FOLDER]
    if mode == "dry-run" and cfg.preview_dir.exists():
        shutil.rmtree(cfg.preview_dir)

    counts: Counter[str] = Counter()
    entries = []
    for doc in docs:
        try:
            entries.append((doc, drafts.parse(drive.export_text(doc.id)), None))
        except drafts.DraftError as exc:
            entries.append((doc, None, exc))
        except Exception as exc:
            report.fail(f"{doc.name}：Driveから読めなかった（{_describe(exc)}）")
            counts["失敗"] += 1

    # 承認済みの原稿が2本、同じ素材フォルダを指していたら、どちらも止める（コピーの出しっぱなし対策）
    folder_use = Counter(nfc(d.folder).strip() for _, d, _ in entries
                         if d is not None and d.approved and d.folder)
    shared = {name for name, count in folder_use.items() if count > 1}

    for doc, draft, error in entries:
        try:
            outcome = process(doc, draft, error, mode=mode, drive=drive, ig=ig, host=host, cfg=cfg,
                              now=now, report=report, material_folders=material_folders,
                              shared=shared)
        except Exception as exc:  # 1本の失敗でほかを止めない
            report.fail(f"{doc.name}：想定外の失敗（{_describe(exc)}）")
            outcome = "失敗"
        counts[outcome] += 1

    summary = "、".join(f"{name} {count}" for name, count in counts.items()) or "原稿なし"
    report.add(f"{now.strftime('%Y-%m-%d %H:%M')} JST ／ {summary}")
    return 1 if report.failures else 0


def check(*, drive, ig, host, cfg: Config, report: Report) -> int:
    for label, folder_id in (("01_素材", cfg.material_id), ("02_原稿", cfg.draft_id),
                             ("03_投稿済み", cfg.posted_id)):
        try:
            folder = drive.get(folder_id)
            count = len(drive.children(folder_id))
            report.add(f"✓ Drive：{label} →「{folder.name}」が読める（中身 {count} 件）")
        except Exception as exc:
            report.fail(f"Drive：{label} が読めない。一番上のフォルダをサービスアカウントに"
                        f"「編集者」で共有したか、IDが合っているか確認する（{_describe(exc)}）")
    try:
        me = ig.me()
        recent = ig.recent_media(str(me.get("user_id") or me.get("id")))
        report.add(f"✓ Instagram：@{me.get('username', '?')} として接続でき、"
                   f"最近の投稿 {len(recent)} 件が読めた")
    except Exception as exc:
        report.fail(f"Instagram：接続できない。トークンを確認する（{_describe(exc)}）")
    try:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "check.jpg"
            Image.new("RGB", (320, 400), (200, 80, 60)).save(sample, "JPEG")
            url = host.push([sample], "check")[0]
            host.wait_public(url)
        report.add("✓ 写真の置き場：media ブランチに置いて、外から取れた")
    except Exception as exc:
        report.fail(f"写真の置き場：使えない（{_describe(exc)}）")
    return 1 if report.failures else 0


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"設定が足りない：{name}")
    return value


def main(argv: list[str]) -> int:
    mode = (argv[1] if len(argv) > 1 else "run").strip() or "run"
    if mode not in ("run", "dry-run", "check"):
        raise SystemExit(__doc__)

    from .drive import Drive

    cfg = Config(material_id=_env("DRIVE_MATERIAL_ID"), draft_id=_env("DRIVE_DRAFT_ID"),
                 posted_id=_env("DRIVE_POSTED_ID"),
                 preview_dir=Path(os.environ.get("PREVIEW_DIR", "preview")))
    drive = Drive(_env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    report = Report()

    if mode == "dry-run":
        code = run(mode, drive=drive, ig=None, host=None, cfg=cfg, now=datetime.now(JST),
                   report=report)
        report.write_summary("点検（dry-run）")
        return code

    ig = Instagram(_env("IG_ACCESS_TOKEN"))
    host = GitHubHost(_env("GITHUB_REPOSITORY"), _env("GITHUB_TOKEN"))
    if mode == "check":
        code = check(drive=drive, ig=ig, host=host, cfg=cfg, report=report)
        report.write_summary("接続の確認（check）")
        return code

    code = run(mode, drive=drive, ig=ig, host=host, cfg=cfg, now=datetime.now(JST), report=report)
    report.write_summary("投稿（run）")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
