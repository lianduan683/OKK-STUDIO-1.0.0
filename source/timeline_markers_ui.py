"""Undoable frame-aligned timeline markers independent of source clip edits."""
from __future__ import annotations

import math
import tkinter as tk

from editor_core import format_time


MARKER_COLOR = '#edc36b'


class TimelineMarkersMixin:
    def _init_timeline_markers(self):
        self.timeline_marker_items = {}
        self._timeline_marker_key_down = False
        self.timeline_marker_menu = None

    def _marker_key_press(self):
        if not self._timeline_marker_key_down:
            self._timeline_marker_key_down = True
            self.add_timeline_marker()
        return 'break'

    def _marker_key_release(self):
        self._timeline_marker_key_down = False

    def add_timeline_marker(self, position=None):
        if not self._available() or self.project.duration <= 0:
            return False
        position = self.current if position is None else position
        if not isinstance(position, (int, float)) or not math.isfinite(position):
            return False
        fps = self.project.fps
        position = max(0., min(self.project.duration, float(position)))
        frame = math.floor(position * fps + .5)
        value = frame / fps
        markers = self.project.timeline_markers
        if any(math.floor(point * fps + .5) == frame for point in markers):
            self.status.set(f'此处已有标记：{format_time(value)}')
            return False
        self._remember()
        markers.append(value)
        markers.sort()
        self.draw_timeline()
        self.status.set(f'已添加时间标记：{format_time(value)} · M 标记，右键标记可删除')
        return True

    def remove_timeline_marker(self, position):
        if not self._available():
            return False
        fps = self.project.fps
        frame = math.floor(position * fps + .5)
        remaining = [value for value in self.project.timeline_markers
                     if math.floor(value * fps + .5) != frame]
        if len(remaining) == len(self.project.timeline_markers):
            return False
        self._remember()
        self.project.timeline_markers = remaining
        self.draw_timeline()
        self.status.set(f'已删除时间标记：{format_time(position)} · Ctrl+Z 撤销')
        return True

    def _timeline_marker_extent(self):
        return max(self.project.timeline_markers, default=0.)

    def _draw_timeline_markers(self, ruler_h, content_height, font):
        """Call after ruler ticks and before draw_playhead in draw_timeline."""
        canvas = self.timeline
        self.timeline_marker_items = {}
        self._timeline_marker_ruler_h = ruler_h
        left, right = canvas.canvasx(0), canvas.canvasx(canvas.winfo_width())
        for index, point in enumerate(self.project.timeline_markers, 1):
            x = self._timeline_x(point)
            if x < left - self._px(10) or x > right + self._px(10):
                continue
            # Only the ruler flag is clickable; the guide must not steal
            # selection, trimming or dragging from source/programme clips.
            canvas.create_line(x, self._px(11), x, content_height,
                               fill='#9b824e', dash=(self._px(2), self._px(4)),
                               tags=('timeline-marker-guide',))
            size = self._px(5)
            top = self._px(1)
            flag = canvas.create_polygon(x-size, top, x+size, top,
                                         x+size, top+size, x, top+2*size,
                                         x-size, top+size, fill=MARKER_COLOR, outline='#f5dda9',
                                         tags=('timeline-marker',))
            self.timeline_marker_items[flag] = point
            # A compact label remains distinguishable from the time ruler.
            if index == 1 or x - self._timeline_x(self.project.timeline_markers[index-2]) > self._px(90):
                label = canvas.create_text(x + size + self._px(3), top + size,
                    text=f'M{index}', fill=MARKER_COLOR, anchor='w', font=font,
                    tags=('timeline-marker',))
                self.timeline_marker_items[label] = point

    def _timeline_marker_at(self, event):
        canvas = self.timeline
        x, y = canvas.canvasx(event.x), canvas.canvasy(event.y)
        if y < 0 or y >= getattr(self, '_timeline_marker_ruler_h', 0):
            return None
        for item in reversed(canvas.find_overlapping(x-self._px(2), y-self._px(2),
                                                     x+self._px(2), y+self._px(2))):
            if item in self.timeline_marker_items:
                return self.timeline_marker_items[item]
        return None

    def _timeline_marker_press(self, event):
        position = self._timeline_marker_at(event)
        if position is None:
            return False
        if self._available():
            self.timeline.focus_set()
            self.seek(min(self.project.duration, position))
            self.status.set(f'时间标记：{format_time(position)}')
        return True

    def _timeline_marker_context(self, event):
        position = self._timeline_marker_at(event)
        canvas_y = self.timeline.canvasy(event.y)
        if position is None and not 0 <= canvas_y < getattr(self, '_timeline_marker_ruler_h', 0):
            return False
        if not self._available():
            return True
        self.timeline.focus_set()
        if position is None:
            position = self._timeline_origin() + (
                self.timeline.canvasx(event.x)-self._timeline_left)/self._timeline_scale()
            action = lambda: self.add_timeline_marker(position)
            label = '在此处添加时间标记'
        else:
            action = lambda: self.remove_timeline_marker(position)
            label = '删除时间标记 · ' + format_time(position)
        if self.timeline_marker_menu is not None:
            self.timeline_marker_menu.destroy()
        menu = self.timeline_marker_menu = tk.Menu(self, tearoff=False, bg='#292b2e',
                                                    fg='#dedfe3', activebackground='#3c5263')
        menu.add_command(label=label, command=action)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return True
