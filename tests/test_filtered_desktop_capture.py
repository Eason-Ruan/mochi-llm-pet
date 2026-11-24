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
from deskpet.observation_v2.media import DesktopCaptureRestricted, capture_filtered_desktop, capture_unfiltered_desktop


def _native_window(window_id, pid, x, *, name="Editor", bundle="app.editor", title=None,
                   width=500, y=50, height=300, layer=0):
    owner = SimpleNamespace(processID=lambda: pid, applicationName=lambda: name, bundleIdentifier=lambda: bundle)
    native = SimpleNamespace(
        windowID=lambda: window_id, owningApplication=lambda: owner, windowLayer=lambda: layer, title=lambda: title,
        frame=lambda: SimpleNamespace(origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=width, height=height)),
    )
    row = {
        "kCGWindowNumber": window_id, "kCGWindowOwnerPID": pid, "kCGWindowOwnerName": name,
        "kCGWindowLayer": layer, "kCGWindowSharingState": 1,
        "kCGWindowBounds": {"X": x, "Y": y, "Width": width, "Height": height},
    }
    return native, row


def _screenshot_overlay(state, *, window_id=9, x=0, title=None, path="/System/Library/CoreServices/screencaptureui.app"):
    name = "Screenshot"
    bundle = "com.apple.screencaptureui"
    state.applications[81] = SimpleNamespace(
        bundleIdentifier=lambda: bundle,
        bundleURL=lambda: SimpleNamespace(path=lambda: path),
        localizedName=lambda: name,
    )
    native, row = _native_window(window_id, 81, x, name=name, bundle=bundle, title=title,
                                 y=0, width=1200, height=800, layer=24)
    row["kCGWindowAlpha"] = 1.0
    return native, row


def _dock_background(state):
    state.applications[90] = SimpleNamespace(
        bundleIdentifier=lambda: "com.apple.dock",
        bundleURL=lambda: SimpleNamespace(path=lambda: "/System/Library/CoreServices/Dock.app"),
        localizedName=lambda: "Dock",
    )
    native, row = _native_window(13, 90, 0, name="Dock", bundle="com.apple.dock", title="Dock",
                                 y=0, width=1200, height=800, layer=20)
    row.update(kCGWindowName="Dock", kCGWindowAlpha=1.0)
    return native, row


@pytest.fixture
def native_capture(monkeypatch):
    state = SimpleNamespace(windows=[], requested=[], inventory_calls=0, deny_permission=False, fail_image=False,
                            session=(False, "console"), display_ids=(1, 2), after_capture=None, front_pid=80,
                            applications={}, quartz_only=set(), sck_only=set(),
                            single_requested=[], dock_image=None, raw_requested=[],
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
        CGWindowListCopyWindowInfo=lambda _flags, _id: [row for window, row in state.windows
                                                      if window.windowID() not in state.sck_only],
        kCGWindowListOptionOnScreenOnly=1, kCGWindowListExcludeDesktopElements=2, kCGNullWindowID=0,
    )

    def get_content(exclude_desktop, on_screen_only, complete):
        assert on_screen_only
        state.inventory_calls += 1
        if state.inventory_calls == 2 and state.after_capture is not None:
            state.after_capture()
        content = SimpleNamespace(displays=lambda: list(displays.values()))
        if exclude_desktop:
            content.windows = lambda: [window for window, _ in state.windows if window.windowID() not in state.quartz_only]
        complete(content, None)

    def capture(filter_, config, complete):
        display_id, window_ids = filter_.display_id, filter_.window_ids
        assert config.cursor is False
        assert config.aspect is False
        size = (config.width, config.height)
        if display_id is None:
            assert config.children is False
            assert config.ignore_single_window_shadows is True
            assert config.opaque is False
            assert config.scales_to_fit is True
            state.single_requested.append((window_ids, size))
            output = state.dock_image(size) if state.dock_image is not None else None
        else:
            if window_ids is None:
                assert not hasattr(config, "children")
                assert not hasattr(config, "ignore_shadows")
                state.raw_requested.append((display_id, size))
            else:
                assert config.children is False
                assert config.ignore_shadows is True
                state.requested.append((display_id, window_ids, size))
            color = (20, 90, 180) if display_id == 1 else (90, 160, 20)
            output = Image.new("RGB", size, color) if not state.fail_image else None
        if output is None:
            complete(None, "simulated error")
            return
        with output, BytesIO() as buffer:
            output.save(buffer, "PNG")
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

        def setIgnoreShadowsSingleWindow_(self, ignore_shadows):
            self.ignore_single_window_shadows = ignore_shadows

        def setShouldBeOpaque_(self, opaque):
            self.opaque = opaque

        def setScalesToFit_(self, scales_to_fit):
            self.scales_to_fit = scales_to_fit

    def whole_display_filter(display, exclusions):
        assert not exclusions
        return SimpleNamespace(display_id=display.displayID(), window_ids=None)

    appkit = SimpleNamespace(
        NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=state.applications.get),
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
        SCContentFilter=SimpleNamespace(alloc=lambda: SimpleNamespace(
            initWithDisplay_excludingWindows_=whole_display_filter,
            initWithDisplay_includingWindows_=lambda display, windows: SimpleNamespace(
                display_id=display.displayID(), window_ids=tuple(window.windowID() for window in windows),
            ),
            initWithDesktopIndependentWindow_=lambda window: SimpleNamespace(
                display_id=None, window_ids=(window.windowID(),), pointPixelScale=lambda: 1.0,
            ),
        )),
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


