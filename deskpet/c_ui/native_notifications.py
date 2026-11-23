"""Platform notifications with explicit capability and submission results."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import sys


MAC_BUNDLE_ID = "io.github.mochi.deskpet"
GENERIC_TITLE = "Mochi 有一条提醒"
GENERIC_BODY = "打开 Mochi 查看提醒。"


def notification_id(delivery_id: str) -> str:
    # Windows tags are limited to 64 characters. Never expose a task ID to the OS.
    return sha256(delivery_id.encode()).hexdigest()


class UnavailableNotifications:
    can_schedule = False
    can_verify_removal = False

    def __init__(self, reason="当前桌面环境不支持系统通知"):
        self.status = reason

    available = False

    def refresh(self, *, request=False):
        pass

    def submit(self, identifier, due_at, *, sound, complete):
        complete("failed", self.status)

    def inventory(self, complete):
        complete(None, None)

    def remove_pending(self, identifier):
        return False


class MacNotifications:
    can_schedule = True
    can_verify_removal = False

    def __init__(self):
        from Foundation import NSBundle
        from UserNotifications import UNUserNotificationCenter

        bundle = NSBundle.mainBundle()
        if bundle.bundleIdentifier() != MAC_BUNDLE_ID or not str(bundle.bundlePath()).endswith(".app"):
            raise RuntimeError("系统通知需要从 Mochi.app 启动")
        self.center = UNUserNotificationCenter.currentNotificationCenter()
        self.available = False
        self.status = "正在查询系统通知权限"
        self.sound_available = False
        self._refreshing = False
        self._request_pending = False
        self._authorizing = False
        self.delegate = self._delegate()
        self.center.setDelegate_(self.delegate)
        self.refresh()

    @staticmethod
    @lru_cache(maxsize=1)
    def _delegate():
        import objc
        from Foundation import NSObject
        from UserNotifications import (
            UNNotificationPresentationOptionBanner, UNNotificationPresentationOptionList,
            UNNotificationPresentationOptionSound,
        )

        class MochiNotificationDelegate(NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]):
            def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, notification, complete):
                options = UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList
                if notification.request().content().sound() is not None:
                    options |= UNNotificationPresentationOptionSound
                complete(options)

        return MochiNotificationDelegate.alloc().init()

    def refresh(self, *, request=False):
        from UserNotifications import (
            UNAuthorizationOptionAlert, UNAuthorizationOptionSound, UNAuthorizationStatusAuthorized,
            UNAuthorizationStatusDenied, UNAuthorizationStatusNotDetermined, UNNotificationSettingEnabled,
        )

        self._request_pending |= request
        if self._refreshing or self._authorizing:
            return
        self._refreshing = True

        def checked(settings):
            if settings is None:
                self.available = self.sound_available = False
                self.status = "系统通知权限状态暂不可用"
                self._refreshing = False
                return
            state = settings.authorizationStatus()
            self.available = state == UNAuthorizationStatusAuthorized and settings.alertSetting() == UNNotificationSettingEnabled
            self.sound_available = self.available and settings.soundSetting() == UNNotificationSettingEnabled
            if self.available:
                self.status = "可用" if self.sound_available else "可用，系统通知音已关闭"
            elif state == UNAuthorizationStatusDenied:
                self.status = "系统通知权限已拒绝"
            elif state == UNAuthorizationStatusNotDetermined:
                self.status = "系统通知尚未授权"
            else:
                self.status = "系统已关闭通知横幅"

            ask = self._request_pending and state == UNAuthorizationStatusNotDetermined
            self._request_pending = False
            self._authorizing = ask
            self._refreshing = False
            if ask:
                self.center.requestAuthorizationWithOptions_completionHandler_(
                    UNAuthorizationOptionAlert | UNAuthorizationOptionSound, authorized,
                )

        def authorized(granted, error):
            self._request_pending = False
            self._authorizing = False
            self.refresh()

        self.center.getNotificationSettingsWithCompletionHandler_(checked)

    def submit(self, identifier, due_at, *, sound, complete):
        from UserNotifications import (
            UNMutableNotificationContent, UNNotificationRequest, UNNotificationSound,
            UNTimeIntervalNotificationTrigger,
        )

        if not self.available:
            complete("failed", self.status)
            return
        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(GENERIC_TITLE)
        content.setBody_(GENERIC_BODY)
        if sound and self.sound_available:
            content.setSound_(UNNotificationSound.defaultSound())
        trigger = None
        if due_at is not None:
            delay = (due_at - datetime.now(timezone.utc)).total_seconds()
            if delay < 1:
                complete("failed", "系统预登记时间已到")
                return
            trigger = UNTimeIntervalNotificationTrigger.triggerWithTimeInterval_repeats_(delay, False)
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(identifier, content, trigger)

        def submitted(error):
            if error is not None:
                complete("failed", f"系统拒绝通知（{error.domain()} {error.code()}）")
            else:
                complete("scheduled" if due_at is not None else "submitted", None)

        self.center.addNotificationRequest_withCompletionHandler_(request, submitted)

    def inventory(self, complete):
        def pending(requests):
            if requests is None:
                complete(None, None)
                return
            queued = frozenset(str(request.identifier()) for request in requests)

            def delivered(notifications):
                seen = None if notifications is None else frozenset(
                    str(item.request().identifier()) for item in notifications
                )
                complete(queued, seen)

            self.center.getDeliveredNotificationsWithCompletionHandler_(delivered)

        self.center.getPendingNotificationRequestsWithCompletionHandler_(pending)

    def remove_pending(self, identifier):
        self.center.removePendingNotificationRequestsWithIdentifiers_([identifier])
        return True


class WindowsNotifications:
    # A Start-menu AUMID gives a Win32 desktop process immediate toasts.
    can_schedule = False
    can_verify_removal = False
    group = "mochi-reminders"

    def __init__(self):
        from winrt.windows.ui.notifications import ToastNotificationManager

        from .windows_notification_identity import activate_notification_identity

        self.app_id = activate_notification_identity()
        self.manager = ToastNotificationManager
        self.notifier = self.manager.create_toast_notifier_with_id(self.app_id)
        self.refresh()

    def refresh(self, *, request=False):
        from winrt.windows.ui.notifications import NotificationSetting

        setting = self.notifier.setting
        self.available = setting == NotificationSetting.ENABLED
        self.status = "可用（Mochi 运行时可发送）" if self.available else f"Windows 通知不可用（{setting.name}）"

    def submit(self, identifier, due_at, *, sound, complete):
        from winrt.windows.data.xml.dom import XmlDocument
        from winrt.windows.ui.notifications import ToastNotification

        if due_at is not None:
            complete("failed", "当前 Windows 桌面通道无法提前登记")
            return
        self.refresh()
        if not self.available:
            complete("failed", self.status)
            return
        xml = XmlDocument()
        audio = "" if sound else '<audio silent="true"/>'
        # Only generic constants go to Windows, including when the desktop is locked.
        xml.load_xml(f'<toast><visual><binding template="ToastGeneric"><text>{GENERIC_TITLE}</text>'
                     f'<text>{GENERIC_BODY}</text></binding></visual>{audio}</toast>')
        item = ToastNotification(xml)
        item.tag, item.group = identifier, self.group
        self.notifier.show(item)
        complete("submitted", None)

    def inventory(self, complete):
        delivered = self.manager.history.get_history_with_id(self.app_id)
        complete(frozenset(), frozenset(item.tag for item in delivered if item.group == self.group))

    def remove_pending(self, identifier):
        return False


def platform_notifications(*, native: bool):
    if not native:
        return UnavailableNotifications("离屏模式仅验证桌宠通道")
    backend = {"darwin": MacNotifications, "win32": WindowsNotifications}.get(sys.platform)
    if backend is None:
        return UnavailableNotifications()
    try:
        return backend()
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError) as exc:
        return UnavailableNotifications(str(exc) if isinstance(exc, RuntimeError) else "系统通知组件不可用")
