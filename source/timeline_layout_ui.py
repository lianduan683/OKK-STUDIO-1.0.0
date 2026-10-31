"""Stable track sizes, view-only folding, and draggable timeline dividers."""
from __future__ import annotations
import math
import tkinter as tk
from tkinter import ttk


class TimelineLayoutMixin:
    def _init_timeline_layout(self):
        self._timeline_video_height = 32.
        self._timeline_audio_height = 48.
        self._timeline_video_height_var = tk.DoubleVar(self, value=32.)
        self._timeline_audio_height_var = tk.DoubleVar(self, value=48.)
        self._timeline_pane_drag = None

    def _build_timeline_size_control(self, parent):
        menu = tk.Menu(parent, tearoff=False)
        for kind, title, choices in (
                ('video', '视频轨道高度', (('紧凑', 24), ('标准', 32), ('中', 48), ('大', 64), ('加大', 96))),
                ('audio', '音频轨道高度', (('紧凑', 32), ('标准', 48), ('中', 64), ('大', 96), ('加大', 128)))):
            submenu = tk.Menu(menu, tearoff=False)
            for label, height in choices:
                submenu.add_radiobutton(label=label, value=height,
                    variable=getattr(self, '_timeline_'+kind+'_height_var'),
                    command=lambda k=kind, h=height: self._set_timeline_track_height(k, h))
            menu.add_cascade(label=title, menu=submenu)
        menu.add_separator()
        menu.add_command(label='恢复标准轨道高度', command=self._reset_timeline_track_heights)
        self.timeline_size_button = ttk.Menubutton(parent, text='轨道大小 ▾', menu=menu, style='Compact.TMenubutton')
        self.timeline_size_button.pack(side='left', padx=self._px(4))

    def _set_timeline_track_height(self, kind, height, *, persist=True):
        if (kind not in ('video', 'audio') or isinstance(height, bool)
                or not isinstance(height, (int, float)) or not math.isfinite(height)
                or not 16 <= height <= 256):
            return False
        setattr(self, '_timeline_'+kind+'_height', float(height))
        getattr(self, '_timeline_'+kind+'_height_var').set(height)
        if hasattr(self, 'timeline'): self._queue_timeline_redraw()
        if persist: self._workspace_layout_changed()
        return True

    def _reset_timeline_track_heights(self):
        self._set_timeline_track_height('video', 32., persist=False)
        self._set_timeline_track_height('audio', 48.)

    def _build_timeline_resize_grip(self, parent):
        grip = self.timeline_resize_grip = tk.Frame(parent, height=self._px(9), bg='#383d43',
                                                    cursor='sb_v_double_arrow', takefocus=True)
        grip.grid(row=0, column=0, columnspan=2, sticky='ew')
        mark = tk.Frame(grip, bg='#a1b0bb', height=max(1, self._px(2)), width=self._px(40),
                        cursor='sb_v_double_arrow')
        mark.place(relx=.5, rely=.5, anchor='center')
        for widget in (grip, mark):
            widget.bind('<ButtonPress-1>', self._begin_timeline_pane_resize)
            widget.bind('<B1-Motion>', self._move_timeline_pane_resize)
            widget.bind('<ButtonRelease-1>', self._end_timeline_pane_resize)
        grip.bind('<Escape>', lambda _event: self._end_timeline_pane_resize(cancel=True))

    def _begin_timeline_pane_resize(self, event):
        if len(self.workspace_split.panes()) < 2: return 'break'
        self.timeline_resize_grip.focus_set()
        self._timeline_pane_drag = (event.y_root, self.workspace_split.sashpos(0))
        self.status.set('上下拖动调整整个时间线高度；轨道保持当前大小。')
        return 'break'

    def _move_timeline_pane_resize(self, event):
        drag = self._timeline_pane_drag
        if drag is None: return 'break'
        low = self._px(130)
        high = max(low, self.workspace_split.winfo_height()-self._px(100))
        self.workspace_split.sashpos(0, max(low, min(high, drag[1]+event.y_root-drag[0])))
        return 'break'

    def _end_timeline_pane_resize(self, _event=None, *, cancel=False):
        drag, self._timeline_pane_drag = self._timeline_pane_drag, None
        if drag is not None:
            if cancel: self.workspace_split.sashpos(0, drag[1])
            else: self._workspace_layout_changed()
        return 'break'

    def _draw_audio_fold_icon(self, x, y, folded, tag):
        size = self._px(4)
        points = ((x-size/2, y-size, x+size/2, y, x-size/2, y+size) if folded else
                  (x-size, y-size/2, x+size, y-size/2, x, y+size/2))
        self.timeline.create_polygon(*points, fill='#a7c6b8', outline='', tags=(tag,))

    def _audio_layout_key(self):
        return (bool(getattr(self, '_camera_audio_collapsed', False)),
                tuple(sorted(getattr(self, '_collapsed_audio_tracks', set()))))

    def _timeline_track_layout(self, cameras, height, track_top, minimum_row, audio_h):
        tracks = self._timeline_audio_tracks(cameras)
        folded = getattr(self, '_collapsed_audio_tracks', set())
        group_folded = getattr(self, '_camera_audio_collapsed', False)
        group_h = self._timeline_audio_group_h = max(self._px(24), minimum_row)
        compact_h = max(self._px(24), minimum_row)
        audio_min = max(self._px(28), minimum_row + self._px(6))
        shown = [track for track in tracks if track == 0 or not group_folded]
        minima = {track: compact_h if track in folded and track != 0 else audio_min for track in shown}
        video_h = max(minimum_row, self._px(self._timeline_video_height))
        audio_h = max(audio_min, self._px(self._timeline_audio_height))
        preferred = {track: compact_h if track in folded and track != 0 else audio_h for track in shown}
        group_total = group_h if any(track != 0 for track in tracks) else 0
        divider_h = self._px(12) if tracks and cameras else 0
        video_min = len(cameras) * minimum_row
        video_total = len(cameras) * video_h
        content_height = max(height, track_top + video_total + sum(preferred.values())
                             + group_total + divider_h + self._px(4))
        # Spare viewport space stays empty. Neither folding nor deleting tracks
        # is a request to enlarge the remaining video/audio rows.
        available = video_total + sum(preferred.values())
        expanded = [track for track in shown if track == 0 or track not in folded]
        self._timeline_audio_row_heights = preferred
        row_h = video_h
        divider_y = track_top + video_total
        self._timeline_divider_bounds = (divider_y, divider_y + divider_h) if divider_h else None
        self._timeline_split_geometry = dict(available=available, video=video_total,
            low=video_min, high=min(len(cameras)*self._px(256), available-sum(minima.values())),
            video_count=len(cameras), audio_count=len(expanded),
            compact_total=sum(preferred[t] for t in shown if t not in expanded))
        return row_h, divider_y + divider_h, content_height

    def _draw_timeline_divider(self, width):
        bounds = getattr(self, '_timeline_divider_bounds', None)
        if bounds is None: return
        canvas = self.timeline
        top, bottom = bounds
        left = canvas.canvasx(0)
        right = left + canvas.winfo_width()
        middle = (left+right)/2
        canvas.create_rectangle(0, top, width, bottom, fill='#383d43', outline='',
                                tags=('timeline-av-divider',))
        canvas.create_line(0, top, width, top, fill='#59636b', tags=('timeline-av-divider',))
        canvas.create_line(middle-self._px(28), (top+bottom)/2,
                           middle+self._px(28), (top+bottom)/2,
                           fill='#a1b0bb', width=max(1, self._px(2)), tags=('timeline-av-divider',))

    def _timeline_divider_at(self, event):
        bounds = getattr(self, '_timeline_divider_bounds', None)
        return bounds is not None and bounds[0] <= self.timeline.canvasy(event.y) <= bounds[1]

    def _begin_timeline_divider_drag(self, event):
        if not self._timeline_divider_at(event): return False
        self._cancel_timeline_redraw()
        self.timeline.focus_set()
        geometry = dict(self._timeline_split_geometry)
        geometry.update(y=event.y, original=(self._timeline_video_height, self._timeline_audio_height))
        self._timeline_divider_drag = geometry
        self.timeline.configure(cursor='sb_v_double_arrow')
        self.status.set('上下拖动调整视频与音频轨道大小；Esc 取消。')
        return True

    def _move_timeline_divider_drag(self, event):
        drag = getattr(self, '_timeline_divider_drag', None)
        if drag is None: return False
        video = max(drag['low'], min(drag['high'], drag['video'] + event.y-drag['y']))
        scale = self._px(100)/100.
        if drag['video_count']:
            self._set_timeline_track_height('video', video/drag['video_count']/scale, persist=False)
        if drag['audio_count']:
            audio = (drag['available']-video-drag['compact_total'])/drag['audio_count']/scale
            self._set_timeline_track_height('audio', min(256., audio), persist=False)
        self._queue_timeline_redraw()
        return True

    def _end_timeline_divider_drag(self, *, cancel=False):
        drag = getattr(self, '_timeline_divider_drag', None)
        if drag is None: return False
        self._timeline_divider_drag = None
        if cancel:
            self._set_timeline_track_height('video', drag['original'][0], persist=False)
            self._set_timeline_track_height('audio', drag['original'][1], persist=False)
        self._cancel_timeline_redraw()
        self.timeline.configure(cursor='')
        self.draw_timeline()
        if not cancel: self._workspace_layout_changed()
        self.status.set('已取消轨道大小调整。' if cancel else '已调整视频与音频轨道大小。')
        return True

    def _toggle_camera_audio_fold(self, track=None):
        if track == 0: return
        if track is None:
            self._camera_audio_collapsed = not getattr(self, '_camera_audio_collapsed', False)
        else:
            folded = vars(self).setdefault('_collapsed_audio_tracks', set())
            if track in folded: folded.remove(track)
            else: folded.add(track)
        self.draw_timeline()

    def _timeline_audio_fold_press(self, event):
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        group = getattr(self, '_audio_group_button', None)
        if group is not None and group[0] <= x <= group[2] and group[1] <= y <= group[3]:
            self._toggle_camera_audio_fold()
            return True
        for track, (left, top, right, bottom) in getattr(self, '_audio_fold_buttons', {}).items():
            if left <= x <= right and top <= y <= bottom:
                self._toggle_camera_audio_fold(track)
                return True
        return False

    def _timeline_layout_hover(self, event):
        if getattr(self, '_timeline_divider_drag', None) is not None or self._timeline_divider_at(event):
            self.timeline.configure(cursor='sb_v_double_arrow')
            self.status.set('上下拖动分隔条，调整视频与音频轨道大小。')
            return True
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        buttons = list(getattr(self, '_audio_fold_buttons', {}).values())
        group = getattr(self, '_audio_group_button', None)
        if group is not None: buttons.append(group)
        if any(left <= x <= right and top <= y <= bottom for left, top, right, bottom in buttons):
            self.timeline.configure(cursor='hand2')
            return True
        return False