@pytest.mark.parametrize("quartz_only", [True, False])
def test_inactive_screenshot_overlay_keeps_underlying_windows_visible(native_capture, quartz_only):
    native_capture.windows = [
        _screenshot_overlay(native_capture),
        _screenshot_overlay(native_capture, window_id=12, x=1200),
        _native_window(8, 80, 50, name="Google Chrome", bundle="com.google.Chrome"),
        _native_window(10, 82, 1300),
    ]
    if quartz_only:
        native_capture.quartz_only.update((9, 12))

    def remove_first_overlay():
        del native_capture.windows[0]

    native_capture.after_capture = remove_first_overlay
    sample = _sample(app_name="Google Chrome", app_id="com.google.Chrome")
    result = capture_filtered_desktop(sample, platform="darwin")
    assert not result.foreground_excluded
    assert [(display, windows) for display, windows, _ in native_capture.requested] == [(1, (8,)), (2, (10,))]
    with Image.open(BytesIO(result.jpeg)) as frame:
        assert frame.getpixel((100, 150))[2] > 100


@pytest.mark.parametrize("blocked_by", ["blacklisted_app", "incognito"])
def test_inactive_screenshot_overlay_keeps_private_occlusion(native_capture, blocked_by):
    if blocked_by == "blacklisted_app":
        restricted_app = _native_window(11, 83, 100, name="Company Vault", bundle="com.example.vault", width=300)
        excluded_apps = ("com.example.vault",)
    else:
        restricted_app = _native_window(11, 83, 100, name="Google Chrome", bundle="com.google.Chrome",
                                        title="Incognito Window", width=300)
        excluded_apps = ()
    native_capture.display_ids = (1,)
    native_capture.windows = [_screenshot_overlay(native_capture), restricted_app, _native_window(8, 80, 50)]

    result = capture_filtered_desktop(_sample(), platform="darwin", excluded_apps=excluded_apps)
    assert not result.foreground_excluded
    assert [(display, windows) for display, windows, _ in native_capture.requested] == [(1, (8,))]
    with Image.open(BytesIO(result.jpeg)) as frame:
        assert max(frame.getpixel((200, 150))) < 50
        assert frame.getpixel((500, 150))[2] > 100


