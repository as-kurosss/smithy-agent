"""Failure screenshot capture.

Best effort: if ``mss``/``Pillow`` are not installed (the ``screenshot``
extra) the capture is skipped - a failed run must still be reported even
when screenshots are unavailable. Screenshots are skipped on non-Windows
hosts and headless sessions (no interactive desktop to capture).
"""

from __future__ import annotations

import contextlib
import logging
import sys

logger = logging.getLogger(__name__)

MAX_SCREENSHOT_BYTES = 6 * 1024 * 1024  # must match the server-side cap


def capture_screenshot() -> tuple[bytes, str] | None:
    """Return ``(png_bytes, filename)`` or ``None`` when unavailable.

    The filename encodes the capture time for easy scanning in the UI.
    """
    if sys.platform != "win32":
        return None
    try:
        import io

        import mss
        from PIL import Image
    except ImportError:
        logger.info("Screenshot skipped: install smithy-agent[screenshot] to enable")
        return None

    try:
        with mss.mss() as sct:
            monitor = sct.monitors[0]  # full virtual screen (all monitors)
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.rgb)
    except Exception:
        logger.exception("Screenshot capture failed")
        return None
    def _close(obj: object) -> None:
        close = getattr(obj, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    try:
        with io.BytesIO() as buf:
            img.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
        if len(data) > MAX_SCREENSHOT_BYTES:
            # Downscale just enough to fit (keep aspect ratio).
            scale = (MAX_SCREENSHOT_BYTES / len(data)) ** 0.5
            resized_img = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            )
            try:
                with io.BytesIO() as resized:
                    resized_img.save(resized, format="PNG", optimize=True)
                    data = resized.getvalue()
            finally:
                _close(resized_img)
            if len(data) > MAX_SCREENSHOT_BYTES:
                logger.warning("Screenshot exceeds size cap even after downscale - dropping")
                return None
    except Exception:
        logger.exception("Screenshot encode failed")
        return None
    finally:
        _close(img)

    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return data, f"failure-{stamp}.png"
