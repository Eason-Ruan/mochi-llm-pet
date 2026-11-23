"""Native suspend and resume events for the desktop session."""
from __future__ import annotations

from PyQt6.QtCore import QAbstractNativeEventFilter
from PyQt6.QtGui import QGuiApplication


class DesktopPower:
    def __init__(self, app, window, dispatch):
        self.app = app
        self.filter = None
        self.registration = None
        self.notification_center = None
        self.observers = []
        platform = QGuiApplication.platformName()
        if platform == "cocoa":
            import AppKit

            self.notification_center = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
            for notification, kind in ((AppKit.NSWorkspaceWillSleepNotification, "system_sleep"),
                                       (AppKit.NSWorkspaceDidWakeNotification, "system_wake")):
                def handler(_notice, event=kind):
                    dispatch({"kind": event})

                self.observers.append(self.notification_center.addObserverForName_object_queue_usingBlock_(
                    notification, None, None, handler,
                ))
        elif platform == "windows":
            import ctypes

            user32 = ctypes.windll.user32
            register = user32.RegisterSuspendResumeNotification
            register.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
            register.restype = ctypes.c_void_p
            self.registration = register(int(window.winId()), 0)
            self.filter = _WindowsPowerFilter(dispatch)
            app.installNativeEventFilter(self.filter)

    def close(self):
        for observer in self.observers:
            self.notification_center.removeObserver_(observer)
        self.observers.clear()
        if self.filter is not None:
            self.app.removeNativeEventFilter(self.filter)
            self.filter = None
        if self.registration:
            import ctypes

            unregister = ctypes.windll.user32.UnregisterSuspendResumeNotification
            unregister.argtypes = (ctypes.c_void_p,)
            unregister(self.registration)
            self.registration = None


class _WindowsPowerFilter(QAbstractNativeEventFilter):
    def __init__(self, dispatch):
        super().__init__()
        self.dispatch = dispatch

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            from ctypes.wintypes import MSG

            event = MSG.from_address(int(message))
            if event.message == 0x0218:  # WM_POWERBROADCAST
                if event.wParam == 0x0004:  # PBT_APMSUSPEND
                    self.dispatch({"kind": "system_sleep"})
                elif event.wParam == 0x0012:  # PBT_APMRESUMEAUTOMATIC
                    self.dispatch({"kind": "system_wake"})
        return False, 0
