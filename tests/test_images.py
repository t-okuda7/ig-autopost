import pytest
from PIL import Image

from autopost import images


def _make(path, size, fmt="JPEG", mode="RGB", **kwargs):
    Image.new(mode, size, (120, 90, 60) if mode == "RGB" else (120, 90, 60, 128)).save(
        path, fmt, **kwargs)
    return path


def test_portrait_3x4_is_cropped_to_4x5(tmp_path):
    src = _make(tmp_path / "a.jpg", (3024, 4032))
    [out] = images.normalize_set([src], tmp_path / "out")
    assert out.width == 1440
    assert out.width / out.height == pytest.approx(0.8, abs=0.002)
    assert out.cropped_fraction == pytest.approx(0.0625, abs=0.002)
    assert not out.heavily_cropped
    assert Image.open(out.path).format == "JPEG"


def test_all_images_follow_the_first_ratio(tmp_path):
    first = _make(tmp_path / "a.jpg", (3024, 4032))
    wide = _make(tmp_path / "b.jpg", (4032, 3024))
    a, b = images.normalize_set([first, wide], tmp_path / "out")
    assert a.width / a.height == pytest.approx(b.width / b.height, abs=0.003)
    assert b.heavily_cropped  # 横長を縦長に切るので大きく減る
    assert [a.path.name, b.path.name] == ["01.jpg", "02.jpg"]


def test_very_wide_first_image_is_limited(tmp_path):
    src = _make(tmp_path / "pano.jpg", (4000, 1000))
    [out] = images.normalize_set([src], tmp_path / "out")
    assert out.width / out.height == pytest.approx(1.91, abs=0.01)


def test_small_image_is_not_enlarged(tmp_path):
    src = _make(tmp_path / "s.jpg", (800, 1000))
    [out] = images.normalize_set([src], tmp_path / "out")
    assert (out.width, out.height) == (800, 1000)


def test_location_data_is_removed(tmp_path):
    src = tmp_path / "gps.jpg"
    image = Image.new("RGB", (1200, 1500), (10, 20, 30))
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif.get_ifd(0x8825)[2] = (35.0, 40.0, 30.0)  # GPSの緯度
    image.save(src, "JPEG", exif=exif)
    assert Image.open(src).getexif().get_ifd(0x8825)

    [out] = images.normalize_set([src], tmp_path / "out")
    result = Image.open(out.path).getexif()
    assert not result.get_ifd(0x8825)
    assert 0x010F not in result


def test_sideways_photo_is_turned_upright(tmp_path):
    src = tmp_path / "rot.jpg"
    image = Image.new("RGB", (4032, 3024), (10, 20, 30))  # 横に寝た状態で保存されている
    exif = Image.Exif()
    exif[0x0112] = 6  # 表示するときは90度回す、という印
    image.save(src, "JPEG", exif=exif)
    [out] = images.normalize_set([src], tmp_path / "out")
    assert out.height > out.width


def test_heic_is_converted(tmp_path):
    pytest.importorskip("pillow_heif")
    src = _make(tmp_path / "IMG_0001.HEIC", (1200, 1600), fmt="HEIF")
    [out] = images.normalize_set([src], tmp_path / "out")
    assert Image.open(out.path).format == "JPEG"
    assert out.width / out.height == pytest.approx(0.8, abs=0.002)


def test_transparent_png_gets_white_background(tmp_path):
    src = _make(tmp_path / "t.png", (1000, 1250), fmt="PNG", mode="RGBA")
    [out] = images.normalize_set([src], tmp_path / "out")
    assert Image.open(out.path).mode == "RGB"


def test_output_stays_under_8mb(tmp_path):
    import os

    src = tmp_path / "noise.png"
    Image.frombytes("RGB", (3000, 3750), os.urandom(3000 * 3750 * 3)).save(src, "PNG")
    [out] = images.normalize_set([src], tmp_path / "out")
    assert out.path.stat().st_size <= images.MAX_BYTES


def test_ratio_never_falls_outside_the_limit_after_rounding(tmp_path):
    # 1179×2556（iPhoneの画面比）を4:5に切ると、四捨五入で 0.7999 になり得る
    for size in [(1179, 2556), (1290, 2796), (3024, 4032), (999, 1333), (1081, 1921)]:
        src = _make(tmp_path / f"{size[0]}.jpg", size)
        [out] = images.normalize_set([src], tmp_path / f"out{size[0]}")
        assert images.MIN_RATIO <= out.width / out.height <= images.MAX_RATIO, size
    src = _make(tmp_path / "wide.jpg", (3821, 2000))
    [out] = images.normalize_set([src], tmp_path / "outwide")
    assert out.width / out.height <= images.MAX_RATIO


def test_every_kind_of_metadata_is_dropped(tmp_path):
    src = tmp_path / "meta.jpg"
    image = Image.new("RGB", (1200, 1500), (10, 20, 30))
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><GPSLatitude>35,40.5N</GPSLatitude></x:xmpmeta>'
    image.save(src, "JPEG", comment=b"shot at home, 35.67N 139.65E", xmp=xmp)
    [out] = images.normalize_set([src], tmp_path / "out")
    data = out.path.read_bytes()
    assert b"GPSLatitude" not in data and b"shot at home" not in data and b"xmpmeta" not in data
    assert not Image.open(out.path).info.get("comment")


def test_colour_profile_is_applied_and_not_carried_over(tmp_path):
    from PIL import ImageCms

    # sRGBのプロファイル付きなら色は変わらず、プロファイル自体は出力に残らない。
    # iPhoneの実写真（Display P3）で色が変換されることは、手元で別に確かめてある
    src = tmp_path / "tagged.jpg"
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (1200, 1500), (200, 40, 40)).save(src, "JPEG", icc_profile=profile,
                                                       quality=95)
    [out] = images.normalize_set([src], tmp_path / "out")
    result = Image.open(out.path)
    assert "icc_profile" not in result.info or not result.info["icc_profile"]
    r, g, b = result.getpixel((10, 10))
    assert abs(r - 200) <= 3 and abs(g - 40) <= 3 and abs(b - 40) <= 3  # sRGB同士なら色は変わらない


def test_broken_file_is_an_image_problem(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image at all")
    with pytest.raises(images.ImageProblem):
        images.normalize_set([bad], tmp_path / "out")


def test_16bit_greyscale_is_refused_rather_than_posted_as_a_white_square(tmp_path):
    src = tmp_path / "deep.png"
    Image.new("I;16", (1200, 1500), 30000).save(src, "PNG")
    with pytest.raises(images.ImageProblem):
        images.normalize_set([src], tmp_path / "out")


def test_cmyk_and_greyscale_are_converted(tmp_path):
    cmyk = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (1200, 1500), (0, 200, 200, 0)).save(cmyk, "JPEG")
    grey = _make(tmp_path / "grey.png", (1200, 1500), fmt="PNG", mode="RGB").with_suffix(".png")
    Image.new("L", (1200, 1500), 128).save(grey, "PNG")
    a, b = images.normalize_set([cmyk, grey], tmp_path / "out")
    assert Image.open(a.path).mode == "RGB" and Image.open(b.path).mode == "RGB"
    r, g, bl = Image.open(a.path).getpixel((5, 5))
    assert r > 200 and g < 120 and bl < 120  # 赤のまま（白飛びや反転をしていない）


def test_absurdly_large_image_is_an_image_problem(tmp_path, monkeypatch):
    src = _make(tmp_path / "big.jpg", (3000, 3750))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(images.ImageProblem):
        images.normalize_set([src], tmp_path / "out")