@pytest.mark.parametrize("condition", [
    "blacklisted", "foreground", "unknown_owner", "wrong_path", "native_title",
    "missing_alpha", "translucent", "active_peer", "sck_only_peer",
])
def test_screenshot_overlay_without_inactive_fingerprint_still_occludes(native_capture, condition):
    overlay = _screenshot_overlay(
        native_capture,
        path="/Applications/screencaptureui.app" if condition == "wrong_path"
        else "/System/Library/CoreServices/screencaptureui.app",
        title="Screenshot" if condition == "native_title" else None,
    )
    if condition == "unknown_owner":
        native_capture.applications.clear()
        native_capture.quartz_only.add(9)
    if condition == "foreground":
        native_capture.front_pid = 81
    if condition == "missing_alpha":
        overlay[1].pop("kCGWindowAlpha")
    elif condition == "translucent":
        overlay[1]["kCGWindowAlpha"] = 0.9
    excluded_apps = ("com.apple.screencaptureui",) if condition == "blacklisted" else ()
    native_capture.windows = [overlay, _native_window(8, 80, 50)]
    if condition in {"active_peer", "sck_only_peer"}:
        native_capture.windows.insert(0, _native_window(
            12, 81, 500, name="Screenshot", bundle="com.apple.screencaptureui", width=200, height=80, layer=24,
        ))
    if condition == "sck_only_peer":
        native_capture.sck_only.add(12)

    with pytest.raises(DesktopCaptureRestricted, match="No eligible windows"):
        capture_filtered_desktop(_sample(), platform="darwin", excluded_apps=excluded_apps)
    assert not native_capture.requested


