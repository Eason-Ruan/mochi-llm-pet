"""Video replay and optional desktop image capture in memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
import math
from pathlib import Path
import re
import subprocess

import mss
from mss.exception import ScreenShotError
from PIL import Image

_WIDTH, _HEIGHT = 960, 540
_FRAME_BYTES = _WIDTH * _HEIGHT * 3
_TIMESTAMPS = re.compile(r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:(\S+)")


@dataclass(frozen=True)
class Frame:
    seconds: float
    jpeg: bytes


def _jpeg(image: Image.Image) -> bytes:
    image.thumbnail((_WIDTH, _HEIGHT))
    with BytesIO() as buffer:
        image.convert("RGB").save(buffer, format="JPEG", quality=88)
        return buffer.getvalue()


def capture_desktop() -> tuple[datetime, bytes]:
    """Capture the primary desktop when explicitly enabled by the live caller."""
    try:
        with mss.mss() as capture:
            raw = capture.grab(capture.monitors[1])
            captured_at = datetime.now(UTC)
        return captured_at, _jpeg(Image.frombytes("RGB", raw.size, raw.rgb))
    except (ScreenShotError, IndexError) as exc:
        raise RuntimeError("Desktop capture failed") from exc


def recording_frames(recording: Path, *, interval_s: float, max_frames: int) -> tuple[Frame, ...]:
    if not recording.is_file():
        raise ValueError("Recording must be a local file")
    if not math.isfinite(interval_s) or interval_s <= 0 or not 1 <= max_frames <= 64:
        raise ValueError("Replay needs a positive frame interval and 1–64 frames")
    filters = (
        f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval_s:.8f})',"
        f"showinfo,scale={_WIDTH}:{_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={_WIDTH}:{_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
    )
    try:
        decoded = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
                "-protocol_whitelist", "file,pipe", "-i", str(recording.resolve()),
                "-an", "-sn", "-dn", "-map", "0:v:0", "-vf", filters,
                "-fps_mode", "passthrough", "-frames:v", str(max_frames),
                "-pix_fmt", "rgb24", "-c:v", "rawvideo", "-threads", "1", "-f", "rawvideo", "pipe:1",
            ],
            check=True, capture_output=True, timeout=180,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is required for recording replay") from None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError("ffmpeg could not decode the recording") from None

    count, remainder = divmod(len(decoded.stdout), _FRAME_BYTES)
    times = _TIMESTAMPS.findall(decoded.stderr.decode(errors="replace"))
    if not count or remainder or len(times) < count:
        raise RuntimeError("Recording has no timestamped video frames")
    pts = [float(value) for value in times[:count]]
    if any(not math.isfinite(value) for value in pts) or any(
        previous >= current for previous, current in zip(pts, pts[1:])
    ):
        raise RuntimeError("Recording timestamps are not strictly increasing")
    return tuple(
        Frame(
            seconds - pts[0],
            _jpeg(Image.frombytes("RGB", (_WIDTH, _HEIGHT), decoded.stdout[
                index * _FRAME_BYTES:(index + 1) * _FRAME_BYTES
            ])),
        )
        for index, seconds in enumerate(pts)
    )
