"""Local visual change detection and a bounded upload schedule."""

from __future__ import annotations

from collections import OrderedDict
from io import BytesIO
import re

from PIL import Image, ImageFilter


ACTIVITY_POLL_S = 1.5
FOREGROUND_SETTLE_S = 3.0
UPLOAD_INTERVAL_S = 30.0
VISUAL_REVIEW_S = 120.0
_THUMBNAIL_SIZE = (96, 54)
_VIDEO_APPS = frozenset({
    "iina", "movies & tv", "mpv", "quicktime player", "vlc", "vlc media player", "windows media player", "wmplayer",
})
_VIDEO_DOMAINS = frozenset({"youtube.com", "youtu.be", "twitch.tv", "netflix.com", "bilibili.com", "vimeo.com"})
_VIDEO_TITLE = re.compile(r"(?:[-–|] |^)(?:youtube|twitch|netflix|bilibili|哔哩哔哩)(?:$|\b)", re.IGNORECASE)


def known_video_source(app: str | None, title: str | None, domain: str | None) -> bool:
    app = (app or "").casefold().removesuffix(".exe")
    return (
        app in _VIDEO_APPS
        or bool(domain and any(domain == item or domain.endswith("." + item) for item in _VIDEO_DOMAINS))
        or bool(title and _VIDEO_TITLE.search(title))
    )


def frame_thumbnail(image: bytes) -> bytes:
    """Keep only a small blurred luminance map in memory for frame comparison."""
    with Image.open(BytesIO(image)) as frame:
        return frame.convert("L").resize(_THUMBNAIL_SIZE, Image.Resampling.BOX).filter(
            ImageFilter.BoxBlur(1),
        ).tobytes()


def materially_different(previous: bytes, current: bytes) -> bool:
    # A cursor or one blinking caret affects a few pixels. A content change
    # must survive downsampling and affect an area of the window.
    return sum(abs(left - right) >= 10 for left, right in zip(previous, current, strict=True)) >= 18


class CaptureSchedule:
    def __init__(self, *, last_upload_s: float = float("-inf")) -> None:
        self.last_upload_s = last_upload_s
        self.identity: object | None = None
        self.window: object | None = None
        self.stable_since_s = 0.0
        self.next_probe_s = float("inf")
        self.review_s = float("inf")
        self.soon_s = float("inf")
        self.soon_used = False
        self.latest: bytes | None = None
        self.uploaded: OrderedDict[object, bytes] = OrderedDict()
        self.video = False
        self.moving = 0

    def observe(self, identity: object | None, monotonic_s: float, *, window: object | None = None) -> None:
        window = identity if window is None else window
        if identity == self.identity and window == self.window:
            return
        self.identity = identity
        self.window = window
        self.stable_since_s = monotonic_s
        self.latest = None
        self.video = self.soon_used = False
        self.moving = 0
        self.soon_s = float("inf")
        self.next_probe_s = self.review_s = monotonic_s + FOREGROUND_SETTLE_S

    def due(self, monotonic_s: float, *, interactive: bool) -> bool:
        if self.identity is None:
            return False
        if monotonic_s < max(self.next_probe_s, self.last_upload_s + UPLOAD_INTERVAL_S - ACTIVITY_POLL_S):
            return False
        return (interactive and not self.video) or monotonic_s >= min(self.soon_s, self.review_s)

    def inspect(self, thumbnail: bytes, monotonic_s: float) -> bool:
        """Admit the first settled foreground, then look for materially new frames."""
        previous, self.latest = self.latest, thumbnail
        self.next_probe_s = monotonic_s + ACTIVITY_POLL_S
        if self.soon_s <= monotonic_s:
            self.soon_s = float("inf")
        moving = previous is not None and materially_different(previous, thumbnail)
        self.moving = self.moving + 1 if moving else 0
        if (monotonic_s < self.stable_since_s + FOREGROUND_SETTLE_S
                or monotonic_s < self.last_upload_s + UPLOAD_INTERVAL_S):
            self.review_s = self.next_probe_s
            return False
        prior_upload = self.uploaded.get(self.window)
        if self.video and (previous is None or moving):
            # A known video can leave the same window. Look for a settled view
            # in a bounded local check before making another request.
            self.review_s = self.next_probe_s
            if self.moving >= 3:
                self.moving = 0
                self.latest = None
                self.next_probe_s = self.review_s = monotonic_s + VISUAL_REVIEW_S
            return False
        if prior_upload is not None and moving and self.moving < 3:
            self.review_s = self.next_probe_s
            return False
        self.review_s = monotonic_s + VISUAL_REVIEW_S
        return prior_upload is None or materially_different(prior_upload, thumbnail)

    def probe_failed(self, monotonic_s: float) -> None:
        self.latest = None
        self.next_probe_s = self.review_s = monotonic_s + UPLOAD_INTERVAL_S

    def sent(self, monotonic_s: float) -> None:
        self.last_upload_s = monotonic_s
        if self.latest is not None:
            self.uploaded[self.window] = self.latest
            self.uploaded.move_to_end(self.window)
            if len(self.uploaded) > 8:
                self.uploaded.popitem(last=False)

    def recommend(self, next_check: str | None, monotonic_s: float, *, video: bool = False) -> None:
        was_video = self.video
        self.video = video
        if self.video:
            self.latest = None
            self.soon_s = float("inf")
            self.next_probe_s = self.review_s = monotonic_s + VISUAL_REVIEW_S
        elif was_video:
            self.next_probe_s = monotonic_s + ACTIVITY_POLL_S
            self.review_s = monotonic_s + VISUAL_REVIEW_S
        if next_check == "check_soon" and not self.soon_used and not self.video:
            self.soon_used = True
            self.soon_s = max(monotonic_s + FOREGROUND_SETTLE_S, self.last_upload_s + UPLOAD_INTERVAL_S)
