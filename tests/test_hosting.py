import subprocess

import pytest
from PIL import Image

from autopost import hosting


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_push_replaces_media_branch_and_returns_sha_urls(tmp_path):
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")
    photos = []
    for name in ("01.jpg", "02.jpg"):
        path = tmp_path / name
        Image.new("RGB", (40, 50), (1, 2, 3)).save(path, "JPEG")
        photos.append(path)

    first = hosting.push(photos, "owner/repo", "tok", "20261008-210700", remote=str(bare))
    sha = _git(bare, "rev-parse", "refs/heads/media")
    assert first == [
        f"https://raw.githubusercontent.com/owner/repo/{sha}/20261008-210700/01.jpg",
        f"https://raw.githubusercontent.com/owner/repo/{sha}/20261008-210700/02.jpg",
    ]
    assert _git(bare, "ls-tree", "-r", "--name-only", sha).split("\n") == [
        "20261008-210700/01.jpg", "20261008-210700/02.jpg"]

    # 次の投稿では上書きされ、前の写真はブランチに残らない
    hosting.push(photos[:1], "owner/repo", "tok", "20261010-120700", remote=str(bare))
    sha2 = _git(bare, "rev-parse", "refs/heads/media")
    assert sha2 != sha
    assert _git(bare, "ls-tree", "-r", "--name-only", sha2) == "20261010-120700/01.jpg"
    assert _git(bare, "rev-list", "--count", sha2) == "1"


def test_push_failure_hides_the_token(tmp_path):
    photo = tmp_path / "01.jpg"
    Image.new("RGB", (40, 50)).save(photo, "JPEG")
    with pytest.raises(hosting.HostingError) as error:
        hosting.push([photo], "o/r", "SECRET-TOKEN", "x",
                     remote=str(tmp_path / "SECRET-TOKEN-missing.git"))
    assert "SECRET-TOKEN" not in str(error.value)
