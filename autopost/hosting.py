"""変換した写真を、Instagramが取りに来られる公開URLに置く。

このリポジトリの media ブランチに毎回上書きで置き、コミットのIDを含むURLを渡す。
main ブランチには写真が残らないので、リポジトリは大きくならない。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests

BRANCH = "media"


class HostingError(Exception):
    pass


def _git(cwd: Path, *args: str, secret: str = "") -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        if secret:
            message = message.replace(secret, "***")
        raise HostingError(f"git {args[0]} に失敗: {message[:400]}")
    return result.stdout.strip()


def push(files: list[Path], repo: str, token: str, slug: str,
         remote: str | None = None) -> list[str]:
    """files を media ブランチに置いて、公開URLの一覧を返す。

    remote はテスト用。ふだんは GitHub の自分のリポジトリに送る。
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        _git(work, "init", "-q")
        _git(work, "config", "user.name", "ig-autopost")
        _git(work, "config", "user.email", "ig-autopost@users.noreply.github.com")
        target = work / slug
        target.mkdir()
        for file in files:
            shutil.copy2(file, target / file.name)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", f"media {slug}")
        sha = _git(work, "rev-parse", "HEAD")
        remote = remote or f"https://x-access-token:{token}@github.com/{repo}.git"
        _git(work, "push", "-q", "-f", remote, f"HEAD:refs/heads/{BRANCH}", secret=token)
    return [f"https://raw.githubusercontent.com/{repo}/{sha}/{slug}/{file.name}" for file in files]


def wait_public(url: str, timeout: int = 120, interval: int = 4) -> None:
    """誰でも（ログインなしで）JPEGとして取れるようになるまで待つ。"""
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            response = requests.get(url, timeout=20, stream=True)
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and content_type.startswith("image/"):
                response.close()
                return
            last = f"HTTP {response.status_code} / {content_type or '種類不明'}"
            response.close()
        except requests.RequestException as exc:
            last = str(exc)
        if time.monotonic() > deadline:
            raise HostingError(
                f"写真のURLが公開されていない（{last}）。リポジトリが Public になっているか確認する"
            )
        time.sleep(interval)
