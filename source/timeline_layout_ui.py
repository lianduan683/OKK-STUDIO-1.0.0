"""View-only audio folding and a draggable video/audio divider."""
from __future__ import annotations


class TimelineLayoutMixin:
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
        preferred = {track: compact_h if track in folded and track != 0 else audio_h for track in shown}
        group_total = group_h if any(track != 0 for track in tracks) else 0
        divider_h = self._px(12) if tracks and cameras else 0
        video_min = len(cameras) * minimum_row
        default_video = len(cameras) * max(minimum_row, self._px(29))
        content_height = max(height, track_top + default_video + sum(preferred.values())
                             + group_total + divider_h + self._px(4))
        available = content_height - track_top - group_total - divider_h - self._px(4)
        ratios = vars(self).setdefault('_timeline_split_ratios', {})
        ratio = ratios.get(self._audio_layout_key())
        desired = available - sum(preferred.values()) if ratio is None else available * ratio
        video_total = max(video_min, min(available - sum(minima.values()), desired)) if cameras else 0
        remaining = max(0., available - video_total - sum(minima.values()))
        expanded = [track for track in shown if track == 0 or track not in folded]
        self._timeline_audio_row_heights = {
            track: minima[track] + (remaining / len(expanded) if track in expanded else 0.)
            for track in shown}
        # If all camera rows are compact, give spare space to video instead.
        if not expanded and cameras:
            video_total += remaining
        row_h = video_total / max(1, len(cameras))
        divider_y = track_top + video_total
        self._timeline_divider_bounds = (divider_y, divider_y + divider_h) if divider_h else None
        self._timeline_split_geometry = dict(available=available, video=video_total,
            low=video_min, high=available-sum(minima.values()), key=self._audio_layout_key())
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
        geometry.update(y=event.y,
            original=self._timeline_split_ratios.get(geometry['key']))
        self._timeline_divider_drag = geometry
        self.timeline.configure(cursor='sb_v_double_arrow')
        self.status.set('上下拖动调整视频与音频轨道大小；Esc 取消。')
        return True

    def _move_timeline_divider_drag(self, event):
        drag = getattr(self, '_timeline_divider_drag', None)
        if drag is None: return False
        video = max(drag['low'], min(drag['high'], drag['video'] + event.y-drag['y']))
        self._timeline_split_ratios[drag['key']] = video / max(1., drag['available'])
        self._queue_timeline_redraw()
        return True

    def _end_timeline_divider_drag(self, *, cancel=False):
        drag = getattr(self, '_timeline_divider_drag', None)
        if drag is None: return False
        self._timeline_divider_drag = None
        if cancel:
            if drag['original'] is None: self._timeline_split_ratios.pop(drag['key'], None)
            else: self._timeline_split_ratios[drag['key']] = drag['original']
        self._cancel_timeline_redraw()
        self.timeline.configure(cursor='')
        self.draw_timeline()
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
