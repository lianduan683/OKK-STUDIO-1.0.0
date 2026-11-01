"""Undoable source-instance placement controls for the existing inspector."""
from copy import deepcopy

from video_placement import placement_values


class VideoPlacementMixin:
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

    def apply_video_placement(self, value):
        if not self._available(): return False
        values = placement_values(value)
        clips = self.video_placement_targets()
        if not clips:
            self.status.set('请先选中时间线上的视频素材。'); return False
        identifiers = {clip.id for clip in clips}
        candidate = deepcopy(self.project)
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
        self.status.set(f'已调整 {len(clips)} 个视频素材的位置与大小；预览和导出同步，Ctrl+Z 撤销。')
        return True
