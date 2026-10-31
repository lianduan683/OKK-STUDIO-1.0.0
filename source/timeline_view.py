"""Per-project timeline navigation state, independent of edit history."""
from __future__ import annotations

import math
import json
from pathlib import Path


def validate_timeline_view(value):
    if not isinstance(value, dict) or set(value) - {'position', 'zoom', 'x_fraction', 'y_fraction'}:
        raise ValueError('时间线视图设置无效')
    for name, number in value.items():
        if type(number) not in (int, float) or not math.isfinite(number):
            raise ValueError('时间线视图必须使用有效数值')
        if name == 'position' and number < 0:
            raise ValueError('时间线光标位置不能为负数')
        if name == 'zoom' and not 1 <= number <= 10000:
            raise ValueError('时间线缩放比例无效')
        if name in ('x_fraction', 'y_fraction') and not 0 <= number <= 1:
            raise ValueError('时间线滚动位置无效')


class TimelineViewMixin:
    def _persist_timeline_view(self):
        """Remember navigation without publishing unsaved edits."""
        path = self.project_path or getattr(self.app, 'project_path', None)
        if not path:
            return
        path = Path(path)
        if vars(self).get('_project_upgrade_blocked_path') == str(path.resolve()):
            return
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if path.suffix.lower() == '.rtxproj':
                project = data.get('state', {}).get('okk_multicam', {}).get('project')
            else:
                project = data
            if not isinstance(project, dict) or project.get('format') != 'okk-multicam':
                return
            view = self._capture_timeline_view()
            if project.get('timeline_view') == view:
                return
            project['timeline_view'] = view
            project['version'] = max(19, project.get('version', 0))
            from project_file import write_project_file
            write_project_file(path, data)
        except (OSError, ValueError, TypeError, AttributeError) as error:
            log = getattr(self.app, '_log', None)
            if callable(log):
                log('时间线视图未能保存：' + str(error))

    def _capture_timeline_view(self):
        pending = vars(self).get('_pending_timeline_view')
        if pending is not None and pending[0] is self.project:
            view = dict(pending[1])
        else:
            view = dict(x_fraction=float(self.timeline.xview()[0]),
                        y_fraction=float(self.timeline.yview()[0]))
        view.update(position=float(self.current), zoom=float(self.zoom))
        validate_timeline_view(view)
        return view

    def _restore_timeline_view(self):
        self._close_timeline_view()
        view = dict(self.project.timeline_view)
        validate_timeline_view(view)
        self.zoom = float(view.get('zoom', 1.))
        position = min(self.project.duration, view.get('position', 0.))
        self.current = min(self.project.duration, math.floor(position * self.project.fps + .5) / self.project.fps)
        self.playing = False
        self._pending_timeline_view = (self.project, view)
        self._sync_seek_time(force=True)
        self._show_time()
        self.draw_timeline()
        self._queue_timeline_view_restore()

    def _queue_timeline_view_restore(self, _event=None):
        if (self.alive and vars(self).get('_pending_timeline_view') is not None
                and vars(self).get('_timeline_view_after') is None):
            self._timeline_view_after = self.after_idle(self._apply_timeline_viewport)

    def _apply_timeline_viewport(self):
        self._timeline_view_after = None
        pending = vars(self).get('_pending_timeline_view')
        if pending is None or not self.alive:
            return
        project, view = pending
        if project is not self.project:
            self._pending_timeline_view = None
            return
        # The final scrollregion depends on the mapped canvas size. Keep the
        # saved fractions until the workspace has actually been laid out.
        if not self.timeline.winfo_ismapped() or self.timeline.winfo_width() <= 1:
            return
        size = (self.timeline.winfo_width(), self.timeline.winfo_height())
        self.draw_timeline()
        # Rebuilding tracks may reveal a scrollbar and change the canvas width.
        # Restore fractions only after that geometry has settled for an idle pass.
        if vars(self).get('_timeline_view_size') != size:
            self._timeline_view_size = size
            self._queue_timeline_view_restore()
            return
        self.timeline.xview_moveto(view.get('x_fraction', 0.))
        self.timeline.yview_moveto(view.get('y_fraction', 0.))
        self._pending_timeline_view = None
        self._pin_timeline_headers()
        self.draw_playhead()
        self._queue_timeline_redraw()

    def _close_timeline_view(self):
        pending = vars(self).get('_timeline_view_after')
        if pending is not None:
            self.after_cancel(pending)
        self._timeline_view_after = None
        self._pending_timeline_view = None
        self._timeline_view_size = None
