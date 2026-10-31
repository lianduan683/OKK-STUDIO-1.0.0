"""Keep Tk fullscreen on the display containing the original video panel."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W

from tk_native_calls import TkNativeCalls


class _MonitorInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('monitor', W.RECT),
                ('work', W.RECT), ('flags', W.DWORD)]


class _WindowPlacement(C.Structure):
    _fields_ = [('length', W.UINT), ('flags', W.UINT), ('show', W.UINT),
                ('minimum', W.POINT), ('maximum', W.POINT), ('normal', W.RECT)]


class WindowsMonitorFullscreen:
    def __init__(self, panel):
        self.api = C.WinDLL('user32', use_last_error=True)
        self.calls = TkNativeCalls.for_widget(panel)
        self.api.MonitorFromWindow.argtypes = [W.HWND, W.DWORD]
        self.api.MonitorFromWindow.restype = W.HANDLE
        self.api.GetMonitorInfoW.argtypes = [W.HANDLE, C.POINTER(_MonitorInfo)]
        self.api.GetWindowPlacement.argtypes = [W.HWND, C.POINTER(_WindowPlacement)]
        self.api.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.api.SetWindowPlacement.argtypes = [W.HWND, C.POINTER(_WindowPlacement)]
        self.api.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int,
                                          C.c_int, C.c_int, W.UINT]
        info = _MonitorInfo()
        info.size = C.sizeof(info)
        # Query the live panel before wm manage can move a docked panel.
        monitor = self.api.MonitorFromWindow(panel.winfo_id(), 2)  # DEFAULTTONEAREST
        if not monitor or not self.api.GetMonitorInfoW(monitor, C.byref(info)):
            raise C.WinError(C.get_last_error())
        self.bounds = info.monitor

    def capture(self, window):
        self.window = window
        self.placement = _WindowPlacement()
        self.placement.length = C.sizeof(self.placement)
        if not self.api.GetWindowPlacement(self.hwnd(), C.byref(self.placement)):
            raise C.WinError(C.get_last_error())
        self.rectangle = W.RECT()
        if not self.api.GetWindowRect(self.hwnd(), C.byref(self.rectangle)):
            raise C.WinError(C.get_last_error())

    def hwnd(self):
        # Tk replaces the outer wrapper when changing fullscreen attributes;
        # the video children remain intact. Always resolve the current wrapper.
        return int(self.window.tk.call('wm', 'frame', self.window), 0)

    def cover(self):
        self.window.update_idletasks()
        # Tk 8.6 fullscreen uses the primary screen. Keep its chrome/state
        # handling, then place that same native window on the chosen monitor.
        self.position(self.bounds, 0x0024)  # FRAMECHANGED | NOZORDER

    def position(self, rect, flags):
        if not self.calls.call(self.api.SetWindowPos, self.hwnd(), None,
                               rect.left, rect.top, rect.right - rect.left,
                               rect.bottom - rect.top, flags):
            raise C.WinError(C.get_last_error())

    def restore(self):
        self.window.update_idletasks()
        # Preserve the normal restore rectangle as well as a maximized state;
        # restoring only Tk's geometry loses that rectangle on secondary screens.
        if self.placement.show == 3:  # SW_SHOWMAXIMIZED
            if not self.calls.call(self.api.SetWindowPlacement, self.hwnd(), C.byref(self.placement)):
                raise C.WinError(C.get_last_error())
        else:
            # SetWindowPlacement clamps intentionally offscreen windows and
            # uses work-area coordinates. Restore normal windows in screen pixels.
            self.position(self.rectangle, 0x0014)  # NOACTIVATE | NOZORDER
