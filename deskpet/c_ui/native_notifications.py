"""Platform notifications with explicit capability and submission results."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import sys
from threading import Lock, Timer

from deskpet.i18n import tr
from deskpet.task_service.presentation import notification_id


MAC_BUNDLE_ID = "io.github.mochi.deskpet"
GENERIC_TITLE = "Mochi 有一条提醒"
GENERIC_BODY = "打开 Mochi 查看提醒。"
SNOOZE_SECONDS = 300
ACK_ACTION = "mochi.reminder.ack"
SNOOZE_ACTION = "mochi.reminder.snooze.5m"
REMINDER_CATEGORY = "mochi.reminder.ack.v1"
SNOOZE_CATEGORY = "mochi.reminder.snooze.v1"
REMINDER_CATEGORIES = frozenset({REMINDER_CATEGORY, SNOOZE_CATEGORY})


def _generic_content() -> tuple[str, str]:
    return tr(GENERIC_TITLE, "Mochi has a reminder"), tr(GENERIC_BODY, "Open Mochi to view the reminder.")


def _valid_identifier(identifier: str) -> bool:
    return len(identifier) == 64 and all(char in "0123456789abcdef" for char in identifier)


def _foreground_decision(identifier, options, complete, handler):
    """Keep the platform deadline covered if the UI cannot claim a first paint."""
    mutex = Lock()
    answered = False

    def decide(suppress, *, permit=None):
        nonlocal answered
        with mutex:
            if answered:
                return False
            answered = True
            try:
                suppress = bool(suppress and (permit is None or permit()))
            except Exception:
                suppress = False
        timer.cancel()
        complete(0 if suppress else options)
        return suppress

    timer = Timer(0.35, lambda: decide(False))
    timer.daemon = True
    timer.start()
    try:
        handler(identifier, decide)
    except Exception:
        decide(False)


class UnavailableNotifications:
    can_schedule = False
    can_verify_removal = False
    can_respond = False

    def __init__(self, reason=None):
        self.status = reason if reason is not None else tr(
            "当前桌面环境不支持系统通知", "System notifications are unsupported on this desktop",
        )

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
    can_respond = True
    # The two inventories can confirm an early handover while the original
    # scheduled fire time is still safely in the future.
    can_verify_early_transfer = True

    def __init__(self):
        from Foundation import NSBundle
        from UserNotifications import UNUserNotificationCenter

        bundle = NSBundle.mainBundle()
        if bundle.bundleIdentifier() != MAC_BUNDLE_ID or not str(bundle.bundlePath()).endswith(".app"):
            raise RuntimeError(tr("系统通知需要从 Mochi.app 启动", "Launch Mochi.app to use system notifications"))
        self.center = UNUserNotificationCenter.currentNotificationCenter()
        self.available = False
        self.status = tr("正在查询系统通知权限", "Checking system notification permission")
        self.sound_available = False
        self._refreshing = False
        self._request_pending = False
        self._authorizing = False
        self._response_lock = Lock()
        self._queued_responses = []
        self._response_handler = None
        self.delegate = self._delegate_class().alloc().init()
        self.delegate.response_handler = self._receive_response
        self.delegate.foreground_handler = None
        self.center.setDelegate_(self.delegate)
        self._register_categories()
        self.refresh()

    def set_response_handler(self, handler):
        with self._response_lock:
            self._response_handler = handler
            responses, self._queued_responses = self._queued_responses, []
        for identifier, action in responses:
            handler(identifier, action)

    def _receive_response(self, identifier, action):
        with self._response_lock:
            handler = self._response_handler
            if handler is None:
                self._queued_responses.append((identifier, action))
                return
        handler(identifier, action)

    def set_foreground_handler(self, handler):
        self.delegate.foreground_handler = handler

    def _register_categories(self):
        from UserNotifications import (
            UNNotificationAction, UNNotificationActionOptionAuthenticationRequired, UNNotificationCategory,
        )

        options = UNNotificationActionOptionAuthenticationRequired
        acknowledge = UNNotificationAction.actionWithIdentifier_title_options_(ACK_ACTION, tr("确认收到", "Acknowledge"), options)
        snooze = UNNotificationAction.actionWithIdentifier_title_options_(SNOOZE_ACTION, tr("5 分钟后提醒", "Snooze 5 minutes"), options)
        categories = {
            UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
                REMINDER_CATEGORY, [acknowledge], [], 0,
            ),
            UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
                SNOOZE_CATEGORY, [acknowledge, snooze], [], 0,
            ),
        }
        self.center.setNotificationCategories_(categories)

    @staticmethod
    @lru_cache(maxsize=1)
    def _delegate_class():
        import objc
        from Foundation import NSObject
        from UserNotifications import (
            UNNotificationDefaultActionIdentifier,
            UNNotificationPresentationOptionBanner, UNNotificationPresentationOptionList,
            UNNotificationPresentationOptionSound,
        )

        class MochiNotificationDelegate(NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]):
            def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, notification, complete):
                request = notification.request()
                options = UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList
                if request.content().sound() is not None:
                    options |= UNNotificationPresentationOptionSound
                identifier = str(request.identifier())
                handler = self.foreground_handler
                if (handler is not None and _valid_identifier(identifier)
                        and str(request.content().categoryIdentifier()) in REMINDER_CATEGORIES):
                    _foreground_decision(identifier, options, complete, handler)
                else:
                    complete(options)

            def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
                self, center, response, complete,
            ):
                try:
                    request = response.notification().request()
                    identifier = str(request.identifier())
                    action = str(response.actionIdentifier())
                    category = str(request.content().categoryIdentifier())
                    if not _valid_identifier(identifier):
                        return
                    if action == UNNotificationDefaultActionIdentifier and category in REMINDER_CATEGORIES | {""}:
                        self.response_handler(identifier, "open")
                    elif action == ACK_ACTION and category in REMINDER_CATEGORIES:
                        self.response_handler(identifier, "ack")
                    elif action == SNOOZE_ACTION and category == SNOOZE_CATEGORY:
                        self.response_handler(identifier, "snooze")
                finally:
                    complete()

        return MochiNotificationDelegate

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
                self.status = tr("系统通知权限状态暂不可用", "System notification permission status is temporarily unavailable")
                self._refreshing = False
                return
            state = settings.authorizationStatus()
            self.available = state == UNAuthorizationStatusAuthorized and settings.alertSetting() == UNNotificationSettingEnabled
            self.sound_available = self.available and settings.soundSetting() == UNNotificationSettingEnabled
            if self.available:
                self.status = tr("可用", "Available") if self.sound_available else tr("可用，系统通知音已关闭", "Available with system notification sounds off")
            elif state == UNAuthorizationStatusDenied:
                self.status = tr("系统通知权限已拒绝", "System notification permission was denied")
            elif state == UNAuthorizationStatusNotDetermined:
                self.status = tr("系统通知尚未授权", "System notification permission has not been granted")
            else:
                self.status = tr("系统已关闭通知横幅", "System notification banners are off")

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

    def submit(self, identifier, due_at, *, sound, complete, snooze=True):
        from UserNotifications import (
            UNMutableNotificationContent, UNNotificationRequest, UNNotificationSound,
            UNTimeIntervalNotificationTrigger,
        )

        if not self.available:
            complete("failed", self.status)
            return
        content = UNMutableNotificationContent.alloc().init()
        title, body = _generic_content()
        content.setTitle_(title)
        content.setBody_(body)
        content.setCategoryIdentifier_(SNOOZE_CATEGORY if snooze else REMINDER_CATEGORY)
        if due_at is not None:
            content.setThreadIdentifier_(notification_id(f"deadline:{due_at.astimezone(timezone.utc).isoformat()}"))
        if sound and self.sound_available:
            content.setSound_(UNNotificationSound.defaultSound())
        trigger = None
        if due_at is not None:
            delay = (due_at - datetime.now(timezone.utc)).total_seconds()
            if delay < 1:
                complete("failed", tr("系统预登记时间已到", "The scheduled notification time has arrived"))
                return
            trigger = UNTimeIntervalNotificationTrigger.triggerWithTimeInterval_repeats_(delay, False)
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(identifier, content, trigger)

        def submitted(error):
            if error is not None:
                complete("failed", tr("系统拒绝通知（{domain} {code}）", "System rejected the notification ({domain} {code})").format(
                    domain=error.domain(), code=error.code(),
                ))
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
    can_respond = False
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
        if self.available:
            self.status = tr("可用（Mochi 运行时可发送）", "Available while Mochi is running")
        else:
            self.status = tr("Windows 通知不可用（{status}）", "Windows notifications are unavailable ({status})").format(
                status=setting.name,
            )

    def submit(self, identifier, due_at, *, sound, complete):
        from winrt.windows.data.xml.dom import XmlDocument
        from winrt.windows.ui.notifications import ToastNotification

        if due_at is not None:
            complete("failed", tr("当前 Windows 桌面通道无法提前登记", "This Windows notification channel cannot schedule notifications"))
            return
        self.refresh()
        if not self.available:
            complete("failed", self.status)
            return
        xml = XmlDocument()
        audio = "" if sound else '<audio silent="true"/>'
        # Only generic constants go to Windows, including when the desktop is locked.
        title, body = _generic_content()
        xml.load_xml(f'<toast><visual><binding template="ToastGeneric"><text>{title}</text>'
                     f'<text>{body}</text></binding></visual>{audio}</toast>')
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
        return UnavailableNotifications(tr("离屏模式仅验证桌宠通道", "Offscreen mode supports only the pet display"))
    backend = {"darwin": MacNotifications, "win32": WindowsNotifications}.get(sys.platform)
    if backend is None:
        return UnavailableNotifications()
    try:
        return backend()
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError) as exc:
        return UnavailableNotifications(str(exc) if isinstance(exc, RuntimeError) else tr("系统通知组件不可用", "System notifications are unavailable"))
