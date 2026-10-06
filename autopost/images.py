"""写真をInstagramのAPIが受け取れる形にそろえる。

- HEIC / PNG / JPEG を JPEG にする
- 向きを直し、色をsRGBにそろえる（iPhoneの写真はDisplay P3なので、そのままだとくすむ）
- 位置情報などの付帯情報をすべて落とす（公開URLに置くため）
- 縦横比を許容範囲に収め、カルーセルでは全部を1枚目と同じ比率に切る
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

try:  # HEIC は pillow-heif が入っているときだけ読める
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pillow_heif = None

MIN_RATIO = 4 / 5      # 縦長の限界（幅÷高さ）
MAX_RATIO = 1.91       # 横長の限界
MAX_WIDTH = 1440
MAX_BYTES = 8 * 1024 * 1024
HEAVY_CROP = 0.25      # これ以上切られたら知らせる

_SRGB = ImageCms.createProfile("sRGB")


class ImageProblem(Exception):
    """写真そのものに問題があって、何度やっても変換できないとき。"""


@dataclass
class Normalized:
    source: str
    path: Path
    width: int
    height: int
    cropped_fraction: float  # 切り落とした面積の割合

    @property
    def heavily_cropped(self) -> bool:
        return self.cropped_fraction >= HEAVY_CROP


def _to_srgb(image: Image.Image) -> Image.Image:
    profile = image.info.get("icc_profile")
    if not profile:
        return image
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
        return ImageCms.profileToProfile(image, source, _SRGB, outputMode="RGB")
    except (ImageCms.PyCMSError, OSError, ValueError):
        return image  # 色の変換に失敗しても、投稿は止めない


# 色をそのままRGBに直せる形式。16ビットのグレースケールなどは真っ白になってしまうので受け付けない
_PLAIN_MODES = ("RGB", "L", "CMYK", "YCbCr", "LAB", "HSV", "1")
_ALPHA_MODES = ("RGBA", "LA", "P", "PA", "RGBa", "La")


def _open(path: Path) -> Image.Image:
    try:
        image = Image.open(path)
        image = ImageOps.exif_transpose(image)
        if image.mode not in _PLAIN_MODES + _ALPHA_MODES:
            raise ValueError(f"色の形式 {image.mode} には対応していない。JPEGに書き出し直す")
        if image.mode in _ALPHA_MODES:
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            flat.info["icc_profile"] = image.info.get("icc_profile")
            image = flat
        elif image.mode != "RGB":
            profile = image.info.get("icc_profile")
            image = image.convert("RGB")
            image.info["icc_profile"] = profile
        image = _to_srgb(image)
    except Exception as exc:  # 手元のファイルの処理なので、何が起きても「この写真の問題」として扱う
        raise ImageProblem(f"{path.name} を開けない（{type(exc).__name__}: {exc}）") from None

    # 画素だけを写し取る。EXIF・XMP・コメントなど、元の写真に付いていた情報は持ち越さない
    clean = Image.new("RGB", image.size)
    clean.paste(image)
    return clean


def clamp_ratio(ratio: float) -> float:
    return max(MIN_RATIO, min(MAX_RATIO, ratio))


def _crop_center(image: Image.Image, width: int, height: int) -> Image.Image:
    left = (image.width - width) // 2
    top = (image.height - height) // 2
    return image.crop((left, top, left + width, top + height))


def _crop_to_ratio(image: Image.Image, ratio: float) -> Image.Image:
    width, height = image.size
    current = width / height
    if abs(current - ratio) < 0.002:
        return image
    if current > ratio:  # 横が余る
        return _crop_center(image, max(1, round(height * ratio)), height)
    return _crop_center(image, width, max(1, round(width / ratio)))  # 縦が余る


def _inside_limits(image: Image.Image) -> Image.Image:
    """四捨五入のせいで許容範囲を1画素はみ出すことがあるので、最後に必ず内側へ寄せる。"""
    width, height = image.size
    while width / height < MIN_RATIO and height > 1:
        height -= 1
    while width / height > MAX_RATIO and width > 1:
        width -= 1
    return image if (width, height) == image.size else _crop_center(image, width, height)


def _save(image: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for quality in (90, 85, 80, 72):
        image.save(dest, "JPEG", quality=quality, optimize=True)
        if dest.stat().st_size <= MAX_BYTES:
            return
    raise ImageProblem(f"{dest.name} を8MB以下にできなかった")


def normalize_set(sources: list[Path], out_dir: Path) -> list[Normalized]:
    """1枚目の比率を基準に、全部の写真を同じ比率のJPEGにする。"""
    if not sources:
        raise ImageProblem("写真がない")

    first = _open(sources[0])
    target = clamp_ratio(first.width / first.height)

    results: list[Normalized] = []
    for index, source in enumerate(sources, start=1):
        original = first if index == 1 else _open(source)
        image = _crop_to_ratio(original, target)
        if image.width > MAX_WIDTH:
            height = max(1, round(image.height * MAX_WIDTH / image.width))
            image = image.resize((MAX_WIDTH, height), Image.LANCZOS)
        image = _inside_limits(image)
        kept = min(1.0, _kept_fraction(original.size, image.width / image.height))
        dest = out_dir / f"{index:02d}.jpg"
        _save(image, dest)
        results.append(Normalized(source.name, dest, image.width, image.height, 1 - kept))
    return results


def _kept_fraction(original: tuple[int, int], ratio: float) -> float:
    """元の写真のうち、その比率に切ったあとに残る面積の割合。"""
    width, height = original
    if width / height > ratio:
        return (height * ratio) / width
    return (width / ratio) / height
