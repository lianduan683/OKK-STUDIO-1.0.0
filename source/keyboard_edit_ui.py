"""Map editing presets onto existing timeline, transport and range actions."""
from __future__ import annotations
from editor_core import format_time
from keyboard_profiles import checked_profile, shortcut_action, shortcut_label
from program_edits import program_spans

class KeyboardEditMixin:
    def split_timeline_at_playhead(self):
        if not self._available(): return False
        if self._selection_gesture: self._finish_selection_gesture()
        from timeline_split import split_timeline
        keys = set(self._timeline_selection)
        try:
            candidate, count, selection = split_timeline(self.project, self.current, keys)
        except (ValueError, OSError) as error:
            self.status.set(str(error)); return False
        if not count:
            self.status.set('播放头处没有可切开的'+('所选片段。' if keys else '片段。'))
            return False
        self.pause()
        self._remember_timeline_change()
        self.project = candidate
        self._refresh()
        self._set_timeline_selection(selection if keys else set())
        self.status.set(f'已在 {format_time(self.current)} 切开 {count} 项'+('所选内容' if keys else '轨道内容')+'；Ctrl+Z 撤销。')
        return True

    def _keyboard_profile(self):
        return checked_profile(getattr(self.app, 'keyboard_profile', 'current'))

    def _split_shortcut_label(self):
        action = 'split_timeline' if self._keyboard_profile() == 'resolve' else 'split'
        return shortcut_label(self._keyboard_profile(), action)

    def _keyboard_shortcut_label(self, action):
        return shortcut_label(self._keyboard_profile(), action)

    def _set_keyboard_tool(self, tool):
        self._keyboard_tool = tool
        profile = self._keyboard_profile()
        if profile == 'current': hint = 'C 切开 · 中键拖动画布 · Ctrl滚轮缩放'
        else:
            hint = (shortcut_label(profile, 'select_tool')+' 选择 · '+shortcut_label(profile, 'razor_tool')+
                    ' 剃刀 · '+self._split_shortcut_label()+' 切开')
        if tool == 'razor': hint = '剃刀工具 · 点击片段切开节目 · '+hint
        variable = getattr(self, '_keyboard_hint_var', None)
        if variable is not None: variable.set(hint)
        self.timeline.configure(cursor='crosshair' if tool == 'razor' else '')

    def _keyboard_blade_press(self, event):
        if getattr(self, '_keyboard_tool', 'select') != 'razor': return False
        if self.timeline.canvasx(event.x) < self.timeline.canvasx(0)+self._timeline_left: return False
        hit = self._timeline_hit_key(event)
        if hit is None or hit[0] not in {'program', 'clip'}: return False
        self._stop_transport_hold()
        position = self._timeline_origin()+(self.timeline.canvasx(event.x)-self._timeline_left)/self._timeline_scale()
        self.split_program_at_playhead(position)
        return True

    def _keyboard_range(self, action):
        if not self._available() or self.project.duration <= 0: return
        try: start, end, _ = self._read_range()
        except ValueError: start, end = self.project.range_start, self.project.range_end
        # The text fields show milliseconds. Recover frame-exact values before
        # making the undo snapshot, so repeated marks/undo do not lose a frame.
        start = round(start*self.project.fps)/self.project.fps
        end = round(end*self.project.fps)/self.project.fps if end is not None else None
        previous = start, end
        current = min(self.project.duration, max(0., round(self.current*self.project.fps)/self.project.fps))
        if action == 'go_in': self.seek(start); return
        if action == 'go_out':
            self.seek(max(start, (end if end is not None else self.project.duration)-1/self.project.fps))
            return
        if action == 'mark_in':
            if current >= self.project.duration: return
            start = current
            if end is not None and end <= start: end = None
        elif action == 'mark_out':
            # The project's exclusive out point includes the displayed frame.
            end = min(self.project.duration, current+1/self.project.fps)
            if start >= end: start = 0.
        elif action == 'clear_in': start = 0.
        elif action == 'clear_out': end = None
        elif action == 'clear_range': start, end = 0., None
        if (start, end) == (self.project.range_start, self.project.range_end): return
        self._remember()
        self.undo_stack[-1].range_start, self.undo_stack[-1].range_end = previous
        self.project.range_start, self.project.range_end = start, end
        self._restore_timeline_range_fields()
        self.status.set('成片范围：'+format_time(start)+' — '+(format_time(end) if end is not None else '结尾')+'；Ctrl+Z 撤销。')

    def _keyboard_jump_cut(self, direction):
        if not self._available(): return
        points = {0., self.project.duration}
        for span in program_spans(self.project): points.update((span.start, span.end))
        epsilon = .5/self.project.fps
        if direction < 0: value = max((p for p in points if p < self.current-epsilon), default=0.)
        else: value = min((p for p in points if p > self.current+epsilon), default=self.project.duration)
        self._stop_transport_hold(); self.seek(value)

    def _dispatch_keyboard_action(self, action, event):
        if action is None: return
        if action.startswith('camera_'):
            index = int(action.split('_')[1])-1
            camera = self._visible_camera_ids[index] if index < len(self._visible_camera_ids) else None
            if camera is not None: self.switch(camera)
        elif action == 'undo': self.undo()
        elif action == 'redo': self.redo()
        elif action == 'save': self.save()
        elif action == 'save_as':
            method = getattr(self.app, '_project_save_as', None)
            if method is not None: method()
        elif action == 'open': self.open()
        elif action == 'select_all': return self._select_all_timeline(event)
        elif action == 'delete': return self._delete_timeline_selection(event)
        elif action == 'toggle_clip_enabled': return self._clip_enabled_key_press()
        elif action == 'split': self.split_program_at_playhead()
        elif action == 'split_timeline': self.split_timeline_at_playhead()
        elif action == 'project_settings': self.show_project_settings()
        elif action == 'marker': return self._marker_key_press()
        elif action == 'toggle': return self._transport_space_press()
        elif action in {'previous_frame', 'reverse'}: return self._transport_key_press(-1)
        elif action == 'next_frame': return self._transport_key_press(1)
        elif action == 'pause': self._stop_transport_hold(); self.pause()
        elif action == 'play':
            self._stop_transport_hold()
            if self.player is None and self._available(): self.start_monitors()
            if self.player is not None: self.player.submit('play')
        elif action in {'select_tool', 'razor_tool'}:
            self._set_keyboard_tool('razor' if action == 'razor_tool' else 'select')
            self.status.set('剃刀工具：点击片段切开节目。' if action == 'razor_tool' else '已切换到选择工具。')
        elif action in {'previous_cut', 'next_cut'}:
            self._keyboard_jump_cut(-1 if action == 'previous_cut' else 1)
        elif action in {'home', 'end'}:
            self._stop_transport_hold()
            self.seek(0. if action == 'home' else self._last_transport_frame()/self.project.fps)
        elif action in {'mark_in', 'mark_out', 'go_in', 'go_out', 'clear_in', 'clear_out', 'clear_range'}:
            self._keyboard_range(action)
        elif action == 'zoom_in': self.change_zoom(2)
        elif action == 'zoom_out': self.change_zoom(.5)
        elif action == 'fit': self.fit_timeline()
        elif action == 'deselect':
            self._set_timeline_selection(set()); self.creative_panel_refresh()
        elif action == 'show_keys':
            method = getattr(self.app, '_show_keyboard_reference', None)
            if method is not None: method()
        return 'break'

    def _keyboard_action(self, event):
        return shortcut_action(self._keyboard_profile(), event)
