"""Editor shortcuts use physical Windows virtual keys, not IME text.

The local IME guard disassociates a context only while a non-text editor
widget has focus. It restores that context before focus leaves the editor;
it never changes the user's input language, conversion mode, or text widgets.
"""
from __future__ import annotations

import ctypes
import os
import tkinter as tk


_VIRTUAL_KEYS = {
    0x08: 'BackSpace', 0x09: 'Tab', 0x0D: 'Return', 0x1B: 'Escape',
    0x20: 'space', 0x21: 'Prior', 0x22: 'Next', 0x23: 'End', 0x24: 'Home',
    0x25: 'Left', 0x26: 'Up', 0x27: 'Right', 0x28: 'Down', 0x2E: 'Delete',
}


def canonical_key(event, *, windows=None):
    """Return an editor key name even if ``char`` is Chinese or empty.

    Windows Tk ``keycode`` is a virtual-key code. Other platforms and test
    events without a keycode fall back to keysym/char. Editable controls must
    be excluded by the caller before dispatching any shortcut.
    """
    if windows is None:
        windows = os.name == 'nt'
    code = getattr(event, 'keycode', None)
    if windows and type(code) is int:
        if 0x30 <= code <= 0x39 or 0x41 <= code <= 0x5A:
            return chr(code).lower()
        if 0x60 <= code <= 0x69:
            return str(code - 0x60)
        if code in _VIRTUAL_KEYS:
            return _VIRTUAL_KEYS[code]
    symbol = str(getattr(event, 'keysym', '') or '')
    if len(symbol) == 1 and symbol.isascii():
        return symbol.lower()
    if symbol in _VIRTUAL_KEYS.values():
        return symbol
    if symbol.startswith('KP_') and symbol[3:] in '0123456789' and len(symbol) == 4:
        return symbol[3:]
    char = str(getattr(event, 'char', '') or '')
    if len(char) == 1 and char.isascii() and char.isalnum():
        return char.lower()
    return symbol


class EditorIMEGuard:
    """Temporarily remove IME contexts from this editor's focused HWNDs.

    Saving and restoring the previous HIMC follows the window-local IMM API:
    https://learn.microsoft.com/en-us/windows/win32/api/imm/nf-imm-immassociatecontext
    """

    def __init__(self, associate=None, is_window=None):
        self._associate = associate
        self._is_window = is_window
        self._contexts = {}
        self._attempted = set()
        self._unavailable = False

    def _load(self):
        if self._associate is not None:
            return True
        if self._unavailable or os.name != 'nt':
            return False
        try:
            imm = ctypes.WinDLL('imm32', use_last_error=True)
            user = ctypes.WinDLL('user32', use_last_error=True)
            associate = imm.ImmAssociateContext
            associate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            associate.restype = ctypes.c_void_p
            is_window = user.IsWindow
            is_window.argtypes = [ctypes.c_void_p]
            is_window.restype = ctypes.c_int
            self._associate, self._is_window = associate, is_window
            return True
        except (AttributeError, OSError):
            self._unavailable = True
            return False

    def suspend(self, widget):
        if not self._load():
            return
        try:
            handle = int(widget.winfo_id())
            if handle in self._attempted or not self._is_window(handle):
                return
            previous = self._associate(handle, None)
            self._attempted.add(handle)
            if previous:
                self._contexts[handle] = previous
        except (AttributeError, ValueError, OSError, tk.TclError):
            return

    def restore_all(self):
        if self._associate is not None:
            for handle, context in tuple(self._contexts.items()):
                if self._is_window(handle):
                    self._associate(handle, context)
        self._contexts.clear()
        self._attempted.clear()
