"""Frame-aligned preview caches. The background owner never accesses Tk."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import tempfile
import threading

from creative_render import has_pixel_effects
from exporter_core import ExportCancelled, ExportSettings
from multicam_core import render_multicam
from multicam_frame_clock import canonical_frame_rate
from quick_proxy import _cancelled, _probe_media_cancel, _run_encode

CACHE_VERSION = 1
COLORS = {'yellow': '#e4bd48', 'red': '#e26359', 'green': '#65bd83'}
RESOLUTIONS = {'960 × 540': (960, 540), '1280 × 720': (1280, 720),
               '1920 × 1080': (1920, 1080), '2560 × 1440': (2560, 1440),
               '3840 × 2160': (3840, 2160), '自定义': None}


def validate_resolution(width, height):
    if any(type(value) is not int or value % 2 or not 16 <= value <= 7680 for value in (width, height)):
        raise ValueError('预渲染宽高须为 16–7680 之间的偶数。')
    return width, height


def file_stamp(path):
    if not path: return None
    resolved = Path(path).expanduser().resolve()
    try:
        stat = resolved.stat()
        return [str(resolved), stat.st_size, stat.st_mtime_ns]
    except OSError:
        return [str(resolved), None, None]


def picture_signature(project, resolution=(1280, 720)):
    """Exclude sound, view state and export ranges; include external asset changes."""
    data = asdict(project)
    keep = ('clips', 'cuts', 'fps', 'global_grade', 'camera_grades', 'transitions',
            'subtitles', 'clip_grades', 'stabilizations', 'library_overlays',
            'video_stack_enabled', 'video_stack_order', 'video_stack_hidden', 'project_settings',
            'edit_points', 'master_duration')
    data = {key: data[key] for key in keep if key in data}
    paths = {clip.path for clip in project.clips}
    for item in project.stabilizations: paths.add(item.cache_path)
    for item in project.library_overlays: paths.update((item.cache_path, item.asset_path))
    for item in project.transitions: paths.add(getattr(item, 'mask_path', ''))
    for grade in (project.global_grade, *project.camera_grades.values(), *(x.grade for x in project.clip_grades)):
        if grade is not None: paths.add(grade.lut_path)
    data['files'] = [file_stamp(path) for path in sorted(paths) if path]
    data['cache_version'] = CACHE_VERSION
    data['resolution'] = resolution
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class PreviewBlock:
    first: int
    last: int
    fps: float
    state: str = 'yellow'
    path: str = ''
    reason: str = ''

    @property
    def start(self): return self.first/self.fps
    @property
    def end(self): return self.last/self.fps


def plan_blocks(project, seconds=4.):
    fps = float(canonical_frame_rate(project.fps))
    last = max(0, math.floor(project.duration*fps+.5))
    step = max(1, round(seconds*fps))
    return [PreviewBlock(first, min(first+step, last), fps) for first in range(0, last, step)]


def needs_render(project, block, media):
    first, last = block.start, block.end
    if any(t.time-t.duration/2 < last and t.time+t.duration/2 > first for t in project.transitions):
        return '转场合成'
    active = [clip for clip in project.clips if clip.start < last and clip.end > first]
    if project.video_stack_enabled:
        cameras = set(project.stacked_cameras())
        active = [clip for clip in active if clip.camera in cameras]
        edges = sorted({first, last, *(max(first, clip.start) for clip in active),
                        *(min(last, clip.end) for clip in active)})
        if any(len({clip.camera for clip in active if clip.start < right and clip.end > left}) > 1
               for left, right in zip(edges, edges[1:])): return '多图层合成'
    else:
        active = [clip for clip in active if any(span.camera == clip.camera and span.start < last
                  and span.end > first for span in project.segments(start=first, end=last))]
    for clip in active:
        position = max(first, clip.start)
        if has_pixel_effects(project, clip.camera, position): return '画面调色或特效'
        if any(item.start < last and item.end > first for item in project.clip_grades): return '片段调色'
        info = media.get(clip.path)
        if info is None: return '素材信息不可用'
        if (info.width*info.height*max(info.fps, project.fps) > 1920*1080*60
                or info.has_alpha or info.bit_depth > 8): return '高解码负载'
    return ''


def check_folder(value):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('请选择预渲染视频保存位置。')
    folder = Path(value).expanduser()
    if not folder.is_absolute(): raise ValueError('预渲染保存位置须为绝对路径。')
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=folder): pass
    return folder


def render_block(project, block, folder, signature, caps, cancel, progress=None, resolution=(1280, 720)):
    """Full compositor at the chosen size; audio continues on the master clock."""
    _cancelled(cancel)
    root = Path(folder)/'OKK-Prerender'/signature
    root.mkdir(parents=True, exist_ok=True)
    destination = root/f'{block.first}-{block.last}.mp4'
    marker = destination.with_suffix('.json')
    expected = dict(version=CACHE_VERSION, signature=signature, first=block.first,
                    last=block.last, fps=block.fps)

    def validate(path):
        info = _probe_media_cancel(caps.ffprobe_path, str(path), cancel_event=cancel)
        if (info.video_codec != 'h264' or info.width <= 0 or info.height <= 0
                or abs(info.fps-block.fps) > .01
                or abs(info.duration-(block.end-block.start)) > max(.04, 1/block.fps)+.025):
            raise ValueError('预渲染视频未通过时长、帧率或格式校验。')
    if destination.is_file() and marker.is_file():
        try:
            saved = json.loads(marker.read_text(encoding='utf-8'))
            if saved != {**expected, 'file': file_stamp(destination)}: raise ValueError('缓存已更改')
            validate(destination)
            return str(destination)
        except (OSError, ValueError, RuntimeError):
            _cancelled(cancel)
    preview = copy.deepcopy(project)
    preview.fps, preview.range_start, preview.range_end = block.fps, block.start, block.end
    # The live monitor shows enabled subtitles even when export burn-in is off.
    preview.subtitles.burn_in = True
    # Fit within the selected box while preserving the actual timeline aspect.
    validate_resolution(*resolution)
    info = _probe_media_cancel(caps.ffprobe_path, project.clips[0].path, cancel_event=cancel)
    size = project.project_settings or dict(width=info.width, height=info.height)
    ratio = min(resolution[0]/max(1, size['width']), resolution[1]/max(1, size['height']))
    preview.project_settings = dict(width=max(16, round(size['width']*ratio/2)*2),
        height=max(16, round(size['height']*ratio/2)*2), sample_rate=48000, bit_depth='24bit')
    settings = ExportSettings(codec='libx264', preset='veryfast', quality=20, resolution='timeline',
        audio='none', decode_mode='cpu', bit_depth='8-bit', gop=max(1, round(block.fps)),
        bframes=0, extra_args=['-threads', '2', '-filter_threads', '2', '-filter_complex_threads', '2'])

    def runner(command, report):
        _run_encode(command, block.end-block.start, cancel_event=cancel,
                    progress=progress, phase='预渲染')
        return 0, ''
    with tempfile.TemporaryDirectory(prefix='.render-', dir=root) as work:
        temporary = Path(work)/'preview.mp4'
        render_multicam(preview, temporary, caps.ffmpeg_path, caps.ffprobe_path, settings, caps,
                        runner, cancel_event=cancel, work_directory=work)
        validate(temporary)
        _cancelled(cancel)
        if picture_signature(project, resolution) != signature: raise ExportCancelled('素材在预渲染期间发生变化')
        os.replace(temporary, destination)
    temporary_marker = marker.with_suffix('.pending')
    try:
        temporary_marker.write_text(json.dumps({**expected, 'file': file_stamp(destination)}), encoding='utf-8')
        os.replace(temporary_marker, marker)
    finally: temporary_marker.unlink(missing_ok=True)
    return str(destination)


class PrerenderWorker:
    """One cancellable generation and one worker, with an explicit shutdown owner."""
    def __init__(self, events):
        self.events = events
        self.condition = threading.Condition()
        self.cancel = threading.Event()
        self.closed = False
        self.pending = None
        self.thread = threading.Thread(target=self._run, name='okk-prerender', daemon=False)
        self.thread.start()

    def request(self, generation, project, folder, caps, slow=(), resolution=(1280, 720)):
        with self.condition:
            self.cancel.set()
            self.pending = (generation, copy.deepcopy(project), folder, copy.deepcopy(caps), set(slow), resolution)
            self.condition.notify()

    def invalidate(self):
        with self.condition:
            self.cancel.set(); self.pending = None

    def close(self):
        with self.condition:
            self.closed = True; self.cancel.set(); self.pending = None; self.condition.notify()
        # The non-daemon worker reaps its child processes before it exits.

    def _run(self):
        media = {}
        while True:
            with self.condition:
                while self.pending is None and not self.closed: self.condition.wait()
                if self.closed: return
                job, self.pending = self.pending, None
                cancel = self.cancel = threading.Event()
            generation, project, folder, caps, slow, resolution = job
            def emit(kind, **payload): self.events.put((generation, kind, payload))
            try:
                signature = picture_signature(project, resolution)
                blocks = plan_blocks(project)
                for clip in project.clips:
                    _cancelled(cancel)
                    stamp = tuple(file_stamp(clip.path))
                    if stamp not in media:
                        media[stamp] = _probe_media_cancel(caps.ffprobe_path, clip.path, cancel_event=cancel)
                infos = {clip.path: media[tuple(file_stamp(clip.path))] for clip in project.clips}
                # Bound metadata retention across successive projects.
                media = {tuple(file_stamp(clip.path)): infos[clip.path] for clip in project.clips}
                for index, block in enumerate(blocks):
                    _cancelled(cancel)
                    reason = '实测播放帧率不足' if block.first in slow else needs_render(project, block, infos)
                    blocks[index] = replace(block, state='red' if reason else 'yellow', reason=reason)
                emit('plan', signature=signature, blocks=blocks,
                     source_rates={path: info.fps for path, info in infos.items()})
                for block in blocks:
                    _cancelled(cancel)
                    root = Path(folder)/'OKK-Prerender'/signature
                    # Reuse validated caches even when initial analysis is now yellow.
                    if block.state != 'red' and not (root/f'{block.first}-{block.last}.json').is_file(): continue
                    emit('progress', first=block.first, percent=0.)
                    try:
                        path = render_block(project, block, folder, signature, caps, cancel,
                            lambda percent, _phase: emit('progress', first=block.first, percent=percent), resolution)
                        _cancelled(cancel)
                        emit('ready', block=replace(block, state='green', path=path))
                    except ExportCancelled: raise
                    except Exception as error: emit('failed', first=block.first, error=str(error))
                emit('done')
            except ExportCancelled: pass
            except Exception as error:
                if not cancel.is_set(): emit('error', error=str(error))


def cached_spec(items, position):
    for path, first, last in items:
        if first <= position < last and Path(path).is_file():
            return ('prerender', path, first, last), path, position-first
    return None
