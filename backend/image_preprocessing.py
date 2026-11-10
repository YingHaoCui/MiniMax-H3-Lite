"""Prepare request-local image streams without changing the uploaded originals."""
import mimetypes
from pathlib import Path
from tempfile import SpooledTemporaryFile

IMAGE_PREPROCESS_MODES = ("original", "resize", "crop")


def validate_crop(crop):
    import math
    if not isinstance(crop, dict):
        raise ValueError("裁剪区域必须为对象")
    values = [crop.get(k) for k in ("x", "y", "width", "height")]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("裁剪区域必须为有限数字")
    x, y, w, h = values
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001:
        raise ValueError("裁剪区域超出图像范围")


def prepare_image_upload(path, mode, width, height, stack, crop=None):
    """Return an httpx file tuple; the caller's ExitStack owns its stream."""
    if mode not in IMAGE_PREPROCESS_MODES:
        raise ValueError(f"Unsupported image preprocessing mode: {mode}")
    if crop is not None:
        validate_crop(crop)
        mode = "crop"
    path = Path(path)
    if mode == "original":
        return path.name, stack.enter_context(path.open("rb")), mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    from PIL import Image, ImageOps

    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise ValueError("Image preprocessing requires positive target width and height")
    stream = stack.enter_context(SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b"))
    with Image.open(path) as source, ImageOps.exif_transpose(source) as oriented:
        with oriented.convert("RGB") as rgb:
            if mode == "resize":
                processed = rgb.resize((width, height), Image.Resampling.LANCZOS)
            else:
                if crop is not None:
                    iw, ih = rgb.size
                    box = (crop["x"] * iw, crop["y"] * ih,
                           (crop["x"] + crop["width"]) * iw, (crop["y"] + crop["height"]) * ih)
                    with rgb.crop(box) as selected:
                        processed = ImageOps.fit(selected, (width, height), method=Image.Resampling.LANCZOS)
                else:
                    processed = ImageOps.fit(rgb, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            with processed:
                processed.save(stream, format="PNG")
    stream.seek(0)
    return f"{path.stem}_{mode}.png", stream, "image/png"
