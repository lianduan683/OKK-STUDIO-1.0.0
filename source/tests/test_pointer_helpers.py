"""Real desktop hit tests with reachable, opaque test windows."""
import ctypes as C
from ctypes import wintypes as W
import os
import tkinter as tk
import unittest


def pointer_hit(widget, windows):
    """Expose the actual host briefly; keep geometry/opacity intact afterwards.

    WindowFromPoint cannot hit windows at +10000+10000 or alpha=0. Tests
    should run on a private Windows desktop so exposing them stays invisible.
    The assertion still uses Tk's real hit test and detects covering siblings.
    """
    records = []
    target = None
    api = C.WinDLL('user32', use_last_error=True) if os.name == 'nt' else None
    if api is not None:
        api.GetAncestor.argtypes = [W.HWND, W.UINT]
        api.GetAncestor.restype = W.HWND
        ancestor = api.GetAncestor(widget.winfo_id(), 2)
    try:
        for window in dict.fromkeys(windows):
            state = str(window.tk.call('wm', 'state', window))
            if state == 'withdrawn':continue
            geometry = str(window.tk.call('wm', 'geometry', window))
            opacity = window.tk.call('wm', 'attributes', window, '-alpha')
            records.append((window, state, geometry, opacity))
            window.tk.call('wm', 'attributes', window, '-alpha', 1)
            window.tk.call('wm', 'geometry', window, '+80+80')
            if api is None or int(window.tk.call('wm', 'frame', window), 0) == ancestor:
                target = window
        if target is not None:target.lift()
        widget.update()
        if api is not None:
            # Grouped panels keep their Tk owner while their native wrapper
            # is reparented. Tk's logical root coordinates are not the native
            # child's physical desktop coordinates after SetParent.
            api.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
            rect = W.RECT()
            if not api.GetWindowRect(widget.winfo_id(), C.byref(rect)):
                raise C.WinError(C.get_last_error())
            x, y = (rect.left+rect.right)//2, (rect.top+rect.bottom)//2
        else:
            x = widget.winfo_rootx() + max(1, widget.winfo_width() // 2)
            y = widget.winfo_rooty() + max(1, widget.winfo_height() // 2)
        hit = widget.winfo_containing(x, y)
        return hit
    finally:
        for window, state, geometry, opacity in reversed(records):
            window.tk.call('wm', 'geometry', window, geometry)
            window.tk.call('wm', 'state', window, state)
            window.tk.call('wm', 'attributes', window, '-alpha', opacity)
        widget.update_idletasks()


class PointerHelperTests(unittest.TestCase):
    def test_covering_sibling_is_detected_and_window_geometry_is_restored(self):
        root = tk.Tk()
        root.attributes('-alpha', 0)
        root.geometry('500x300+10000+10000')
        try:
            canvas = tk.Canvas(root)
            canvas.pack(fill='both', expand=True)
            root.update()
            geometry = root.geometry()
            self.assertIs(pointer_hit(canvas, [root]), canvas)
            cover = tk.Frame(root, bg='red')
            cover.place(relwidth=1, relheight=1)
            cover.lift()
            root.update()
            self.assertIs(pointer_hit(canvas, [root]), cover)
            self.assertEqual(root.geometry(), geometry)
            self.assertEqual(float(root.attributes('-alpha')), 0.)
        finally:root.destroy()
