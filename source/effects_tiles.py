"""Grouped, keyboard accessible effect cards with bounded lazy mask icons."""
from __future__ import annotations

from collections import OrderedDict
import io
import math
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image

from effects_library import transition_mask_path

BG, CARD, EDGE, TEXT, MUTED, ACCENT = '#25262b', '#2d2e34', '#15161a', '#dedfe3', '#92959d', '#ea705c'


def section_name(item):
    if item.kind == 'transition':
        return '视频转场 / ' + ('叠化' if item.action in ('fade', 'dissolve', 'fadeblack', 'fadewhite') else '运动与形状')
    if item.kind == 'mask':
        return '视频转场 / ' + item.category.split(' / ')[-1]
    return item.category


class EffectTiles(tk.Canvas):
    def __init__(self, parent, *, scale, **kwargs):
        super().__init__(parent, bg=BG, highlightthickness=0, width=1, height=1,
                         takefocus=True, yscrollincrement=scale(42), **kwargs)
        self.px = scale
        self._items = ()
        self._by_key = {}
        self._selected = ''
        self._hovered = ''
        self._boxes = {}
        self._sections = []
        self._total_height = 1
        self._columns = 1
        self._draw_after = None
        self._draw_timer = None
        self._images = OrderedDict()
        self._pending = set()
        self._failed = set()
        self._requests = queue.Queue(maxsize=24)
        self._results = queue.Queue()
        self._closed = threading.Event()
        self._worker = None
        self._icon_after = self.after(100, self._collect_icons)
        self._font = tkfont.Font(self, family='Microsoft YaHei UI', size=9)
        self._heading_font = tkfont.Font(self, family='Microsoft YaHei UI', size=9)
        self.bind('<Configure>', self._layout)
        self.bind('<ButtonPress-1>', self._press)
        self.bind('<Motion>', self._hover)
        self.bind('<Leave>', self._leave)
        self.bind('<MouseWheel>', self._wheel)
        self.bind('<KeyPress>', self._key)

    def set_items(self, items):
        self._items = tuple(items)
        self._by_key = {item.key: item for item in items}
        if self._selected not in self._by_key:
            self._selected = ''
        self.yview_moveto(0)
        self._layout()

    def get_children(self):
        return tuple(item.key for item in self._items)

    def selection(self):
        return (self._selected,) if self._selected in self._by_key else ()

    def selection_set(self, key):
        if isinstance(key, (list, tuple)):
            key = key[0] if key else ''
        key = key if key in self._by_key else ''
        if key != self._selected:
            self._selected = key
            self._schedule_draw()
            self.event_generate('<<TreeviewSelect>>', when='tail')

    def item_at(self, x, y):
        x, y = self.canvasx(x), self.canvasy(y)
        return next((key for key, (left, top, width, height) in self._boxes.items()
                     if left <= x < left + width and top <= y < top + height), '')

    def bbox(self, key):
        box = self._boxes.get(key)
        if box is None:
            return ()
        x, y, width, height = box
        y -= self.canvasy(0)
        if y + height <= 0 or y >= self.winfo_height():
            return ()
        return (x, y, width, height)

    def see(self, key):
        box = self._boxes.get(key)
        if box is None:
            return
        _, y, _, height = box
        top, viewport = self.canvasy(0), self.winfo_height()
        if y < top:
            self.yview_moveto(y / self._total_height)
        elif y + height > top + viewport:
            self.yview_moveto((y + height - viewport) / self._total_height)
        self._schedule_draw()

    def yview(self, *args):
        result = super().yview(*args)
        if args:
            self._schedule_draw()
        return result

    def yview_moveto(self, fraction):
        super().yview_moveto(fraction)
        self._schedule_draw()

    def _layout(self, event=None):
        width = max(1, self.winfo_width())
        gap, margin, row = self.px(8), self.px(8), self.px(42)
        previous_columns=self._columns
        self._columns = max(1, min(3, (width - margin * 2 + gap) // self.px(204)))
        card_width = max(1, (width - 2 * margin - gap * (self._columns - 1)) / self._columns)
        groups = OrderedDict()
        for item in self._items:
            groups.setdefault(section_name(item), []).append(item)
        self._boxes, self._sections = {}, []
        y = self.px(8)
        for name, items in groups.items():
            self._sections.append((name, len(items), y))
            y += self.px(30)
            for index, item in enumerate(items):
                self._boxes[item.key] = (margin + (index % self._columns) * (card_width + gap),
                                        y + (index // self._columns) * row, card_width, self.px(35))
            y += math.ceil(len(items) / self._columns) * row + self.px(14)
        self._total_height = max(1, y)
        self.configure(scrollregion=(0, 0, width, self._total_height))
        if previous_columns!=self._columns and self._selected:
            self.see(self._selected)
        self._schedule_draw()

    def _schedule_draw(self):
        if self._draw_after is None and not self._closed.is_set():
            self._draw_after = self.after_idle(self._draw)
            # Give drawing a deadline while live resize keeps idle work waiting.
            self._draw_timer = self.after(16, self._draw)

    def _draw(self):
        for pending in (self._draw_after, self._draw_timer):
            if pending is not None:
                self.after_cancel(pending)
        self._draw_after = None
        self._draw_timer = None
        super().delete('all')
        top, bottom = self.canvasy(0) - self.px(50), self.canvasy(0) + self.winfo_height() + self.px(50)
        width = self.winfo_width()
        for name, count, y in self._sections:
            if top - self.px(30) <= y <= bottom:
                label = name.split(' / ')[-1]
                self.create_text(self.px(9), y + self.px(9), text=label, anchor='w',
                                 fill=MUTED, font=self._heading_font)
                self.create_text(width - self.px(10), y + self.px(9), text=str(count),
                                 anchor='e', fill='#6d717a', font=self._heading_font)
                self.create_line(self.px(9), y + self.px(23), width - self.px(9), y + self.px(23), fill='#36383f')
        for item in self._items:
            x, y, w, h = self._boxes[item.key]
            if y + h < top or y > bottom:
                continue
            selected, hovered = item.key == self._selected, item.key == self._hovered
            self.create_rectangle(x, y, x + w, y + h, fill='#35363e' if hovered or selected else CARD,
                                  outline=ACCENT if selected else '#656973' if hovered else EDGE,
                                  width=self.px(2) if selected else 1)
            icon_width = min(self.px(58), w * .28)
            self.create_line(x + icon_width, y + 1, x + icon_width, y + h - 1, fill=EDGE)
            self._draw_icon(item, x + self.px(3), y + self.px(3), icon_width - self.px(6), h - self.px(6))
            label, available = item.name, max(1, w - icon_width - self.px(16))
            while label and self._font.measure(label) > available:
                label = label[:-2].rstrip('…') + '…' if len(label) > 2 else ''
            self.create_text(x + icon_width + self.px(9), y + h / 2, text=label, anchor='w',
                             fill=TEXT if selected or hovered else '#b2b5be', font=self._font)
        if not self._items:
            self.create_text(width / 2, self.px(60), text='没有匹配的效果\n请更换分类或搜索词',
                             justify='center', fill=MUTED, font=self._font)

    def _draw_icon(self, item, x, y, w, h):
        image = self._images.get(item.key)
        if image is not None:
            self._images.move_to_end(item.key)
            self.create_image(x + w / 2, y + h / 2, image=image)
            return
        color, dark = '#828691', '#454851'
        if item.kind == 'mask':
            self.create_polygon(x, y + h, x + w, y, x + w, y + h, fill=color)
            self.create_polygon(x, y, x + w, y, x, y + h, fill=dark)
            if item.key not in self._pending and item.key not in self._failed:
                try:
                    path = transition_mask_path(item.asset)
                    self._requests.put_nowait((item.key, path, (max(1, round(w)), max(1, round(h)))))
                    self._pending.add(item.key)
                    if self._worker is None:
                        self._worker = threading.Thread(target=self._icon_worker, name='effect-card-icons', daemon=True)
                        self._worker.start()
                except (ValueError, queue.Full):
                    pass
        elif item.kind == 'transition':
            action = item.action
            if action == 'circleopen':
                self.create_oval(x + w * .24, y + h * .1, x + w * .76, y + h * .9, fill=color, outline='')
            elif action in ('fade', 'dissolve', 'fadeblack', 'fadewhite'):
                for index in range(8):
                    shade = 65 + index * 13
                    self.create_rectangle(x + index * w / 8, y + 2, x + (index + 1) * w / 8, y + h - 2,
                                          fill=f'#{shade:02x}{shade:02x}{shade:02x}', outline='')
                self.create_line(x + 2, y + h - 3, x + w - 2, y + 3, fill='#c1c4cc')
                self.create_line(x + 2, y + 3, x + w - 2, y + h - 3, fill='#c1c4cc')
            else:
                self.create_rectangle(x + w * .2, y + 3, x + w * .55, y + h - 3, fill=dark, outline=color)
                left = action.endswith('left')
                self.create_line(x + w * (.85 if left else .15), y + h / 2,
                                 x + w * (.18 if left else .82), y + h / 2,
                                 fill='#b8bdc9', arrow='last', width=self.px(2))
        elif item.kind == 'overlay':
            for offset in (0, 5, 10):
                self.create_rectangle(x + w * .18 + self.px(offset), y + h * .12 + self.px(offset / 2),
                                      x + w * .63 + self.px(offset), y + h * .68 + self.px(offset / 2), outline=color)
        elif item.kind in ('grade', 'reset'):
            colors = ('#b36d61', '#7a9f8e', '#6389b4') if item.kind == 'grade' else (color,) * 3
            for index, fill in enumerate(colors):
                cx = x + w * (.29 + index * .22)
                self.create_oval(cx - h * .24, y + h * .26, cx + h * .24, y + h * .74,
                                 fill=fill, outline=EDGE)
        else:
            self.create_text(x + w / 2, y + h / 2, text='T' if item.action == 'subtitle' else '✥',
                             fill=color, font=('Segoe UI', 16))

    def _icon_worker(self):
        while not self._closed.is_set():
            try:
                key, path, size = self._requests.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                with Image.open(path) as picture:
                    if picture.mode in ('I', 'I;16'):
                        picture = picture.convert('I').point(lambda value: value / 257).convert('L')
                    else:
                        picture = picture.convert('L')
                    picture.thumbnail(size, Image.Resampling.LANCZOS)
                    low,high=picture.getextrema()
                    threshold=(low+high)/2
                    picture = picture.point(lambda value: 145 if value < threshold else 60).convert('RGB')
                    data = io.BytesIO()
                    picture.save(data, format='PNG')
                    self._results.put((key, data.getvalue()))
            except (OSError, ValueError):
                self._results.put((key, None))

    def _collect_icons(self):
        self._icon_after = None
        changed = False
        while True:
            try:
                key, data = self._results.get_nowait()
            except queue.Empty:
                break
            self._pending.discard(key)
            if data is None:
                self._failed.add(key)
                continue
            self._images[key] = tk.PhotoImage(master=self, data=data)
            while len(self._images) > 96:
                self._images.popitem(last=False)
            changed = True
        if changed:
            self._schedule_draw()
        if not self._closed.is_set():
            self._icon_after = self.after(100, self._collect_icons)

    def _press(self, event):
        self.focus_set()
        key = self.item_at(event.x, event.y)
        if key:
            self.selection_set(key)

    def _hover(self, event):
        key = self.item_at(event.x, event.y)
        if key != self._hovered:
            self._hovered = key
            self._schedule_draw()

    def _leave(self, event):
        self._hovered = ''
        self._schedule_draw()

    def _wheel(self, event):
        steps=max(1,round(abs(event.delta)/120)) if event.delta else 0
        super().yview_scroll((-1 if event.delta>0 else 1)*steps*2, 'units')
        self._schedule_draw()
        return 'break'

    def _key(self, event):
        keys = self.get_children()
        if not keys or event.keysym not in ('Up', 'Down', 'Left', 'Right', 'Home', 'End', 'Return', 'space'):
            return
        if event.keysym in ('Return', 'space'):
            self.event_generate('<<ApplyEffect>>')
            return 'break'
        index = keys.index(self._selected) if self._selected in keys else 0
        offset = {'Up': -self._columns, 'Down': self._columns, 'Left': -1, 'Right': 1}.get(event.keysym, 0)
        index = 0 if event.keysym == 'Home' else len(keys) - 1 if event.keysym == 'End' else min(len(keys) - 1, max(0, index + offset))
        self.selection_set(keys[index])
        self.see(keys[index])
        return 'break'

    def destroy(self):
        self._closed.set()
        for pending in (self._draw_after, self._draw_timer, self._icon_after):
            if pending is not None:
                self.after_cancel(pending)
        self._images.clear()
        super().destroy()
