"""Read-only sampled exposure checks shared by GUI, vision APIs and MCP."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import bisect
import json
import math
from pathlib import Path
import struct
import subprocess
import threading
import time
import uuid
import zlib

import numpy as np
from creative_core import grade_filter
from exporter_core import CREATE_NO_WINDOW, ExportCancelled, probe_media
from program_edits import grade_at, program_spans
from transition_preview import transition_signature
from vision_review import validate_review, request_review


@dataclass(frozen=True)
class ScanOptions:
    scope: str = 'program'
    start: float = 0.
    end: float | None = None
    interval: float = 5.
    max_frames: int = 120
    view: str = 'graded'
    cameras: tuple[int, ...] = ()

    @classmethod
    def parse(cls, value, project):
        try:
            result = cls(**(value or {}))
        except TypeError:
            raise ValueError('曝光检测参数字段无效。') from None
        end = project.duration if result.end is None else result.end
        for field, number in (('开始', result.start), ('结束', end), ('间隔', result.interval)):
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ValueError(field + '必须是有限数字。')
        if not 0 <= result.start < end <= project.duration + 1e-7 or not .5 <= result.interval <= 300:
            raise ValueError('检测范围须在工程内，抽样间隔须为 0.5–300 秒。')
        if type(result.max_frames) is not int or not 1 <= result.max_frames <= 300:
            raise ValueError('抽样上限须为 1–300 帧。')
        if result.scope not in {'program', 'cameras'} or result.view not in {'source', 'graded'}:
            raise ValueError('检测范围或画面类型无效。')
        if not isinstance(result.cameras, (tuple, list)) or any(type(c) is not int or c not in project.cameras for c in result.cameras):
            raise ValueError('检测机位无效。')
        if result.scope == 'program' and project.video_stack_enabled:
            raise ValueError('图层合成工程请先检查各机位；本功能不把单机位抽样当作最终合成。')
        return cls(result.scope, result.start, end, result.interval, result.max_frames,
                   result.view, tuple(sorted(set(result.cameras))))


def plan_samples(project, options):
    fps = project.fps
    spans = []
    if options.scope == 'program':
        intervals = [(span.start, span.end, span.camera) for span in program_spans(project)]
    else:
        chosen = set(options.cameras or project.cameras)
        intervals = [(clip.start, clip.start + clip.duration, clip.camera) for clip in project.clips
                     if clip.camera in chosen and getattr(clip, 'enabled', True)]
    step = max(1, round(options.interval * fps))
    for start, end, camera in sorted(intervals, key=lambda item: (item[0], item[2])):
        first = math.ceil(max(options.start, start, 0) * fps - 1e-7)
        stop = math.floor(min(options.end, end) * fps + 1e-7)
        if stop > first:
            spans.append((first, stop, camera, max(1, math.ceil((stop - first) / step))))
    cumulative, total = [], 0
    for span in spans:
        total += span[3]; cumulative.append(total)
    if not total:
        raise ValueError('所选范围内没有可检测的视频。')
    count = min(total, options.max_frames)
    indices = [total // 2] if count == 1 else sorted({round(i * (total - 1) / (count - 1)) for i in range(count)})
    samples, seen = [], set()
    for index in indices:
        position = bisect.bisect_right(cumulative, index)
        first, stop, camera, _ = spans[position]
        local = index - (cumulative[position - 1] if position else 0)
        frame = min(stop - 1, first + local * step + min(step, stop - first) // 2)
        identity = camera, frame
        if identity not in seen:
            seen.add(identity); samples.append((camera, frame / fps))
    return samples, total


def analyze_pixels(pixels):
    rgb = np.frombuffer(pixels, dtype=np.uint8).reshape(-1, 3).astype(np.float64) / 255.
    if not len(rgb):
        raise ValueError('抽样图像为空。')
    y = rgb @ np.array((.2126, .7152, .0722))
    dark, bright, clipped = float(np.mean(y < .05)), float(np.mean(y >= .98)), float(np.mean(rgb.max(axis=1) >= .995))
    percentiles = np.quantile(y, (.01, .1, .5, .9, .99))
    over = bright >= .08
    under = dark >= .65 and percentiles[2] < .12
    kind = 'mixed' if over and under else 'overexposed' if over else 'underexposed' if under else 'uncertain' if clipped >= .15 else 'normal'
    reason = (f'暗部像素 {dark:.1%}，高亮像素 {bright:.1%}，通道剪切 {clipped:.1%}。'
              '这些是亮度指标，需结合主体和拍摄意图判断。')
    return dict(classification=kind, reason=reason, dark_fraction=round(dark, 6),
                highlight_fraction=round(bright, 6), channel_clip_fraction=round(clipped, 6),
                mean_luma=round(float(y.mean()), 6),
                **{name: round(float(value), 6) for name, value in zip(('p01', 'p10', 'p50', 'p90', 'p99'), percentiles)})


def _ppm(data):
    tokens, index = [], 0
    while len(tokens) < 4:
        while index < len(data) and data[index] in b' \r\n\t': index += 1
        if index >= len(data): raise ValueError('帧头不完整。')
        if data[index] == 35:
            stop = data.find(b'\n', index)
            if stop < 0: raise ValueError('帧头不完整。')
            index = stop + 1; continue
        stop = index
        while stop < len(data) and data[stop] not in b' \r\n\t': stop += 1
        tokens.append(data[index:stop]); index = stop
    if tokens[0] != b'P6' or tokens[3] != b'255': raise ValueError('帧格式无效。')
    width, height = int(tokens[1]), int(tokens[2])
    if not 1 <= width <= 640 or not 1 <= height <= 360: raise ValueError('帧尺寸无效。')
    index += 2 if data[index:index+2] == b'\r\n' else 1
    pixels = data[index:]
    if len(pixels) != width * height * 3: raise ValueError('帧数据不完整。')
    return width, height, pixels


def encode_png(width, height, pixels):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    raw = b''.join(b'\0' + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw, 5)) + chunk(b'IEND', b'')


def extract_frame(project, camera, position, view, ffmpeg, event):
    clip = project.clip_at(camera, position)
    if clip is None: raise ValueError('该时刻没有可读取的原片。')
    source_time = max(0., position - getattr(clip, 'source_origin', clip.start))
    media = (clip.path, source_time, ()) if view == 'source' else project.media_at(camera, position, use_proxies=False)
    if media is not None and str(media[0]) == clip.path:
        media = (clip.path, source_time, ())
    if media is None or not Path(media[0]).is_file(): raise ValueError('该时刻没有可读取的原片。')
    chains = []
    if view == 'graded':
        local = grade_at(project, position) if project.camera_at(position) == camera else None
        for grade in (project.camera_grades.get(camera), local, project.global_grade):
            if grade is not None:
                value = grade_filter(grade)
                if value: chains.append(value)
    chains.append('scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2,format=rgb24')
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '2',
        '-ss', f'{media[1]:.9f}', '-i', str(media[0]), '-map', '0:v:0', '-an', '-sn', '-dn',
        '-frames:v', '1', '-vf', ','.join(chains), '-c:v', 'ppm', '-f', 'image2pipe', 'pipe:1']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    deadline = time.monotonic() + 30
    try:
        while True:
            if event.is_set(): raise ExportCancelled('已取消曝光检测')
            if time.monotonic() > deadline: raise ValueError('抽样读取超时。')
            try:
                output, _error = process.communicate(timeout=.1); break
            except subprocess.TimeoutExpired: pass
        if process.returncode or len(output) > 2 * 1024 * 1024: raise ValueError('无法解码抽样画面。')
        width, height, pixels = _ppm(output)
        return encode_png(width, height, pixels), analyze_pixels(pixels), clip, source_time
    finally:
        if process.poll() is None:
            process.kill(); process.communicate(timeout=2)


class ExposureService:
    def __init__(self, ffmpeg='', root=None):
        self.ffmpeg, self.root = ffmpeg, Path(root) if root else None
        self.jobs = {}
        self.lock = threading.RLock()
        self.latest_id = ''
        self.threads = []
        self.events = {}

    def start(self, project, options=None):
        project = deepcopy(project); project.validate()
        options = ScanOptions.parse(options, project)
        samples, requested = plan_samples(project, options)
        if not self.ffmpeg or not Path(self.ffmpeg).is_file(): raise ValueError('请等待 FFmpeg 检测完成。')
        if self.root is None:
            from cache_paths import cache_dir
            self.root = cache_dir('exposure-check')
        identifier, event = uuid.uuid4().hex, threading.Event()
        with self.lock:
            if any(j['status'] == 'running' or j['review_status'] == 'running' for j in self.jobs.values()):
                raise ValueError('已有曝光检测或模型复核正在运行。')
            if len(self.jobs) >= 10:
                del self.jobs[next(iter(self.jobs))]
            folder = self.root / identifier; folder.mkdir(parents=True)
            self.jobs[identifier] = dict(scan_id=identifier, scan_revision=transition_signature(project), status='running',
                review_status='idle', progress=0., frames=[], error_count=0, generation=0, message='正在抽样…',
                view=options.view, scope=options.scope, start=options.start, end=options.end,
                interval=options.interval, requested_samples=requested, planned_samples=len(samples),
                capped=requested > len(samples), sampling_note='按间隔抽样，未检查的时间点不能据此判定无曝光问题。',
                view_note='原片画面' if options.view == 'source' else '机位调色画面，含相应色轮/曲线/LUT，不含转场、字幕及图层合成。')
            self.events[identifier] = event; self.latest_id = identifier
        ffmpeg = self.ffmpeg

        def worker():
            metadata = {}
            probe = Path(ffmpeg).with_name('ffprobe.exe' if str(ffmpeg).lower().endswith('.exe') else 'ffprobe')
            try:
                for index, (camera, position) in enumerate(samples):
                    if event.is_set(): raise ExportCancelled('已取消曝光检测')
                    frame_id = f'{index + 1:05d}'
                    try:
                        png, metrics, clip, source_time = extract_frame(project, camera, position, options.view, ffmpeg, event)
                        if clip.path not in metadata:
                            try:
                                info = probe_media(str(probe), clip.path) if probe.is_file() else None
                                metadata[clip.path] = (getattr(info, 'color_transfer', None) or '', getattr(info, 'color_primaries', None) or '')
                            except (OSError, ValueError, RuntimeError):
                                metadata[clip.path] = ('', '')
                        transfer, primaries = metadata[clip.path]
                        metrics.update(color_transfer=transfer, color_primaries=primaries)
                        if transfer in {'smpte2084', 'arib-std-b67'}:
                            metrics.update(classification='uncertain', reason='原素材带 HDR 传递函数；当前指标未经显示亮度标定，请结合色彩管理复核。 ' + metrics['reason'])
                        path = folder / (frame_id + '.png'); path.write_bytes(png)
                        record = dict(frame_id=frame_id, camera=camera, timeline_time=position, source_time=source_time,
                                      clip_id=clip.id, file_name=Path(clip.path).name, local=metrics, review=None, review_error='',
                                      image_path=str(path), read_error='')
                    except ExportCancelled: raise
                    except Exception as error:
                        record = dict(frame_id=frame_id, camera=camera, timeline_time=position,
                                      local=None, review=None, review_error='', read_error=str(error)[:500])
                    with self.lock:
                        job = self.jobs[identifier]
                        job['frames'].append(record); job['error_count'] += bool(record['read_error'])
                        job.update(progress=100 * (index + 1) / len(samples), generation=job['generation'] + 1,
                                   message=f'抽样 {index + 1}/{len(samples)} · 机位 {camera}')
                self._state(identifier, status='complete', message='抽样完成，可用模型 API 或 MCP harness 复核。')
            except Exception as error:
                self._state(identifier, status='cancelled' if event.is_set() else 'failed', message=str(error)[:500])
        self._launch(worker)
        return self.get(identifier)

    def _launch(self, function):
        thread = threading.Thread(target=function, daemon=True, name='exposure-check')
        self.threads.append(thread); thread.start()

    def _state(self, identifier, **values):
        with self.lock:
            job = self.jobs[identifier]; job.update(values); job['generation'] += 1

    def get(self, identifier, offset=0, limit=300):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 300:
            raise ValueError('结果分页无效。')
        with self.lock:
            job = deepcopy(self.jobs[identifier]); frames = job.pop('frames')
        page = [{k: v for k, v in item.items() if k != 'image_path'} for item in frames[offset:offset + limit]]
        return dict(**job, frames=page, total_frames=len(frames), next_offset=offset + limit if offset + limit < len(frames) else None)

    def frame(self, identifier, frame_id):
        with self.lock:
            item = next((deepcopy(f) for f in self.jobs[identifier]['frames'] if f['frame_id'] == frame_id), None)
        if item is None or not item.get('image_path'): raise ValueError('此抽样帧不可用。')
        path = Path(item['image_path']).resolve()
        if not path.is_relative_to((self.root / identifier).resolve()) or not path.is_file():
            raise ValueError('抽样缓存不可用，请重新检测。')
        return item

    def _check_revision(self, identifier, expected, current_project=None):
        job = self.jobs[identifier]
        if expected != job['scan_revision'] or (current_project is not None and transition_signature(current_project) != expected):
            raise ValueError('工程或调色已变化，旧抽样结果不能提交为当前结果；请重新检测。')

    def submit(self, identifier, reviews, expected, current_project=None, source='harness', model=''):
        if not isinstance(reviews, list) or not 1 <= len(reviews) <= 300:
            raise ValueError('请提交 1–300 条复核结果。')
        prepared = []
        for item in reviews:
            if not isinstance(item, dict) or set(item) != {'frame_id', 'result'} or not isinstance(item['frame_id'], str):
                raise ValueError('每条结果须包含 frame_id 和 result。')
            prepared.append((item['frame_id'], validate_review(item['result'])))
        if len({item[0] for item in prepared}) != len(prepared): raise ValueError('结果包含重复抽样帧。')
        with self.lock:
            self._check_revision(identifier, expected, current_project)
            job = self.jobs[identifier]
            if source == 'harness' and job['review_status'] == 'running': raise ValueError('API 复核进行中，请完成或取消后提交。')
            records = {f['frame_id']: f for f in job['frames'] if f.get('image_path')}
            if any(frame_id not in records for frame_id, _ in prepared): raise ValueError('包含不存在或无法读取的抽样帧。')
            for frame_id, result in prepared:
                records[frame_id]['review'] = dict(result, source=source, model=str(model)[:200])
                records[frame_id]['review_error'] = ''
            job['generation'] += 1
        return self.get(identifier)

    def review_api(self, identifier, frame_ids, expected, config, key, current_project):
        config.validate()
        if not isinstance(frame_ids, list) or not 1 <= len(frame_ids) <= 20 or len(set(frame_ids)) != len(frame_ids):
            raise ValueError('一次请选择 1–20 帧复核。')
        with self.lock:
            self._check_revision(identifier, expected, current_project)
            job = self.jobs[identifier]
            if job['status'] == 'running' or job['review_status'] == 'running': raise ValueError('请等待当前检测或复核完成。')
            frames = [self.frame(identifier, frame_id) for frame_id in frame_ids]
            event = threading.Event(); self.events[identifier] = event
            self._state(identifier, review_status='running', message=f'模型复核 0/{len(frames)}')

        def worker():
            try:
                for index, record in enumerate(frames):
                    if event.is_set(): raise ExportCancelled('已取消模型复核')
                    result = request_review(config, key, Path(record['image_path']).read_bytes(), record['local'])
                    if event.is_set(): raise ExportCancelled('已取消模型复核')
                    self.submit(identifier, [{'frame_id': record['frame_id'], 'result': result}], expected,
                                source='api', model=config.model)
                    self._state(identifier, message=f'模型复核 {index + 1}/{len(frames)}')
                self._state(identifier, review_status='complete', message='模型复核完成；判断依据可在结果列表查看。')
            except Exception as error:
                text = str(error).replace(key, '[REDACTED]') if key else str(error)
                self._state(identifier, review_status='cancelled' if event.is_set() else 'failed', message=text[:500])
        self._launch(worker)
        return self.get(identifier)

    def cancel(self, identifier):
        with self.lock:
            job = self.jobs[identifier]
            if job['status'] != 'running' and job['review_status'] != 'running':
                return self.get(identifier)
        self.events[identifier].set()
        self._state(identifier, message='正在取消；已完成的抽样和复核保留，当前网络请求可能需等待超时。')
        return self.get(identifier)

    def close(self):
        for event in self.events.values(): event.set()
        for thread in self.threads:
            if thread.is_alive(): thread.join(timeout=.3)


def workspace_service(workspace):
    service = vars(workspace).get('_exposure_service')
    if service is None:
        service = workspace._exposure_service = ExposureService()
    service.ffmpeg = getattr(workspace.app.caps, 'ffmpeg_path', '')
    return service
