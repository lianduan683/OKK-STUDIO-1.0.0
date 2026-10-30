"""Drag library items onto programme shots or an internal edit boundary."""
from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk

from editor_core import format_time
from program_edits import program_spans


@dataclass(frozen=True)
class EffectDropTarget:
    start: float
    end: float
    boundary: float | None = None


def effect_drop_target(workspace, preset, root_x, root_y):
    canvas = workspace.timeline
    if not canvas.winfo_ismapped() or not workspace._available():
        return None
    x, y = root_x - canvas.winfo_rootx(), root_y - canvas.winfo_rooty()
    if not (workspace._timeline_left <= x < canvas.winfo_width()
            and workspace._timeline_ruler_h <= y < canvas.winfo_height()):
        return None
    cy = canvas.canvasy(y)
    top, bottom = workspace._timeline_program_bounds
    if not top <= cy < bottom:
        return None
    scale, origin = workspace._timeline_scale(), workspace._timeline_origin()
    position = origin + (canvas.canvasx(x) - workspace._timeline_left) / scale
    spans = program_spans(workspace.project)
    fps = workspace.project.fps
    def video_at(camera, time):
        return next((clip for clip in workspace.project.clips
                     if clip.camera == camera and clip.start <= time < clip.end), None)
    if preset.kind in ('transition', 'mask'):
        candidates = []
        for left, right in zip(spans, spans[1:]):
            if abs(left.end - right.start) > .5 / fps:
                continue
            distance = abs(left.end - position) * scale
            if distance <= workspace._px(12) and video_at(left.camera, left.end - .5 / fps) and video_at(right.camera, right.start + .5 / fps):
                candidates.append((distance, EffectDropTarget(left.start, left.end, left.end)))
        return min(candidates, key=lambda item: item[0])[1] if candidates else None
    span = next((span for span in spans if span.start <= position < span.end), None)
    clip = video_at(span.camera, position) if span else None
    if clip is None:
        return None
    return EffectDropTarget(max(span.start, clip.start), min(span.end, clip.end))


class EffectDragMixin:
    def _init_effect_drag(self):
        self._effect_drag = None
        self._effect_drag_after = None
        self.list.bind('<ButtonPress-1>', self._effect_drag_press, add='+')
        self.list.bind('<B1-Motion>', self._effect_drag_motion, add='+')
        self.list.bind('<ButtonRelease-1>', self._effect_drag_release, add='+')
        self.list.bind('<Escape>', self._effect_drag_cancel, add='+')
        self.list.bind('<FocusOut>', self._effect_drag_cancel, add='+')

    def _effect_drag_press(self, event):
        self._effect_drag_cancel()
        row = self.list.identify_row(event.y)
        if self.list.identify_region(event.x, event.y) not in ('tree', 'cell') or not row:
            return
        item = self._catalog_by_key.get(row)
        if item is None or not self.ws._available():
            return
        self._effect_drag = dict(preset=item, x=event.x_root, y=event.y_root,
                                 current=(event.x_root, event.y_root), active=False,
                                 project=self.ws.project)

    def _effect_drag_motion(self, event):
        drag = self._effect_drag
        if drag is None:
            return
        drag['current'] = event.x_root, event.y_root
        if not drag['active']:
            if max(abs(event.x_root - drag['x']), abs(event.y_root - drag['y'])) < self.ws._px(5):
                return
            drag['active'] = True
            self.stop_preview(quiet=True)
        self._effect_drag_hover()
        if self._effect_drag_after is None:
            self._effect_drag_after = self.after(80, self._effect_drag_scroll)
        return 'break'

    def _effect_drag_hover(self):
        self.ws.timeline.delete('effect-drop')
        drag = self._effect_drag
        if drag is None or not drag['active']:
            return
        target = effect_drop_target(self.ws, drag['preset'], *drag['current']) if self.ws.project is drag['project'] else None
        self.list.configure(cursor='hand2' if target else 'X_cursor')
        if target is None:
            self.feedback.set('将转场拖到节目／合成轨中两个片段的接缝。' if drag['preset'].kind in ('transition', 'mask')
                              else '将特效拖到节目／合成轨中的视频片段。')
            return
        canvas = self.ws.timeline
        top, bottom = self.ws._timeline_program_bounds
        tx = lambda time: self.ws._timeline_left + (time - self.ws._timeline_origin()) * self.ws._timeline_scale()
        if target.boundary is None:
            canvas.create_rectangle(tx(target.start), top + 2, tx(target.end), bottom - 2,
                outline='#8fdbb1', width=self.ws._px(3), tags='effect-drop')
            message = f'松开应用「{drag["preset"].name}」到 {format_time(target.start)} — {format_time(target.end)}'
        else:
            x = tx(target.boundary)
            canvas.create_rectangle(x-self.ws._px(8), top+2, x+self.ws._px(8), bottom-2,
                outline='#8fdbb1', width=self.ws._px(3), tags='effect-drop')
            message = f'松开在 {format_time(target.boundary)} 接缝添加「{drag["preset"].name}」'
        self.feedback.set(message + ' · Esc 取消')

    def _effect_drag_scroll(self):
        self._effect_drag_after = None
        drag = self._effect_drag
        if drag is None or not drag['active']:
            return
        canvas = self.ws.timeline
        x, y = drag['current'][0]-canvas.winfo_rootx(), drag['current'][1]-canvas.winfo_rooty()
        if canvas.winfo_ismapped() and 0 <= y < canvas.winfo_height():
            edge = self.ws._px(28)
            step = -1 if self.ws._timeline_left <= x < self.ws._timeline_left+edge else 1 if canvas.winfo_width()-edge < x < canvas.winfo_width() else 0
            if step:
                canvas.xview_scroll(step, 'units')
                self.ws.draw_timeline()
            self._effect_drag_hover()
        self._effect_drag_after = self.after(80, self._effect_drag_scroll)

    def _effect_drag_release(self, event):
        drag = self._effect_drag
        if drag is None:
            return
        active = drag['active']
        target = effect_drop_target(self.ws, drag['preset'], event.x_root, event.y_root) if active and self.ws.project is drag['project'] else None
        self._effect_drag_cancel()
        if not active:
            return
        if target is None:
            self.feedback.set('未添加效果：请拖到节目／合成轨的视频片段或两个片段的接缝。')
            return 'break'
        self.ws._cancel_grade_drafts()
        self.ws.select_program_range(target.start, target.end, seek=False)
        self._apply_item(drag['preset'])
        return 'break'

    def _effect_drag_cancel(self, event=None):
        active = self._effect_drag is not None and self._effect_drag['active']
        self._effect_drag = None
        if self._effect_drag_after is not None:
            self.after_cancel(self._effect_drag_after)
            self._effect_drag_after = None
        try:
            self.list.configure(cursor='')
            canvas = getattr(self.ws, 'timeline', None)
            if canvas is not None:
                canvas.delete('effect-drop')
        except tk.TclError:
            pass
        if active and event is not None:
            self.feedback.set('已取消拖放。')
            return 'break'