def test_dock_alpha_reveals_only_visible_approved_pixels(native_capture):
    native_capture.display_ids = (1,)
    native_capture.windows = [
        _dock_background(native_capture),
        _native_window(11, 83, 100, name="Google Chrome", bundle="com.google.Chrome", title="Incognito", width=160),
        _native_window(8, 80, 50),
    ]

    def sparse_dock_alpha(size):
        width, height = size
        frame = Image.new("RGBA", size, (220, 40, 30, 0))
        frame.paste((220, 40, 30, 1), (width * 3 // 12, height // 8, width * 4 // 12, height // 4))
        return frame

    native_capture.dock_image = sparse_dock_alpha
    result = capture_filtered_desktop(_sample(), platform="darwin")
    assert not result.foreground_excluded
    assert native_capture.single_requested
    assert {windows for windows, _ in native_capture.single_requested} == {(13,)}
    assert [(display, windows) for display, windows, _ in native_capture.requested] == [(1, (8,))]
    with Image.open(BytesIO(result.jpeg)) as frame:
        assert max(frame.getpixel((170, 150))) < 50  # Redact content behind the incognito window.
        assert max(frame.getpixel((350, 150))) < 50  # Even a nonzero alpha of one remains occluding.
        assert frame.getpixel((475, 150))[2] > 100


def test_fully_transparent_dock_background_keeps_underlying_window(native_capture):
    native_capture.display_ids = (1,)
    native_capture.windows = [_dock_background(native_capture), _native_window(8, 80, 50)]
    native_capture.dock_image = lambda size: Image.new("RGBA", size, (220, 40, 30, 0))

    result = capture_filtered_desktop(_sample(), platform="darwin")
    assert not result.foreground_excluded
    assert [(display, windows) for display, windows, _ in native_capture.requested] == [(1, (8,))]
    with Image.open(BytesIO(result.jpeg)) as frame:
        assert frame.getpixel((350, 150))[2] > 100


@pytest.mark.parametrize("condition", ["opaque", "unknown_alpha", "native_error"])
def test_dock_with_unverified_or_opaque_alpha_keeps_full_occlusion(native_capture, condition):
    native_capture.display_ids = (1,)
    native_capture.windows = [_dock_background(native_capture), _native_window(8, 80, 50)]
    if condition == "opaque":
        native_capture.dock_image = lambda size: Image.new("RGBA", size, (220, 40, 30, 255))
    elif condition == "unknown_alpha":
        native_capture.dock_image = lambda size: Image.new("RGB", size, (220, 40, 30))

    with pytest.raises(DesktopCaptureRestricted, match="No eligible windows"):
        capture_filtered_desktop(_sample(), platform="darwin")
    assert native_capture.single_requested
    assert not native_capture.requested


def test_changed_dock_alpha_rejects_the_captured_frame(native_capture):
    native_capture.display_ids = (1,)
    native_capture.windows = [_dock_background(native_capture), _native_window(8, 80, 50)]
    native_capture.dock_image = lambda size: Image.new("RGBA", size, (220, 40, 30, 0))

    def dock_becomes_opaque():
        native_capture.dock_image = lambda size: Image.new("RGBA", size, (220, 40, 30, 255))

    native_capture.after_capture = dock_becomes_opaque
    with pytest.raises(DesktopCaptureRestricted) as error:
        capture_filtered_desktop(_sample(), platform="darwin")
    assert error.value.reason == "desktop_inventory_changed"
    assert [windows for windows, _ in native_capture.single_requested] == [(13,), (13,)]
    assert [(display, windows) for display, windows, _ in native_capture.requested] == [(1, (8,))]


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
    assert not native_capture.raw_requested


def test_unfiltered_capture_uses_only_display_metadata(native_capture, monkeypatch):
    def forbidden(*args):
        pytest.fail("Unfiltered display capture must not inspect windows or foreground applications")

    monkeypatch.setattr(sys.modules["Quartz"], "CGWindowListCopyWindowInfo", forbidden)
    monkeypatch.setattr(sys.modules["AppKit"].NSWorkspace, "sharedWorkspace", forbidden)
    result = capture_unfiltered_desktop(_sample(app_name=None, window_id=None, private_window=True), platform="darwin")
    assert result.captured_at.tzinfo is UTC
    assert not result.foreground_excluded
    assert native_capture.inventory_calls == 2
    assert not native_capture.requested
    assert [display for display, _ in native_capture.raw_requested] == [1, 2]
    with Image.open(BytesIO(result.jpeg)) as frame:
        assert frame.format == "JPEG"
        assert frame.width <= 2048
        assert frame.getpixel((100, 100))[2] > frame.getpixel((100, 100))[1]
        assert frame.getpixel((frame.width - 100, 100))[1] > frame.getpixel((frame.width - 100, 100))[2]


@pytest.mark.parametrize("attribute, value, reason", [
    ("session", (True, "console"), "desktop_session_unverified"),
    ("session", (False, "other console"), "desktop_session_unverified"),
    ("display_ids", (1,), "desktop_inventory_changed"),
    ("deny_permission", True, "screen_recording_permission_denied"),
])
def test_unfiltered_capture_discards_frame_when_native_state_changes(native_capture, attribute, value, reason):
    native_capture.after_capture = lambda: setattr(native_capture, attribute, value)
    with pytest.raises(DesktopCaptureRestricted) as error:
        capture_unfiltered_desktop(_sample(), platform="darwin")
    assert error.value.reason == reason
    assert [display for display, _ in native_capture.raw_requested] == [1, 2]


@pytest.mark.parametrize("platform, changes, native_changes, reason", [
    ("win32", {}, {}, "unfiltered_desktop_unavailable"),
    ("darwin", {"screen_locked": True}, {}, "desktop_session_unverified"),
    ("darwin", {"session_id": None}, {}, "desktop_session_unverified"),
    ("darwin", {}, {"deny_permission": True}, "screen_recording_permission_denied"),
    ("darwin", {}, {"display_ids": (1, 2, 3)}, "desktop_display_inventory_unverified"),
])
def test_unfiltered_capture_skips_acquisition_without_valid_native_state(native_capture, platform, changes, native_changes, reason):
    for attribute, value in native_changes.items():
        setattr(native_capture, attribute, value)
    with pytest.raises(DesktopCaptureRestricted) as error:
        capture_unfiltered_desktop(_sample(**changes), platform=platform)
    assert error.value.reason == reason
    assert not native_capture.raw_requested


def test_unfiltered_capture_fails_if_a_native_capture_fails(native_capture):
    native_capture.fail_image = True
    with pytest.raises(DesktopCaptureRestricted) as error:
        capture_unfiltered_desktop(_sample(), platform="darwin")
    assert error.value.reason == "unfiltered_desktop_unavailable"
    assert native_capture.inventory_calls == 1
    assert not native_capture.requested
