from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
import os
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

from deskpet.observation_v2 import desktop_capture
from deskpet.observation_v2.activity import ActivitySample
from deskpet.observation_v2.media import DesktopCaptureRestricted, capture_filtered_desktop


def _native_window(window_id, pid, x, *, name="Editor", bundle="app.editor", title=None, width=500):
    owner = SimpleNamespace(processID=lambda: pid, applicationName=lambda: name, bundleIdentifier=lambda: bundle)
    native = SimpleNamespace(windowID=lambda: window_id, owningApplication=lambda: owner,
                             windowLayer=lambda: 0, title=lambda: title)
    row = {
        "kCGWindowNumber": window_id, "kCGWindowOwnerPID": pid, "kCGWindowOwnerName": name,
        "kCGWindowLayer": 0, "kCGWindowSharingState": 1,
        "kCGWindowBounds": {"X": x, "Y": 50, "Width": width, "Height": 300},
    }
    return native, row


@pytest.fixture
def native_capture(monkeypatch):
    state = SimpleNamespace(windows=[], requested=[], inventory_calls=0, deny_permission=False, fail_image=False,
                            session=(False, "console"), display_ids=(1, 2), after_capture=None, front_pid=80,
                            screens={1: (0, 0, 1200, 800, 2400, 1600), 2: (1200, 0, 1200, 800, 2400, 1600)})
    displays = {index: SimpleNamespace(displayID=lambda index=index: index) for index in (1, 2)}
    workspace = SimpleNamespace(frontmostApplication=lambda: SimpleNamespace(processIdentifier=lambda: state.front_pid))

    def display_bounds(index):
        x, y, width, height, _, _ = state.screens[index]
        return SimpleNamespace(origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=width, height=height))

    quartz = SimpleNamespace(
        CGPreflightScreenCaptureAccess=lambda: not state.deny_permission,
        CGGetActiveDisplayList=lambda _limit, _ids, _count: (0, state.display_ids, len(state.display_ids)),
        CGDisplayBounds=display_bounds,
        CGDisplayPixelsWide=lambda index: state.screens[index][4], CGDisplayPixelsHigh=lambda index: state.screens[index][5],
        CGWindowListCopyWindowInfo=lambda _flags, _id: [row for _, row in state.windows],
        kCGWindowListOptionOnScreenOnly=1, kCGWindowListExcludeDesktopElements=2, kCGNullWindowID=0,
    )

    def get_content(exclude_desktop, on_screen_only, complete):
        assert exclude_desktop and on_screen_only
        state.inventory_calls += 1
        if state.inventory_calls == 2 and state.after_capture is not None:
            state.after_capture()
        complete(SimpleNamespace(displays=lambda: list(displays.values()),
                                 windows=lambda: [window for window, _ in state.windows]), None)

    def capture(filter_, config, complete):
        display_id, window_ids = filter_
        assert config.cursor is False
        assert config.children is False
        assert config.aspect is False
        assert config.ignore_shadows is True
        state.requested.append((display_id, window_ids, (config.width, config.height)))
        if state.fail_image:
            complete(None, "simulated error")
            return
        color = (20, 90, 180) if display_id == 1 else (90, 160, 20)
        with BytesIO() as buffer:
            Image.new("RGB", (config.width, config.height), color).save(buffer, "PNG")
            complete(buffer.getvalue(), None)

    class Config:
        def setWidth_(self, width):
            self.width = width

        def setHeight_(self, height):
            self.height = height

        def setShowsCursor_(self, cursor):
            self.cursor = cursor

        def setIncludeChildWindows_(self, children):
            self.children = children

        def setPreservesAspectRatio_(self, aspect):
            self.aspect = aspect

        def setIgnoreShadowsDisplay_(self, ignore_shadows):
            self.ignore_shadows = ignore_shadows

    appkit = SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
        NSBitmapImageRep=SimpleNamespace(alloc=lambda: SimpleNamespace(initWithCGImage_=lambda pixels: SimpleNamespace(
            representationUsingType_properties_=lambda _format, _properties: pixels,
        ))),
        NSBitmapImageFileTypePNG=4,
    )
    screen_capture_kit = SimpleNamespace(
        SCShareableContent=SimpleNamespace(
            getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_=get_content,
        ),
        SCContentFilter=SimpleNamespace(alloc=lambda: SimpleNamespace(initWithDisplay_includingWindows_=(
            lambda display, windows: (display.displayID(), tuple(window.windowID() for window in windows))
        ))),
        SCStreamConfiguration=SimpleNamespace(alloc=lambda: SimpleNamespace(init=Config)),
        SCScreenshotManager=SimpleNamespace(captureImageWithFilter_configuration_completionHandler_=capture),
    )
    for name, module in (("Quartz", quartz), ("AppKit", appkit), ("ScreenCaptureKit", screen_capture_kit)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(desktop_capture, "_mac_session", lambda: state.session)
    state.config = Config
    return state


def _sample(**changes):
    return replace(ActivitySample(datetime.now(UTC), 10, "Editor", 8, session_id="console", screen_locked=False), **changes)


def test_filters_each_display_before_acquisition_and_puts_active_display_first(native_capture):
    native_capture.windows = [
        _native_window(8, 80, 1300),  # An ordinary foreground window without a title.
        _native_window(9, 81, 1000, width=500),  # A normal window spanning both displays.
        _native_window(10, 82, 150, name="Google Chrome", bundle="com.google.Chrome"),  # Unknown mode and no title.
        _native_window(11, 82, 1300, name="Google Chrome", bundle="com.google.Chrome", title="Incognito Window"),
        _native_window(12, 83, 1300, name="1Password"),
        _native_window(13, 84, 1300, name="Secure Editor", bundle="com.example.restricted"),
        _native_window(14, 85, 1300, name="Scratchpad", title="Password reset"),
        _native_window(15, os.getpid(), 1300, name="Python"),
        _native_window(16, 86, 50, name="Mochi", bundle="io.github.mochi.deskpet"),
    ]
    result = capture_filtered_desktop(_sample(), platform="darwin", excluded_apps=(" EXAMPLE.RESTRICTED ",))
    assert result.captured_at.tzinfo is UTC
    assert not result.foreground_excluded
    assert native_capture.inventory_calls == 2
    assert [(display, window_ids) for display, window_ids, _ in native_capture.requested] == [(2, (8, 9)), (1, (9, 10))]
    with Image.open(BytesIO(result.jpeg)) as frame:
        assert frame.format == "JPEG"
        assert frame.width <= 2048
        assert frame.getpixel((100, 100))[1] > frame.getpixel((100, 100))[2]
        assert frame.getpixel((frame.width - 100, 100))[2] > frame.getpixel((frame.width - 100, 100))[1]


@pytest.mark.parametrize("private, domain, captured", [
    (False, "news.example.com", (8,)),
    (False, "sub.blocked.example.com", ()),
    (None, "news.example.com", ()),
    (True, "news.example.com", ()),
])
def test_domain_exclusions_need_verified_foreground_browser(native_capture, private, domain, captured):
    native_capture.windows = [
        _native_window(8, 80, 1300, name="Google Chrome", bundle="com.google.Chrome"),
        _native_window(9, 80, 50, name="Google Chrome", bundle="com.google.Chrome"),
        _native_window(10, 81, 150),
    ]
    sample = _sample(app_name="Google Chrome", app_id="com.google.Chrome", private_window=private, url=f"https://{domain}/")
    result = capture_filtered_desktop(sample, platform="darwin", excluded_domains=("blocked.example.com",))
    requested = {display: window_ids for display, window_ids, _ in native_capture.requested}
    assert requested == ({1: (10,), 2: captured} if captured else {1: (10,)})
    assert result.foreground_excluded is not bool(captured)


def test_excluded_foreground_keeps_only_safe_background_panels(native_capture):
    native_capture.windows = [
        _native_window(8, 80, 1300, name="Google Chrome", bundle="com.google.Chrome"),
        _native_window(9, 81, 150),
    ]
    result = capture_filtered_desktop(_sample(app_name="Google Chrome"), platform="darwin", foreground_excluded=True)
    assert result.foreground_excluded
    assert [(display, window_ids) for display, window_ids, _ in native_capture.requested] == [(1, (9,))]
    with Image.open(BytesIO(result.jpeg)) as frame:
        assert frame.width > 1200
        assert all(channel < 50 for channel in frame.getpixel((200, 150)))


def test_skips_entirely_covered_windows_before_acquisition(native_capture):
    native_capture.windows = [
        _native_window(11, 81, 50, name="1Password"),
        _native_window(8, 80, 50),
    ]
    with pytest.raises(DesktopCaptureRestricted, match="No eligible windows"):
        capture_filtered_desktop(_sample(), platform="darwin")
    assert not native_capture.requested


def test_entirely_covered_foreground_is_not_reported_as_visible(native_capture):
    native_capture.windows = [
        _native_window(11, 81, 50, name="1Password"),
        _native_window(8, 80, 50),
        _native_window(12, 82, 1300),
    ]
    result = capture_filtered_desktop(_sample(), platform="darwin")
    assert result.foreground_excluded
    assert [(display, window_ids) for display, window_ids, _ in native_capture.requested] == [(2, (12,))]


@pytest.mark.parametrize("occludes", [True, False])
def test_masks_only_overlap_with_excluded_windows_above_on_mixed_displays(native_capture, occludes):
    native_capture.screens[2] = (-900, -100, 900, 1200, 900, 1200)
    safe = _native_window(8, 80, -350, width=700)
    excluded = _native_window(9, 81, -120, name="1Password", width=300)
    native_capture.windows = [excluded, safe] if occludes else [safe, excluded]
    result = capture_filtered_desktop(_sample(), platform="darwin")
    assert [(display, windows) for display, windows, _ in native_capture.requested] == [(1, (8,)), (2, (8,))]
    cell_width = max(size[0] for _, _, size in native_capture.requested)

    def pixel(frame, display, global_x, global_y):
        x, y, width, height, _, _ = native_capture.screens[display]
        _, _, (pixel_width, pixel_height) = native_capture.requested[display - 1]
        cell_x = (display - 1) * (cell_width + 8)
        return frame.getpixel((cell_x + round((global_x - x) * pixel_width / width),
                               24 + round((global_y - y) * pixel_height / height)))

    with Image.open(BytesIO(result.jpeg)) as frame:
        assert (max(pixel(frame, 1, 80, 200)) < 50) is occludes
        assert (max(pixel(frame, 2, -80, 200)) < 50) is occludes
        assert max(pixel(frame, 1, 250, 200)) > 100
        assert max(pixel(frame, 2, -250, 200)) > 100


@pytest.mark.parametrize("change", ["new", "moved"])
def test_unrelated_excluded_window_changes_do_not_drop_ordinary_chrome(native_capture, change):
    native_capture.windows = [_native_window(8, 80, 50, name="Google Chrome", bundle="com.google.Chrome")]
    if change == "moved":
        native_capture.windows.append(_native_window(9, 85, 1300, name="Bitwarden"))

    def change_uncovered_window():
        native_capture.windows[1:] = [_native_window(9, 85, 1450, name="Bitwarden")]

    native_capture.after_capture = change_uncovered_window
    sample = _sample(app_name="Google Chrome", app_id="com.google.Chrome", url="http://127.0.0.1:8000/demo")
    result = capture_filtered_desktop(sample, platform="darwin")
    assert result.jpeg and not result.foreground_excluded
    assert [(display, window_ids) for display, window_ids, _ in native_capture.requested] == [(1, (8,))]


@pytest.mark.parametrize("changed, reason", [
    ("newly_occluded", "desktop_inventory_changed"),
    ("new_allowed_window", "desktop_inventory_changed"),
    ("newly_private", "desktop_inventory_changed"),
    ("display", "desktop_inventory_changed"),
    ("session", "desktop_session_unverified"),
])
def test_rejects_changed_post_capture_inventory_without_returning_pixels(native_capture, changed, reason):
    native_capture.windows = [_native_window(8, 80, 50)]

    def change():
        if changed == "newly_occluded":
            native_capture.windows.insert(0, _native_window(9, 85, 100, name="Bitwarden"))
        elif changed == "new_allowed_window":
            native_capture.windows.append(_native_window(9, 85, 1300))
        elif changed == "newly_private":
            native_capture.windows[:] = [_native_window(8, 80, 50, title="Private Browsing")]
        elif changed == "display":
            native_capture.display_ids = (1,)
        else:
            native_capture.session = (True, "console")

    native_capture.after_capture = change
    with pytest.raises(DesktopCaptureRestricted, match="changed") as error:
        capture_filtered_desktop(_sample(), platform="darwin")
    assert error.value.reason == reason
    assert [(display, window_ids) for display, window_ids, _ in native_capture.requested] == [(1, (8,))]


@pytest.mark.parametrize("condition, reason", [
    ("permission", "screen_recording_permission_denied"),
    ("child_filter", "filtered_desktop_unavailable"),
    ("empty", "desktop_no_visible_windows"),
    ("display_missing", "desktop_display_inventory_unverified"),
    ("window_inventory", "desktop_window_inventory_unverified"),
    ("session", "desktop_session_unverified"),
    ("acquisition", "filtered_desktop_unavailable"),
])
def test_never_uses_unfiltered_fallback(native_capture, monkeypatch, condition, reason):
    native_capture.windows = [_native_window(8, 80, 50)]
    if condition == "permission":
        native_capture.deny_permission = True
    elif condition == "child_filter":
        monkeypatch.delattr(native_capture.config, "setIncludeChildWindows_")
    elif condition == "empty":
        native_capture.windows[:] = [_native_window(8, 80, 50, name="1Password")]
    elif condition == "display_missing":
        native_capture.display_ids = (1, 2, 3)
    elif condition == "window_inventory":
        monkeypatch.setattr(sys.modules["Quartz"], "CGWindowListCopyWindowInfo", lambda _flags, _id: None)
    elif condition == "session":
        native_capture.session = (False, "different session")
    elif condition == "acquisition":
        native_capture.fail_image = True
    with pytest.raises(DesktopCaptureRestricted) as error:
        capture_filtered_desktop(_sample(), platform="darwin")
    assert error.value.reason == reason
    assert len(native_capture.requested) == int(condition == "acquisition")
