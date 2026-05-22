"""Game-asset post-processing utilities.

Background removal via *rembg* and standards-compliant resizing via *Pillow*.
All functions operate on **bytes** so they can be plugged directly into any
pipeline (HTTP responses, file I/O, message queues, etc.).
"""

from __future__ import annotations

import io
import logging
from typing import Tuple

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def remove_background(
    image_bytes: bytes,
    *,
    alpha_matting: bool = True,
    alpha_matting_erode_size: int = 4,
) -> bytes:
    """Strip the background from *image_bytes* and return a **PNG with
    alpha-channel transparency**.

    Uses ``rembg`` under the hood which runs a U²-Net deep-learning model
    on first invocation (model is cached afterward).

    Parameters:
        image_bytes:
            Raw image data in any format supported by Pillow (JPEG, PNG,
            WebP, BMP, …).
        alpha_matting:
            When ``True``, applies alpha-matting post-processing for cleaner
            edges (slightly slower).
        alpha_matting_erode_size:
            Erosion kernel size for alpha matting.  Lower values preserve
            more edge detail; higher values reduce fringing.

    Returns:
        PNG bytes with removed background.

    Raises:
        RuntimeError:
            If *rembg* is not installed.  Install with
            ``pip install rembg[gpu]`` (or ``rembg`` for CPU-only).
    """
    try:
        from rembg import remove  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "rembg is required for background removal. "
            "Install it with: pip install rembg"
        ) from exc

    logger.info(
        "Removing background (alpha_matting=%s, erode=%d, input_size=%d bytes)",
        alpha_matting, alpha_matting_erode_size, len(image_bytes),
    )

    try:
        result = remove(
            image_bytes,
            alpha_matting=alpha_matting,
            alpha_matting_erode_size=alpha_matting_erode_size,
        )
    except Exception as exc:
        logger.exception("rembg failed")
        raise RuntimeError(f"Background removal failed: {exc}") from exc

    logger.info("Background removed → output_size=%d bytes", len(result))
    return result


def resize_to_game_standard(
    image_bytes: bytes,
    target_size: Tuple[int, int] = (512, 512),
    *,
    pad_to_fit: bool = True,
    resample: int = Image.LANCZOS,
) -> bytes:
    """Resize *image_bytes* to an exact *target_size*, optionally padding to
    preserve the original aspect ratio.

    Parameters:
        image_bytes:
            Raw image data (any Pillow-supported format, RGBA recommended for
            assets with transparency).
        target_size:
            ``(width, height)`` tuple.  Common game sizes: ``(64, 64)``,
            ``(128, 128)``, ``(256, 256)``, ``(512, 512)``, ``(1024, 1024)``.
        pad_to_fit:
            If ``True``, the image is fit **inside** the target box while
            preserving aspect ratio, and the remaining area is filled with
            transparent pixels.  If ``False``, the image is stretched to
            exactly fill the target size (may distort).
        resample:
            Pillow resampling filter.  Default is ``LANCZOS`` (high quality).

    Returns:
        PNG bytes at the requested size.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        raise ValueError(f"Cannot open image: {exc}") from exc

    original = img.size
    logger.info(
        "Resize: %dx%d → %dx%d (pad=%s, mode=%s)",
        *original, *target_size, pad_to_fit, img.mode,
    )

    # Ensure RGBA so we have an alpha channel for padding
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    tw, th = target_size

    if pad_to_fit:
        # Scale so the whole image fits inside target_size
        img = ImageOps.contain(img, (tw, th), method=resample)

        # Create a transparent canvas and paste the scaled image centred
        canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        ox = (tw - img.width)  // 2
        oy = (th - img.height) // 2
        canvas.paste(img, (ox, oy))
        img = canvas
    else:
        img = img.resize((tw, th), resample=resample)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    result = buf.getvalue()

    logger.info("Resized → %d bytes", len(result))
    return result


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

GAME_STANDARDS = {
    "icon":     (64, 64),
    "card":     (128, 128),
    "sprite":   (256, 256),
    "portrait": (512, 512),
    "bg":       (1024, 1024),
    "full":     (2048, 2048),
}


def process_to_game_asset(
    image_bytes: bytes,
    preset: str = "sprite",
    *,
    remove_bg: bool = True,
) -> bytes:
    """One-shot pipeline: remove background → resize to game standard.

    Parameters:
        image_bytes:
            Raw image data.
        preset:
            One of ``"icon"``, ``"card"``, ``"sprite"``, ``"portrait"``,
            ``"bg"``, ``"full"``.
        remove_bg:
            Whether to run background removal first.
    """
    target = GAME_STANDARDS.get(preset)
    if target is None:
        raise ValueError(
            f"Unknown preset {preset!r}.  Choose from: {list(GAME_STANDARDS)}"
        )

    if remove_bg:
        image_bytes = remove_background(image_bytes)

    return resize_to_game_standard(image_bytes, target)
