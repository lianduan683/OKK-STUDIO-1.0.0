"""Read native caption gestures; perform docking only on Tk's UI thread."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from queue import Empty, SimpleQueue
from threading import Event, Thread
from types import SimpleNamespace
import tkinter as tk


class NativeDockDrag:
    def __init__(self, manager):
        self.manager = manager
        self.windows = ()
        self.events = SimpleQueue()
        self.stop = Event()
        self.generation = 0
        self.after = None
        self.api = C.WinDLL('user32', use_last_error=True)
        self.api.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.api.GetCursorPos.argtypes = [C.POINTER(W.POINT)]
        self.api.GetAsyncKeyState.argtypes = [C.c_int]
        self.api.GetAsyncKeyState.restype = C.c_short
        self.api.WindowFromPoint.argtypes = [W.POINT]
        self.api.WindowFromPoint.restype = W.HWND
        self.api.GetAncestor.argtypes = [W.HWND, W.UINT]
        self.api.GetAncestor.restype = W.HWND
        self.api.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM,
                                                W.UINT, W.UINT, C.POINTER(C.c_size_t)]
        self.api.SendMessageTimeoutW.restype = C.c_ssize_t
        self.thread = Thread(target=self._observe, name='OKK caption drag', daemon=True)
        self.thread.start()

    def sync(self, windows):
        current = tuple((key, whole, int(window.tk.call('wm', 'frame', window), 0))
                        for key, whole, window in windows)
        if current != self.windows:
            self.generation += 1
            self.windows = current
        if current and self.after is None:
            self.after = self.manager.workspace.after(20, self._drain)

    def _caption(self, windows, point):
        hwnd = self.api.GetAncestor(self.api.WindowFromPoint(point), 2)
        record = next((record for record in windows if record[2] == hwnd), None)
        if record is None:
            return None
        result = C.c_size_t()
        position = (point.x & 0xffff) | ((point.y & 0xffff) << 16)
        # HTCAPTION only: resizing, caption buttons and content retain their
        # normal actions. The timeout bounds observer shutdown as well.
        if (self.api.SendMessageTimeoutW(hwnd, 0x0084, 0, position, 2, 40, C.byref(result))
                and result.value == 2):
            return record
        return None

    def _rectangle(self, hwnd):
        rect = W.RECT()
        if self.api.GetWindowRect(hwnd, C.byref(rect)):
            return rect.left, rect.top, rect.right, rect.bottom
        return None

    def _observe(self):
        down = False
        active = None
        while not self.stop.wait(.016):
            if not self.windows and active is None:
                self.stop.wait(.1)
                down = False
                continue
            pressed = bool(self.api.GetAsyncKeyState(1) & 0x8000)
            point = W.POINT()
            if not self.api.GetCursorPos(C.byref(point)):
                continue
            if pressed and not down:
                generation = self.generation
                record = self._caption(self.windows, point)
                if record:
                    active = dict(record=record, generation=generation,
                                  rectangle=self._rectangle(record[2]), moved=False, cancelled=False)
            if active:
                key, whole, hwnd = active['record']
                args = (active['generation'], key, whole, point.x, point.y)
                if active['generation'] != self.generation or self.api.GetAsyncKeyState(27) & 0x8000:
                    if not active['cancelled']:
                        self.events.put(('cancel', *args))
                    active['cancelled'] = True
                rect = self._rectangle(hwnd)
                if not active['cancelled'] and rect and rect != active['rectangle']:
                    previous = active['rectangle']
                    translated = previous and (rect[2]-rect[0],rect[3]-rect[1]) == (previous[2]-previous[0],previous[3]-previous[1])
                    active['rectangle'] = rect
                    # A caption double-click maximizes the window, but never
                    # starts a docking gesture. Dragging a maximized window
                    # may restore its size before its first translation.
                    if translated or active['moved']:
                        active['moved'] = True
                        self.events.put(('motion', *args))
                if not pressed:
                    if active['moved'] and not active['cancelled']:
                        self.events.put(('release', *args))
                    active = None
            down = pressed

    def _drain(self):
        self.after = None
        manager = self.manager
        try:
            while True:
                phase, generation, key, whole, x, y = self.events.get_nowait()
                if generation != self.generation or key not in manager.panels:
                    continue
                if phase == 'cancel':
                    if manager._drag and manager._drag.get('native'):
                        manager._cancel_drag()
                    continue
                if manager._closed or manager.fullscreen.active is not None:
                    continue
                if manager._drag is not None and not manager._drag.get('native'):
                    continue
                if manager._drag is None:
                    manager._drag = dict(key=key, whole_group=whole, native=True,
                                         widget=manager.workspace, x=x, y=y, started=True)
                if phase == 'motion':
                    manager._show_hint(manager._drag_target(x, y, key))
                else:
                    manager._release(SimpleNamespace(x_root=x, y_root=y))
        except Empty:
            pass
        if not self.stop.is_set() and self.windows and self.after is None:
            self.after = manager.workspace.after(20, self._drain)

    def close(self):
        self.stop.set()
        self.windows = ()
        if self.after is not None:
            try:
                self.manager.workspace.after_cancel(self.after)
            except tk.TclError:
                pass
            self.after = None
        # This reader owns no Tk widgets and never calls Tk from its thread.
        self.thread.join(timeout=.15)
