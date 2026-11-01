"""Undoable shot ranges and selected source placements in the inspector."""
from copy import deepcopy

from video_placement import placement_values, placement_at, set_program_placement


class VideoPlacementMixin:
    def video_placement_scope(self):
        keys=self._timeline_selection
        ranges=sorted(value for kind,value in keys if kind=='program')
        if ranges: return ('program',tuple(ranges))
        identifiers=tuple(sorted(value for kind,value in keys if kind=='clip'))
        if identifiers: return ('sources',identifiers)
        if keys: return None
        identifiers=tuple(self.media_tree.selection())
        if identifiers: return ('sources',identifiers)
        selected=self.selected_program_range or self.program_range_at_playhead()
        return ('program',(tuple(selected),)) if selected else None

    def video_placement_value(self):
        scope=self.video_placement_scope()
        if scope and scope[0]=='program':
            time=scope[1][0][0]
            return placement_at(self.project,self.project.camera_at(time),time)
        clips=self.video_placement_targets()
        return placement_values(clips[0]) if clips else (0.,0.,1.)

    def preview_video_placement(self,value,scope=None):
        if self.player:
            self.player.submit('placement_preview',value=value,scope=scope or self.video_placement_scope())

    def video_placement_targets(self):
        keys = self._timeline_selection
        identifiers = {identifier for kind, identifier in keys if kind == 'clip'}
        if identifiers:
            return [clip for clip in self.project.clips if clip.id in identifiers]
        if keys and not any(kind == 'program' for kind, _ in keys): return []
        if not keys:
            identifiers = set(self.media_tree.selection())
            clips = [clip for clip in self.project.clips if clip.id in identifiers]
            if clips: return clips
        time = self.selected_program_range[0] if self.selected_program_range else self.current
        clip = self.project.clip_at(self.project.camera_at(time), time)
        return [clip] if clip is not None else []

    def apply_video_placement(self, value, *, scope=None):
        if not self._available(): return False
        values = placement_values(value)
        scope=scope or self.video_placement_scope()
        clips = self.video_placement_targets()
        if not clips:
            self.status.set('请先选中时间线上的视频素材。'); return False
        identifiers = {clip.id for clip in clips}
        candidate = deepcopy(self.project)
        if scope and scope[0]=='program':
            for start,end in scope[1]: set_program_placement(candidate,start,end,value)
        else:
            identifiers=set(scope[1]) if scope else identifiers
            for clip in candidate.clips:
                if clip.id in identifiers:
                    clip.placement = {} if values == (0., 0., 1.) else dict(zip(('x', 'y', 'scale'), values))
        candidate.validate()
        if candidate == self.project: return True
        keys = set(self._timeline_selection)
        self._remember()
        self.project = candidate
        self._refresh()
        self._set_timeline_selection(keys)
        self.preview_video_placement(None)
        label='所选节目片段' if scope and scope[0]=='program' else f'{len(clips)} 个视频素材'
        self.status.set(f'已调整{label}的位置与大小；Ctrl+Z 撤销。')
        return True
