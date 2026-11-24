"""Build one in-memory frame from audited windows on active macOS displays."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
import math
from threading import Event
import time
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .activity import (
    _SENSITIVE_TITLE_WORDS, ActivitySample, _app_excluded, _domain_excluded, _excluded_app_rules, _excluded_domain_rules,
    _is_browser, _mac_session, _mochi_process, _page_domain, _private_title,
)
from .documents import is_mochi_app

_NATIVE_TIMEOUT_S = 1.2
_DISPLAY_LIMIT = 64
_BACKGROUND = (23, 25, 28)
_BROWSER_BUNDLES = (
    "com.apple.safari", "com.google.chrome", "org.chromium.chromium", "com.brave.browser", "com.microsoft.edgemac",
    "org.mozilla.firefox", "com.operasoftware.opera", "com.vivaldi.vivaldi", "company.thebrowser.browser",
    "app.zen-browser.zen",
)
_SYSTEM_PRIVATE_APPS = ("com.apple.passwords", "com.apple.securityagent", "com.apple.loginwindow")
_PixelBox = tuple[int, int, int, int]


class DesktopCaptureRestricted(RuntimeError):
    def __init__(self, message: str, *, reason: str = "desktop_privacy_restricted") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class FilteredDesktopCapture:
    captured_at: datetime
    jpeg: bytes = field(repr=False)
    foreground_excluded: bool


@dataclass(frozen=True)
class _Bounds:
    x: float
    y: float
    width: float
    height: float

    def intersection(self, other: _Bounds) -> _Bounds | None:
        x, y = max(self.x, other.x), max(self.y, other.y)
        width = min(self.x + self.width, other.x + other.width) - x
        height = min(self.y + self.height, other.y + other.height) - y
        if width <= 0 or height <= 0:
            return None
        return _Bounds(x, y, width, height)

    def overlap(self, other: _Bounds) -> float:
        overlap = self.intersection(other)
        return overlap.width * overlap.height if overlap is not None else 0


@dataclass(frozen=True)
class _Display:
    display_id: int
    bounds: _Bounds
    pixels: tuple[int, int]
    native: Any = field(compare=False, repr=False)


@dataclass(frozen=True)
class _Window:
    window_id: int
    pid: int
    names: tuple[str, ...]
    bounds: _Bounds
    allowed: bool
    native: Any = field(compare=False, repr=False)


class _NativeResult:
    def __init__(self) -> None:
        self.done = Event()
        self.value = None

    def complete(self, value, error) -> None:
        if error is None:
            self.value = value
        self.done.set()

    def result(self, deadline: float):
        if not self.done.wait(timeout=max(0, deadline - time.monotonic())) or self.value is None:
            raise DesktopCaptureRestricted("macOS filtered capture did not complete", reason="filtered_desktop_unavailable")
        return self.value


def _display_inventory(content, Quartz) -> tuple[_Display, ...]:
    status, display_ids, count = Quartz.CGGetActiveDisplayList(_DISPLAY_LIMIT, None, None)
    if status != 0 or not 0 < count < _DISPLAY_LIMIT or len(set(display_ids)) != count:
        raise DesktopCaptureRestricted("The active displays could not be verified")
    shareable = {display.displayID(): display for display in content.displays()}
    if not set(display_ids) <= shareable.keys():
        raise DesktopCaptureRestricted("ScreenCaptureKit omitted an active display")
    displays = []
    for display_id in display_ids:
        bounds = Quartz.CGDisplayBounds(display_id)
        rectangle = _Bounds(bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height)
        pixels = (Quartz.CGDisplayPixelsWide(display_id), Quartz.CGDisplayPixelsHigh(display_id))
        if min(*pixels, rectangle.width, rectangle.height) <= 0:
            raise DesktopCaptureRestricted("An active display has invalid dimensions")
        displays.append(_Display(display_id, rectangle, pixels, shareable[display_id]))
    return tuple(displays)


def _sensitive_title(title: str | None) -> bool:
    return bool(title and (_private_title(title) or any(word in title.casefold() for word in _SENSITIVE_TITLE_WORDS)))


def _browser_identity(name: str, bundle: str) -> bool:
    return _is_browser(name) or any(bundle == root or bundle.startswith(root + ".") for root in _BROWSER_BUNDLES)


def _window_bounds(row) -> _Bounds:
    raw = row.get("kCGWindowBounds", {})
    return _Bounds(*(raw.get(key, 0) for key in ("X", "Y", "Width", "Height")))


def _occlusion_masks(rows, windows: dict[int, _Window]) -> frozenset[_Bounds]:
    # Quartz lists front to back. Protect the parts of included windows that
    # would be uncovered when the compositor removes a window above them.
    lower_windows = []
    masks = set()
    for row in reversed(rows):
        window = windows.get(row.get("kCGWindowNumber"))
        if window is not None and window.allowed:
            lower_windows.append(window.bounds)
        elif row.get("kCGWindowAlpha", 1) > 0:
            bounds = _window_bounds(row)
            for lower in lower_windows:
                overlap = bounds.intersection(lower)
                if overlap is not None:
                    masks.add(overlap)
    return frozenset(masks)


def _window_inventory(content, Quartz, workspace, sample: ActivitySample, app_rules, domain_rules,
                      foreground_excluded: bool) -> tuple[
    dict[int, _Window], int | None, _Window | None, frozenset[_Bounds],
]:
    rows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    if rows is None:
        raise DesktopCaptureRestricted("The on-screen window inventory is unavailable")
    by_id = {row.get("kCGWindowNumber"): row for row in rows}
    foreground = workspace.frontmostApplication()
    front_pid = foreground.processIdentifier() if foreground is not None else None
    front_id = next((window_id for window_id, row in by_id.items()
                     if row.get("kCGWindowOwnerPID") == front_pid and row.get("kCGWindowLayer") == 0
                     and min(row.get("kCGWindowBounds", {}).get(key, 0) for key in ("Width", "Height")) >= 20), None)
    foreground_window = None
    domain = _page_domain(sample.url)
    restrict_sample = foreground_excluded or sample.private_window is True
    windows = {}
    for native_window in content.windows():
        window_id = native_window.windowID()
        owner = native_window.owningApplication()
        row = by_id.get(window_id)
        if owner is None or row is None or native_window.windowLayer() != 0 or row.get("kCGWindowLayer") != 0:
            continue
        pid = owner.processID()
        if pid <= 0 or row.get("kCGWindowOwnerPID") != pid or row.get("kCGWindowSharingState", 0) == 0:
            continue
        bounds = _window_bounds(row)
        if min(bounds.width, bounds.height) <= 0:
            continue
        name = str(owner.applicationName() or "").strip().casefold()
        bundle = str(owner.bundleIdentifier() or "").strip().casefold()
        quartz_name = str(row.get("kCGWindowOwnerName") or "").strip().casefold()
        names = (name, bundle, quartz_name)
        if not any(names):
            continue
        is_sample_window = window_id == sample.window_id
        is_front = pid == front_pid and window_id == front_id
        same_sample_app = sample.app_name and sample.app_name.casefold() in names
        if sample.app_id and bundle and sample.app_id.casefold() == bundle:
            same_sample_app = True
        app_blocked = (
            _mochi_process(pid) or is_mochi_app(name, bundle) or is_mochi_app(quartz_name)
            or bundle in _SYSTEM_PRIVATE_APPS or _app_excluded(name, bundle, app_rules)
            or _app_excluded(quartz_name, None, app_rules)
        )
        private = (
            _sensitive_title(native_window.title()) or _sensitive_title(row.get("kCGWindowName"))
            or is_sample_window and (restrict_sample or _sensitive_title(sample.window_title)
                                     or _sensitive_title(sample.active_tab_title))
            # Native sampling does not always identify a private window by ID.
            or restrict_sample and not sample.window_id and pid == front_pid
        )
        browser = _browser_identity(name, bundle) or _browser_identity(quartz_name, bundle)
        domain_blocked = browser and domain_rules and not (
            is_front and is_sample_window and same_sample_app and sample.private_window is False
            and domain and not _domain_excluded(domain, domain_rules)
        )
        windows[window_id] = _Window(
            window_id, pid, names, bounds, not (app_blocked or private or domain_blocked), native_window,
        )
        if is_front and (is_sample_window or sample.window_id is None):
            foreground_window = windows[window_id]
    return windows, front_pid, foreground_window, _occlusion_masks(rows, windows)


def _inventory(shareable, Quartz, workspace, sample, app_rules, domain_rules, foreground_excluded):
    callback = _NativeResult()
    shareable.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        True, True, callback.complete,
    )
    content = callback.result(time.monotonic() + _NATIVE_TIMEOUT_S)
    displays = _display_inventory(content, Quartz)
    windows, front_pid, foreground, masks = _window_inventory(
        content, Quartz, workspace, sample, app_rules, domain_rules, foreground_excluded,
    )
    return displays, windows, front_pid, foreground, masks


def _layout(displays: tuple[_Display, ...], foreground: _Window | None):
    active = displays[0]
    first_label = "Main display"
    if foreground is not None:
        active = max(displays, key=lambda display: display.bounds.overlap(foreground.bounds))
        if active.bounds.overlap(foreground.bounds):
            first_label = "Active display" if foreground.allowed else "Foreground window excluded"
    order = (active, *(display for display in displays if display != active))
    columns = math.ceil(math.sqrt(len(order)))
    rows = math.ceil(len(order) / columns)
    width_limit = min(1280, 2048 // columns - 8)
    height_limit = min(800, 1600 // rows - 32)
    sizes = []
    for display in order:
        width, height = display.bounds.width, display.bounds.height
        pixel_width, pixel_height = display.pixels
        scale = min(pixel_width / width, pixel_height / height, width_limit / width, height_limit / height)
        sizes.append((max(1, round(width * scale)), max(1, round(height * scale))))
    return order, sizes, columns, first_label


def _pixel_box(display: _Display, size: tuple[int, int], region: _Bounds) -> _PixelBox | None:
    bounds = display.bounds
    overlap = bounds.intersection(region)
    if overlap is None:
        return None
    width, height = size
    sx, sy = width / bounds.width, height / bounds.height
    x, y = overlap.x - bounds.x, overlap.y - bounds.y
    return (
        math.floor(x * sx), math.floor(y * sy),
        min(width, math.ceil((x + overlap.width) * sx)), min(height, math.ceil((y + overlap.height) * sy)),
    )


def _visible_after_mask(size: tuple[int, int], allowed: list[_PixelBox], masks: tuple[_PixelBox, ...]) -> bool:
    if not allowed or not masks:
        return bool(allowed)
    occupancy = Image.new("1", size)
    for box in allowed:
        occupancy.paste(1, box)
    for box in masks:
        occupancy.paste(0, box)
    return occupancy.getbbox() is not None


def _encode_montage(AppKit, images, sizes, columns: int, first_label: str, masks: list[tuple[_PixelBox, ...]]) -> bytes:
    label_height = 24 if len(images) > 1 or first_label != "Active display" else 0
    gap = 8
    cell_width = max(width for width, _ in sizes)
    cell_height = max(height for _, height in sizes) + label_height
    rows = math.ceil(len(images) / columns)
    canvas = Image.new("RGB", (columns * (cell_width + gap) - gap, rows * (cell_height + gap) - gap), _BACKGROUND)
    if label_height:
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default(size=15)
    for index, (image, size, redactions) in enumerate(zip(images, sizes, masks, strict=True)):
        x = index % columns * (cell_width + gap)
        y = index // columns * (cell_height + gap)
        if label_height:
            label = first_label if index == 0 else "Other display"
            draw.text((x + 5, y + 3), label, fill=(255, 255, 255), font=font)
        if image is None:
            continue
        representation = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
        data = representation.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
        if data is None:
            raise DesktopCaptureRestricted("macOS filtered frame could not be encoded", reason="filtered_desktop_unavailable")
        with Image.open(BytesIO(bytes(data))) as captured:
            rgba = captured.convert("RGBA")
        if rgba.size != size:
            raise DesktopCaptureRestricted("macOS returned unexpected display dimensions", reason="filtered_desktop_unavailable")
        for box in redactions:
            rgba.paste((*_BACKGROUND, 255), box)
        canvas.paste(rgba, (x, y + label_height), rgba.getchannel("A"))
    with BytesIO() as buffer:
        canvas.save(buffer, format="JPEG", quality=88)
        return buffer.getvalue()


def _capture_macos(sample: ActivitySample, app_rules: tuple[str, ...], domain_rules: tuple[str, ...],
                   foreground_excluded: bool) -> FilteredDesktopCapture:
    import AppKit
    import Quartz

    if not Quartz.CGPreflightScreenCaptureAccess():
        raise DesktopCaptureRestricted("Screen recording permission is required", reason="screen_recording_permission_denied")
    from ScreenCaptureKit import SCContentFilter, SCScreenshotManager, SCShareableContent, SCStreamConfiguration

    session = (False, sample.session_id)
    if _mac_session() != session:
        raise DesktopCaptureRestricted("The desktop session could not be verified")
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    inventory = _inventory(SCShareableContent, Quartz, workspace, sample, app_rules, domain_rules, foreground_excluded)
    displays, windows, _, foreground, masks = inventory
    ordered, sizes, columns, first_label = _layout(displays, foreground)

    requests = []
    pixel_masks = []
    for display, size in zip(ordered, sizes, strict=True):
        allowed = [(window.native, box) for window in windows.values() if window.allowed
                   and (box := _pixel_box(display, size, window.bounds)) is not None]
        redactions = tuple(box for mask in masks if (box := _pixel_box(display, size, mask)) is not None)
        pixel_masks.append(redactions)
        if not _visible_after_mask(size, [box for _, box in allowed], redactions):
            requests.append(None)
            continue
        # The positive filter prevents even a newly opened or launched window
        # from entering the compositor until it has passed the next inventory.
        filter_ = SCContentFilter.alloc().initWithDisplay_includingWindows_(display.native, [window for window, _ in allowed])
        if filter_ is None:
            raise DesktopCaptureRestricted("The display filter could not be created", reason="filtered_desktop_unavailable")
        config = SCStreamConfiguration.alloc().init()
        config.setWidth_(size[0])
        config.setHeight_(size[1])
        config.setShowsCursor_(False)
        config.setPreservesAspectRatio_(False)
        config.setIgnoreShadowsDisplay_(True)
        # Child windows are included by default. macOS 14.2 added this selector.
        config.setIncludeChildWindows_(False)
        requests.append((filter_, config, _NativeResult()))
    if not any(requests):
        raise DesktopCaptureRestricted("No eligible windows are visible on the active displays")
    if first_label == "Active display" and foreground is not None:
        focus = _pixel_box(ordered[0], sizes[0], foreground.bounds)
        if focus is None or not _visible_after_mask(sizes[0], [focus], pixel_masks[0]):
            first_label = "Foreground window excluded"
    deadline = time.monotonic() + _NATIVE_TIMEOUT_S
    for request in requests:
        if request is not None:
            filter_, config, callback = request
            SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(filter_, config, callback.complete)
    images = [request[2].result(deadline) if request is not None else None for request in requests]
    captured_at = datetime.now(UTC)
    if not Quartz.CGPreflightScreenCaptureAccess():
        raise DesktopCaptureRestricted("Screen recording permission is required", reason="screen_recording_permission_denied")
    after = _inventory(SCShareableContent, Quartz, workspace, sample, app_rules, domain_rules, foreground_excluded)
    if inventory != after or _mac_session() != session:
        raise DesktopCaptureRestricted("The desktop inventory or session changed during capture")
    jpeg = _encode_montage(AppKit, images, sizes, columns, first_label, pixel_masks)
    return FilteredDesktopCapture(captured_at, jpeg, first_label != "Active display")


def capture_filtered_desktop(
    sample: ActivitySample, *, platform: str, excluded_apps: tuple[str, ...] = (), excluded_domains: tuple[str, ...] = (),
    foreground_excluded: bool = False,
) -> FilteredDesktopCapture:
    """Capture eligible visible windows on every drawable display without saving pixels."""
    if platform != "darwin":
        raise DesktopCaptureRestricted("Filtered desktop capture requires macOS", reason="filtered_desktop_unavailable")
    if sample.screen_locked is not False or not sample.session_id:
        raise DesktopCaptureRestricted("The desktop session is locked or unknown")
    try:
        return _capture_macos(
            sample, _excluded_app_rules(excluded_apps), _excluded_domain_rules(excluded_domains), foreground_excluded,
        )
    except DesktopCaptureRestricted:
        raise
    except Exception:
        raise DesktopCaptureRestricted("The native filtered desktop capture failed", reason="filtered_desktop_unavailable") from None
