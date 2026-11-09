"""Prepare request-local image streams without changing the uploaded originals."""
import mimetypes
from pathlib import Path
from tempfile import SpooledTemporaryFile

IMAGE_PREPROCESS_MODES = ("original", "resize", "crop")


def prepare_image_upload(path, mode, width, height, stack):
    """Return an httpx file tuple; the caller's ExitStack owns its stream."""
    if mode not in IMAGE_PREPROCESS_MODES:
        raise ValueError(f"Unsupported image preprocessing mode: {mode}")
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
                processed = ImageOps.fit(rgb, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            with processed:
                processed.save(stream, format="PNG")
    stream.seek(0)
    return f"{path.stem}_{mode}.png", stream, "image/png"
