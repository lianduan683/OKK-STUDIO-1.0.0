"""Pointer feedback and tab-strip insertion for workspace docking."""
from __future__ import annotations

import tkinter as tk


def tab_insertion(notebook, x, y, px):
    """Return (index, screen rectangle) only over the actual tab strip."""
    left, top = notebook.winfo_rootx(), notebook.winfo_rooty()
    local_x, local_y = x - left, y - top
    width = notebook.winfo_width()
    if not (0 <= local_x < width and 0 <= local_y < px(48)):
        return None

    def at(value):
        try:
            return notebook.index(f'@{value},{local_y}')
        except tk.TclError:
            return None

    # A blank area after the last tab is also an insertion target. Check the
    # same row over the first tab, so notebook content never becomes a tab drop.
    first = next((value for value in range(0, min(width, px(40))) if at(value) is not None), None)
    if first is None:
        return None
    index = at(local_x)
    if index is None:
        index = len(notebook.tabs()) - 1
        lo, hi = first, width
        while lo < hi:
            mid = (lo + hi) // 2
            if at(mid) is None:
                hi = mid
            else:
                lo = mid + 1
        position, insert = lo, index + 1
    else:
        lo, hi = first, local_x
        while lo < hi:
            mid = (lo + hi) // 2
            if at(mid) == index:
                hi = mid
            else:
                lo = mid + 1
        start = lo
        lo, hi = local_x, width
        while lo < hi:
            mid = (lo + hi) // 2
            if at(mid) == index:
                lo = mid + 1
            else:
                hi = mid
        end = lo
        before = local_x < (start + end) / 2
        position, insert = (start, index) if before else (end, index + 1)
    height = 1
    while height < min(notebook.winfo_height(), px(48)):
        try:
            notebook.index(f'@{first},{height}')
        except tk.TclError:
            if height > local_y:
                break
        height += 1
    return insert, (left + min(width - px(3), position), top + 2, px(3), max(px(18), height - 2))


class DragPreview:
    def __init__(self, workspace, px, title, size, whole_group=False):
        self.window = tk.Toplevel(workspace, takefocus=False)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-topmost', True)
        self.window.configure(bg='#58a6d6', bd=1)
        try:
            self.window.attributes('-alpha', .82)
        except tk.TclError:
            pass
        self.px, self.size = px, size
        body = tk.Frame(self.window, bg='#24262a')
        body.pack(fill='both', expand=True, padx=1, pady=1)
        tk.Label(body, text=('面板组 · ' if whole_group else '') + title,
                 bg='#343940', fg='#f1f3f5', anchor='w', padx=px(10), pady=px(7)).pack(fill='x')
        tk.Label(body, text='释放以浮动\n拖到面板边缘停靠 · 中间合并标签\nEsc 取消',
                 bg='#24262a', fg='#b9c0ca', justify='center').pack(expand=True)

    def move(self, x, y, floating):
        if not floating:
            self.window.withdraw()
            return
        # Signed coordinates after an explicit '+' are absolute screen pixels
        # in Tk, including monitors to the left of the primary display.
        width, height = self.size
        self.window.geometry(f'{width}x{height}+{x - self.px(30)}+{y - self.px(12)}')
        self.window.deiconify()

    def destroy(self):
        self.window.destroy()
