"""A programme compound track (0) among independent outer video tracks.

Source IDs and programme cuts remain intact. Only the outer ordering contains
the compound-track token; it never becomes a source camera ID.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import math
import os
from pathlib import Path
import tempfile

GROUP = 0
GROUP_URI = 'okk-multicam://programme'


def _frame(time, fps):
    return math.floor(time*fps+.5)


def outer_order(project):
    return list(getattr(project, 'multicam_outer_order', ())) or [GROUP]


def outer_cameras(project):
    return set(getattr(project, 'multicam_outer_order', ())) - {GROUP}


def has_compound(project):
    """A collapsed legacy view or persistent outer order denotes a compound."""
    return bool(project.clips and (project.timeline_multicam_collapsed or project.multicam_outer_order))


def retain_compound(project):
    """Remember the compound even while expanded and without any outer tracks."""
    if project.clips and not project.multicam_outer_order:
        project.multicam_inner_stack = project.video_stack_enabled
        project.multicam_outer_order = [GROUP]


def compound_duration(project):
    outside = outer_cameras(project)
    inside = [clip for clip in project.clips if clip.camera not in outside]
    if not inside: return 0.
    return max(float(project.master_duration), *(clip.end for clip in inside),
        *(clip.end for clip in project.audio_overrides.values()
          if clip is not None and clip.camera not in outside))


def validate_outer(project):
    values = project.multicam_outer_order
    if (not isinstance(values, list) or len(values) > 100000
            or any(type(value) is not int or value < 0 for value in values)
            or len(values) != len(set(values))
            or (values and GROUP not in values)
            or not (set(values)-{GROUP}) <= set(project.timeline_cameras)
            or type(project.multicam_inner_stack) is not bool):
        raise ValueError('多机位外层轨道顺序无效')


def prune_outer(project):
    if not getattr(project, 'multicam_outer_order', ()): return
    had_outer = bool(outer_cameras(project))
    project.multicam_outer_order = [value for value in project.multicam_outer_order
        if value == GROUP or value in project.timeline_cameras]
    if had_outer and project.multicam_outer_order == [GROUP]:
        project.video_stack_enabled = project.multicam_inner_stack


def place_outer(project, camera, placement):
    if not project.multicam_outer_order:
        project.multicam_inner_stack = project.video_stack_enabled
    order = [value for value in outer_order(project) if value != camera]
    side, reference = placement
    index = (order.index(reference)+(side == 'below')
             if reference in order and side in {'above', 'below'}
             else len(order) if side == 'bottom' else 0)
    order.insert(index, camera)
    project.multicam_outer_order = order
    project.video_stack_enabled = True
    project.video_stack_hidden = [value for value in project.video_stack_hidden if value != camera]


def plan_outer_segments(project, *, continuous_preview=False):
    from video_stack_render import StackLayer, StackSegment, _layer
    fps = project.fps
    first = _frame(project.range_start, fps)
    last = _frame(project.duration if project.range_end is None else project.range_end, fps)
    if first >= last: raise ValueError('成片范围不足一帧')
    cameras = outer_cameras(project)
    clips = [clip for clip in project.clips if clip.enabled and clip.camera in cameras]
    edges = {first, last}
    group_end = _frame(compound_duration(project), fps)
    edges.add(group_end)
    for clip in clips: edges.update((_frame(clip.start, fps), _frame(clip.end, fps)))
    for clip in project.clips:
        if clip.camera not in cameras: edges.update((_frame(clip.start, fps), _frame(clip.end, fps)))
    edges.update(_frame(cut.time, fps) for cut in project.cuts)
    for transition in project.transitions:
        edges.update((_frame(transition.time-transition.duration/2, fps),
                      _frame(transition.time+transition.duration/2, fps)))
    for item in (*project.clip_grades, *project.clip_transforms):
        edges.update((_frame(item.start, fps), _frame(item.end, fps)))
    # Short, reusable compound blocks bound decode work during live preview.
    step = max(1, round(4*fps))
    blocks = range(first//step*step, last, step)
    if not continuous_preview or project.multicam_inner_stack:
        edges.update(blocks)
    else:
        # A direct programme layer is already a bounded streaming decoder.
        # Artificial 4-second splits only serve complex compound caches;
        # reopening a large ProRes file at each split causes visible stalls.
        windows = [(_frame(t.time-t.duration/2,fps),_frame(t.time+t.duration/2,fps))
                   for t in project.transitions]
        edges.update(edge for edge in blocks if any(a<edge<b for a,b in windows))
    boundaries = sorted(value for value in edges if first <= value <= last)
    result = []
    for left, right in zip(boundaries, boundaries[1:]):
        layers = []
        for camera in project.multicam_outer_order:
            if camera == GROUP:
                if left < group_end:
                    layers.append(StackLayer(GROUP, GROUP_URI, GROUP_URI, left/fps, 0.))
            elif camera not in project.video_stack_hidden:
                clip = max((clip for clip in clips if clip.camera == camera
                    and _frame(clip.start, fps) <= left and _frame(clip.end, fps) >= right),
                    key=lambda clip: (clip.start, clip.id), default=None)
                if clip is not None: layers.append(_layer(project, clip, left/fps))
        result.append(StackSegment(left/fps, (right-left)/fps, right-left, tuple(layers)))
    return result


def compound_project(project):
    from creative_core import ColorGrade
    inner = copy.deepcopy(project)
    inner.multicam_outer_order = []
    inner.video_stack_enabled = project.multicam_inner_stack
    inner.multicam_inner_stack = False
    inner.video_stack_hidden = list(set(inner.video_stack_hidden) | outer_cameras(project))
    inner.global_grade = ColorGrade()
    inner.subtitles.enabled = False
    inner.audio_mixer = {}
    inner.timeline_multicam_collapsed = False
    return inner


def resolve_compound(segment, project, ffmpeg, ffprobe, width, height, *,
                     preview=False, deep=False, cancel=None):
    """Resolve the virtual base on a worker using the unchanged programme renderer."""
    if cancel is not None and cancel.is_set():
        from exporter_core import ExportCancelled
        raise ExportCancelled('多机位轨道处理已取消')
    if not any(layer.path == GROUP_URI for layer in segment.layers): return segment
    transition = any(item.time-item.duration/2 < segment.end-1e-8
        and item.time+item.duration/2 > segment.start+1e-8 for item in project.transitions)
    if not project.multicam_inner_stack and not transition:
        camera = project.camera_at(segment.start)
        clip = project.clip_at(camera, segment.start)
        if clip is not None and camera not in outer_cameras(project):
            from video_stack_render import _layer
            source = replace(_layer(project, clip, segment.start), programme=True)
            return replace(segment, layers=tuple(source if layer.path == GROUP_URI else layer
                                                for layer in segment.layers))
    from cache_paths import cache_dir
    from exporter_core import Capabilities, ExportSettings, ExportCancelled
    from multicam_core import render_multicam
    from prerender import picture_signature
    from quick_proxy import _run_encode
    inner = compound_project(project)
    inner.range_start, inner.range_end = segment.start, segment.end
    from project_settings import DEFAULT_SETTINGS
    inner.project_settings = {**DEFAULT_SETTINGS, **inner.project_settings}
    inner.project_settings.update(width=width, height=height)
    signature = picture_signature(inner, (width, height))
    first = _frame(segment.start, project.fps)
    mode = 'preview' if preview else '10bit' if deep else '8bit'
    folder = cache_dir('multicam-compound')/signature
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder/f'{first}-{segment.frames}-{mode}.mp4'
    if cancel is not None and cancel.is_set(): raise ExportCancelled('多机位轨道处理已取消')
    if not destination.is_file() or destination.stat().st_size <= 80:
        codec = 'libx265' if deep and not preview else 'libx264'
        args = ['-threads', '2', '-filter_threads', '2', '-filter_complex_threads', '2']
        if codec == 'libx265': args += ['-x265-params', 'lossless=1:log-level=error:pools=1']
        settings = ExportSettings(codec=codec, preset='ultrafast', quality=20 if preview else 0,
            resolution='timeline', audio='none', decode_mode='cpu',
            bit_depth='10-bit' if deep and not preview else '8-bit', bframes=0, extra_args=args)
        def runner(command, callback):
            _run_encode(command, segment.duration, cancel_event=cancel, phase='多机位轨道合成')
            return 0, ''
        with tempfile.TemporaryDirectory(prefix='.compound-', dir=folder) as work:
            temporary = Path(work)/'compound.mp4'
            render_multicam(inner, temporary, ffmpeg, ffprobe, settings,
                Capabilities(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe), runner,
                cancel_event=cancel, work_directory=work)
            if cancel is not None and cancel.is_set(): raise ExportCancelled('多机位轨道处理已取消')
            if picture_signature(inner, (width, height)) != signature:
                raise ExportCancelled('合成期间素材发生变化，请重试')
            os.replace(temporary, destination)
    return replace(segment, layers=tuple(replace(layer, path=str(destination),
        original_path=str(destination), source_start=0., phase_origin=segment.start)
        if layer.path == GROUP_URI else layer for layer in segment.layers))
