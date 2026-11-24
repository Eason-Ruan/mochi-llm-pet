"""Video replay and optional desktop image capture in memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
import math
from pathlib import Path
import re
import subprocess
from threading import Event

import mss
from mss.exception import ScreenShotError
from PIL import Image

from .activity import (
    _DEFAULT_EXCLUDED_APPS, _SENSITIVE_TITLE_WORDS, ActivitySample, _is_browser, _page_domain, _private_title,
)
from .desktop_capture import (
    DesktopCaptureRestricted as DesktopCaptureRestricted,
    FilteredDesktopCapture as FilteredDesktopCapture,
    capture_filtered_desktop as capture_filtered_desktop,
    capture_unfiltered_desktop as capture_unfiltered_desktop,
)
from .documents import is_mochi_app

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


def cloud_window_eligible(
    sample: ActivitySample, *, browser_url_required: bool, platform: str,
) -> bool:
    """Require enough native metadata to establish a non-private foreground window."""
    if (
        platform not in {"darwin", "win32"} or sample.screen_locked is not False
        or not sample.session_id or not sample.app_name or not sample.window_title
        or type(sample.window_id) is not int or sample.window_id <= 0
        or sample.private_window is True or is_mochi_app(sample.app_name, sample.app_id)
        or _private_title(sample.window_title) or _private_title(sample.active_tab_title)
    ):
        return False
    titles = (sample.window_title, sample.active_tab_title)
    if any(word in title.casefold() for title in titles if title for word in _SENSITIVE_TITLE_WORDS):
        return False
    if any(app in sample.app_name.casefold() for app in _DEFAULT_EXCLUDED_APPS):
        return False
    if _is_browser(sample.app_name):
        if sample.private_window is not False:
            return False
        if browser_url_required and _page_domain(sample.url) is None:
            return False
    return True


def _mac_window(window_id: int) -> Image.Image:
    import AppKit
    import Quartz

    if not Quartz.CGPreflightScreenCaptureAccess():
        raise RuntimeError("Window capture is unavailable without screen recording permission")
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        raise RuntimeError("No foreground window")
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) or []
    window = next((window for window in windows if window.get("kCGWindowNumber") == window_id), None)
    if (
        window is None or window.get("kCGWindowOwnerPID") != app.processIdentifier()
        or window.get("kCGWindowLayer") != 0
    ):
        raise RuntimeError("The foreground window could not be verified")
    try:
        from ScreenCaptureKit import SCScreenshotManager
    except ImportError:
        # Older macOS can still render exactly one WindowServer surface.
        snapshot = Quartz.CGWindowListCreateImageFromArray(
            Quartz.CGRectNull, [window_id], Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
    else:
        snapshot = _mac_single_window_image(SCScreenshotManager, window_id, app.processIdentifier())
    if snapshot is None:
        raise RuntimeError("Single-window capture failed")
    representation = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(snapshot)
    data = representation.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    if data is None:
        raise RuntimeError("Single-window capture could not be encoded")
    with Image.open(BytesIO(bytes(data))) as captured:
        rgba = captured.convert("RGBA")
    background = Image.new("RGB", rgba.size)
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _mac_single_window_image(manager, window_id: int, owner_pid: int):
    from ScreenCaptureKit import SCContentFilter, SCShareableContent, SCStreamConfiguration

    def result_from(call):
        done = Event()
        result = None

        def complete(value, error):
            nonlocal result
            if error is None:
                result = value
            done.set()

        call(complete)
        if not done.wait(timeout=1.2) or result is None:
            raise RuntimeError("Single-window capture did not complete")
        return result

    content = result_from(
        lambda complete: SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, True, complete,
        ),
    )
    window = next((window for window in content.windows() if window.windowID() == window_id), None)
    if window is None or window.owningApplication() is None or window.owningApplication().processID() != owner_pid:
        raise RuntimeError("The foreground window changed before single-window capture")
    filter_ = SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
    config = SCStreamConfiguration.alloc().init()
    size = window.frame().size
    if min(size.width, size.height) <= 0:
        raise RuntimeError("Invalid foreground window size")
    scale = min(_WIDTH / size.width, _HEIGHT / size.height, 1.0)
    config.setWidth_(max(1, round(size.width * scale)))
    config.setHeight_(max(1, round(size.height * scale)))
    config.setShowsCursor_(False)
    config.setIgnoreShadowsSingleWindow_(True)
    if hasattr(config, "setIncludeChildWindows_"):
        config.setIncludeChildWindows_(False)
    return result_from(
        lambda complete: manager.captureImageWithFilter_configuration_completionHandler_(filter_, config, complete),
    )


def _win_window(window_id: int) -> Image.Image:
    import ctypes
    from ctypes import wintypes

    import win32gui
    import win32ui

    if (
        win32gui.GetForegroundWindow() != window_id or not win32gui.IsWindowVisible(window_id)
        or win32gui.IsIconic(window_id)
    ):
        raise RuntimeError("The foreground window could not be verified")
    left, top, right, bottom = win32gui.GetClientRect(window_id)
    width, height = right - left, bottom - top
    if min(width, height) <= 0 or width * height > 32_000_000:
        raise RuntimeError("Invalid foreground window size")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send = user32.SendMessageTimeoutW
    send.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    )
    send.restype = wintypes.LPARAM
    window_dc = win32gui.GetDC(window_id)
    try:
        source = win32ui.CreateDCFromHandle(window_dc)
        target = source.CreateCompatibleDC()
        try:
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(source, width, height)
            old = target.SelectObject(bitmap)
            try:
                target.PatBlt((0, 0), (width, height), 0x00000042)  # BLACKNESS
                result = ctypes.c_size_t()
                # Ask the selected window to draw only its own client area into
                # the private bitmap. Bound the wait if the app stops responding.
                if not send(
                    window_id, 0x0318, target.GetSafeHdc(), 0x00000004 | 0x00000010,
                    0x0002 | 0x0020, 800, ctypes.byref(result),
                ):  # WM_PRINTCLIENT, PRF_CLIENT | PRF_CHILDREN, ABORTIFHUNG | ERRORONEXIT
                    raise RuntimeError("Single-window capture failed")
                image = Image.frombytes("RGB", (width, height), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, -1)
                if image.getbbox() is None:
                    raise RuntimeError("The foreground window did not render")
                return image
            finally:
                target.SelectObject(old)
                win32gui.DeleteObject(bitmap.GetHandle())
        finally:
            target.DeleteDC()
    finally:
        win32gui.ReleaseDC(window_id, window_dc)


def capture_window(sample: ActivitySample, *, platform: str) -> tuple[datetime, bytes]:
    """Capture one verified foreground window in memory, or fail without a desktop fallback."""
    if not cloud_window_eligible(sample, browser_url_required=False, platform=platform):
        raise RuntimeError("The foreground window cannot be captured safely")
    try:
        image = _mac_window(sample.window_id) if platform == "darwin" else _win_window(sample.window_id)
        captured_at = datetime.now(UTC)
        return captured_at, _jpeg(image)
    except Exception:
        raise RuntimeError("Verified single-window capture is unavailable") from None


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
