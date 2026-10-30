"""Timeline snapping and linked-selection controls."""
from bisect import bisect_left
import math
import tkinter as tk

from timeline_audio import source_audio_clips
from timeline_link import edit_keys


class TimelineControlsMixin:
    def _init_timeline_controls(self):
        self.timeline_snap_var = tk.BooleanVar(self, value=True)
        self.timeline_link_var = tk.BooleanVar(self, value=True)
        self._timeline_snap_points = []

    def _build_timeline_controls(self, parent):
        for name, text, variable in (('snap', '吸附', self.timeline_snap_var),
                                     ('link', '链接', self.timeline_link_var)):
            button = tk.Checkbutton(parent, text=text, variable=variable,
                command=lambda key=name: self._toggle_timeline_control(key),
                bg='#292b2e', fg='#73aa9c', activebackground='#292b2e',
                activeforeground='#dedfe3', selectcolor='#18191b',
                font=('Microsoft YaHei UI', 9), borderwidth=0)
            button.pack(side='left', padx=self._px(4))
            setattr(self, 'timeline_'+name+'_button', button)

    def _refresh_timeline_controls(self):
        self.timeline_snap_var.set(self.project.timeline_snap_enabled)
        self.timeline_link_var.set(self.project.timeline_link_enabled)

    def _toggle_timeline_control(self, name):
        variable = getattr(self, 'timeline_'+name+'_var')
        field = 'timeline_'+name+'_enabled'
        if not self._available():
            variable.set(getattr(self.project, field))
            return
        value = bool(variable.get())
        self._finish_selection_gesture(cancel=True)
        if value != getattr(self.project, field):
            self._remember()
            setattr(self.project, field, value)
        if name == 'link':
            self._set_timeline_selection(edit_keys(self.project, self._timeline_selection))
        self.status.set(('吸附已开启：拖动时间标尺时吸附到附近的素材边缘和节目切点。' if value else
                         '吸附已关闭：可自由定位时间线光标。') if name == 'snap' else
                        ('链接已开启：音视频成对选择、移动和删除；保留现有相对位置。' if value else
                         '链接已关闭：视频和机位原声可分别选择、移动和删除。'))

    def _update_timeline_snap_points(self):
        points = {0., self.project.duration, *self.project.edit_points,
                  *(cut.time for cut in self.project.cuts), *self._timeline_boundary_times}
        for clip in [*self.project.clips, *source_audio_clips(self.project)]:
            points.update((clip.start, clip.end))
        self._timeline_snap_points = sorted(value for value in points if 0 <= value <= self.project.duration)

    def _timeline_pointer_time(self, x):
        """Use Canvas coordinates once; tolerance stays eight screen pixels."""
        value = self._timeline_origin()+(self.timeline.canvasx(x)-self._timeline_left)/self._timeline_scale()
        if self.project.timeline_snap_enabled:
            points = self._timeline_snap_points
            index = bisect_left(points, value)
            nearest = min(points[max(0, index-1):index+1], key=lambda p: abs(p-value), default=None)
            if nearest is not None and abs(nearest-value)*self._timeline_scale() <= self._px(8):
                value = nearest
        return max(0., min(self.project.duration, math.floor(value*self.project.fps+.5)/self.project.fps))
