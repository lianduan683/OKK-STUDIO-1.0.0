"""Bounded, cancellable live RGBA preview using the export compositor graph.

Three bounded FFmpeg slots stream the current segment and prepare two successors. No timeline-length
cache is generated. The caller owns audio/time; decoded video follows that
clock and a single latest-frame mailbox, never a queue of image buffers.
"""
from __future__ import annotations

from dataclasses import replace
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections import deque
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from exporter_core import CREATE_NO_WINDOW, _media_probe_command, _media_info_from_payload, ExportCancelled
from multicam_frame_clock import canonical_frame_rate
from video_stack_render import build_stack_command, plan_stack_segments, validate_stack_media
from proxy_media import PreviewProxyResolver, validate_proxy_media
from video_stack import pixel_format_has_alpha


class PreviewCancelled(Exception):
    pass


class PreviewCatchUp(Exception):
    """Replace only a stale continuous decoder, keeping its preview owner."""
    def __init__(self, position):
        super().__init__('continuous preview fell behind the master clock')
        self.position = position


class _PrepareCancel:
    """Stop speculative work without cancelling a clock catch-up request."""
    def __init__(self, parent):
        self.parent = parent
        self.stopped = threading.Event()

    def is_set(self):
        return self.parent.is_set() or self.stopped.is_set()

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic()+timeout
        while not self.is_set():
            remaining = .05 if end is None else min(.05, end-time.monotonic())
            if remaining <= 0: return False
            self.stopped.wait(remaining)
        return True


def preview_segments(project, position, *, single_frame=False):
    """Trim on the original project frame clock, including pauses at 60 fps."""
    project = replace(project, fps=float(canonical_frame_rate(project.fps)), range_start=0., range_end=None)
    fps = float(project.fps)
    frame = min(max(0, math.floor(position*fps+.5)), max(0, math.ceil(project.duration*fps)-1))
    if getattr(project,'multicam_outer_order',()):
        from multicam_outer import plan_outer_segments
        segments = plan_outer_segments(project,continuous_preview=True)
    else:
        segments = plan_stack_segments(project)
    for segment in segments:
        left = round(segment.start*fps); right = left+segment.frames
        if frame >= right: continue
        first = max(frame, left); count = 1 if single_frame else right-first
        delta = (first-left)/fps
        yield replace(segment, start=first/fps, duration=count/fps, frames=count,
                      layers=tuple(replace(layer, source_start=layer.source_start+delta) for layer in segment.layers))
        if single_frame: return
        frame = right


def preview_command(segment, project, ffmpeg, media, width=1280, height=720,
                    *, single_frame=False, max_fps=30., ass_path=None, source_frame_rates=None):
    """Compose at project fps, then sample playback; pause keeps its exact frame."""
    rate = canonical_frame_rate(project.fps)
    project = replace(project, fps=float(rate))
    if segment.camera == -1:
        # This is an already composited picture. Applying the graph again
        # would duplicate layer grades, opacity and burned-in subtitles.
        layer = segment.layers[0]
        output_fps = float(rate) if single_frame else min(float(rate), float(max_fps))
        graph = (f'[0:v]setpts=PTS-STARTPTS,fps={output_fps:.12g},'
                 f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                 f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=rgba[picture]')
        return ([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '2',
                 '-ss', f'{layer.source_start:.9f}', '-i', layer.path, '-filter_complex_threads', '2',
                 '-filter_complex', graph, '-map', '[picture]', '-an', '-sn', '-dn',
                 '-c:v', 'rawvideo', '-threads:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgba',
                 '-r', f'{output_fps:.12g}', '-frames:v', str(1 if single_frame else
                    max(1, math.ceil(segment.duration*output_fps-1e-8))), 'pipe:1'], output_fps)
    # A nominal MediaInfo.fps alone cannot prove a CFR source. Only use the
    # exact avg/r/time-base gate collected by the cancellable metadata probe.
    source_rates = source_frame_rates or {}
    command = build_stack_command(segment, project,
        [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin'], width, height,
        'rgba', str(rate), ['-c:v', 'rawvideo', '-threads:v', '1', '-f', 'rawvideo'],
        'pipe:1', ass_path, source_frame_rates=source_rates, media_by_path=media)
    # Preview decoding can use several slice threads; export keeps its own
    # scheduler limits. Reduce native YUV/YUVA before expensive RGB conversion.
    for index in range(len(command)-1):
        if command[index] == '-threads': command[index+1] = '4'
    # This is one preview graph, not the parallel export scheduler. Keeping
    # its YUVA scaling/conversion on one thread cannot feed 30 fps when an
    # alpha movie and subtitles are active together.
    command[command.index('-filter_complex_threads')+1] = str(min(8, max(1, os.cpu_count() or 1)))
    graph_index = command.index('-filter_complex')+1
    graph = command[graph_index]
    from clip_transform import ClipTransform, transform_at, transform_is_identity
    for index, layer in enumerate(reversed(segment.layers)):
        transform = (transform_at(project, segment.start) if layer.programme or not project.multicam_outer_order
                     else ClipTransform())
        if transform_is_identity(transform):
            marker = f'[{index}:v:0]setpts=PTS-STARTPTS,'
            prescale = (f"scale=w='min(iw,{width})':h='min(ih,{height})':"
                        'force_original_aspect_ratio=decrease:force_divisible_by=2,')
            graph = graph.replace(marker, marker+prescale, 1)
    command[graph_index] = graph
    output_fps = float(rate) if single_frame else min(float(rate), float(max_fps))
    if output_fps < float(rate):
        # Input trim/prefix stays on the project clock. Only the fully composed
        # final picture is resampled, so a 60 fps paused seek does not jump.
        graph_index = command.index('-filter_complex')+1
        command[graph_index] = command[graph_index].replace('[picture]', '[project_picture]') + \
            f';[project_picture]fps=fps={output_fps:.12g}:start_time=0[picture]'
        command[command.index('-r')+1] = f'{output_fps:.12g}'
        command[command.index('-frames:v')+1] = str(max(1, math.ceil(segment.duration*output_fps-1e-8)))
    timescale = command.index('-video_track_timescale')
    del command[timescale:timescale+2]
    return command, output_fps


def cached_stack_segments(segments, items, fps):
    """Split cache boundaries on the project clock, including partial seeks."""
    from video_stack_render import StackLayer
    for segment in segments:
        first = round(segment.start*fps)
        last = first+segment.frames
        edges = sorted({first, last, *(round(time*fps) for _, start, end in items
                        for time in (start, end) if first < round(time*fps) < last)})
        for left, right in zip(edges, edges[1:]):
            cached = next(((path, start) for path, start, end in items
                           if round(start*fps) <= left < round(end*fps) and Path(path).is_file()), None)
            if cached:
                path, start = cached
                layers = (StackLayer(0, path, path, left/fps-start, start),)
            else:
                layers = tuple(replace(layer, source_start=layer.source_start+(left-first)/fps)
                               for layer in segment.layers)
            yield replace(segment, start=left/fps, frames=right-left, duration=(right-left)/fps,
                          layers=layers, camera=-1 if cached else segment.camera)


class StackPreview:
    """Own bounded FFmpeg processes and a single latest RGBA mailbox."""
    CATCHUP_LAG = .5
    CATCHUP_INTERVAL = 2.5
    def __init__(self, ffmpeg, ffprobe, notify, *, width=1280, height=720, max_fps=30., gpu_layers=False):
        self.ffmpeg, self.ffprobe, self.notify = str(ffmpeg), str(ffprobe), notify
        self.width, self.height, self.max_fps = width, height, max_fps
        self.gpu_layers = bool(gpu_layers)
        self._condition = threading.Condition()
        self._pending = None
        self._cancel = threading.Event()
        self._closed = False
        self._process = None
        self._processes = set()
        self._latest = None
        self._notified = False
        self._generation = 0
        self._epoch = None
        self._clock = 0.
        self._playing = False
        self._stream_generation = None
        self._decoded_time = None
        self._decoded_fps = self.max_fps
        self._last_catchup = time.monotonic()
        self._catchup_count = 0
        self._startup_seconds = .1
        self._media = {}
        self._rates = {}
        self._proxies = PreviewProxyResolver()
        self._shutdown_error = None
        self._thread = threading.Thread(target=self._run, name='stack-preview-ffmpeg', daemon=False)
        self._thread.start()

    def update_clock(self, position):
        with self._condition: self._clock = float(position)

    def set_playing(self, playing):
        """Pause pipe consumption; keep decoder state and its bounded cache."""
        with self._condition:
            self._playing = bool(playing)
            self._condition.notify_all()
            return self._stream_generation == self._generation

    def refresh_presentation(self, position, playing):
        """Discard a hidden-window mailbox; report a lagging decode stream.

        A healthy or paused stream keeps both processes and their pipe cache.
        FFmpeg rawvideo has no interactive seek command, so the caller may
        restart only a substantially late stream at the current master time.
        Metadata/proxy caches remain owned by this preview in either case.
        """
        with self._condition:
            self._clock = float(position)
            self._playing = bool(playing)
            self._condition.notify_all()
            if not playing: return False
            self._latest = None
            self._notified = False
            threshold = max(.2, 3./max(1., self._decoded_fps))
            return (self._stream_generation == self._generation
                    and self._decoded_time is not None
                    and position-self._decoded_time > threshold)

    def request(self, project, position, playing, request_id, *, ass_path=None, use_proxies=True, preview_epoch=None, cache_items=(), original_cameras=()):
        with self._condition:
            if self._closed: raise RuntimeError('图层预览已经关闭。')
            if preview_epoch is None or preview_epoch != self._epoch:
                self._cancel.set()
                self._terminate_locked()
                self._generation += 1
                self._latest = None
            self._epoch = preview_epoch
            self._clock = float(position)
            self._playing = bool(playing)
            self._stream_generation = self._generation if playing else None
            self._decoded_time = None
            # A cold first frame can already be behind the running audio.
            # Permit its first recovery immediately; subsequent recoveries
            # retain the normal interval to prevent a restart loop.
            self._last_catchup = time.monotonic()-self.CATCHUP_INTERVAL
            self._pending = (self._generation, project, float(position), bool(playing), int(request_id),
                             ass_path, bool(use_proxies), preview_epoch, tuple(cache_items), frozenset(original_cameras))
            self._condition.notify()

    def cancel(self):
        with self._condition:
            self._generation += 1; self._pending = None; self._latest = None
            self._epoch = None
            self._stream_generation = None
            self._decoded_time = None
            self._cancel.set(); self._terminate_locked()

    def _terminate_locked(self):
        for process in tuple(self._processes):
            if process.poll() is None:
                try: process.terminate()
                except OSError: pass  # The owning worker always waits/reaps below.

    def take_frame(self):
        with self._condition:
            result = self._latest; self._latest = None; self._notified = False
            if result is not None and result.get('playback_frame'):
                clock = self._clock
                # Tk may have been busy after the worker published this
                # mailbox. Recheck the decoder's latest master clock when
                # taking the frame, not an older UI state-event timestamp.
                if self._playing and result['time'] < clock-1.5/result['fps']:
                    return None
                result = {**result, 'master_time': clock, 'playing': self._playing}
            return result

    def close(self, timeout=5.):
        with self._condition:
            self._closed = True; self._pending = None; self._latest = None
            self._cancel.set(); self._terminate_locked(); self._condition.notify()
        self._thread.join(timeout)
        if self._thread.is_alive(): raise RuntimeError('图层预览进程尚未退出，请重试释放缓存。')
        if self._processes:
            errors = []
            for process in tuple(self._processes):
                try: self._reap(process)
                except Exception as error: errors.append(error)
            if errors: raise errors[0]
            self._shutdown_error = None
        if self._shutdown_error: raise RuntimeError(self._shutdown_error)

    def _check(self, cancel):
        if cancel.is_set(): raise PreviewCancelled()

    def _wait_playing(self, cancel):
        with self._condition:
            while not self._playing:
                self._check(cancel)
                self._condition.wait(.05)
            self._check(cancel)

    def _catchup_target(self, generation, stamp, fps, duration):
        with self._condition:
            if (not self._playing or generation != self._generation
                    or self._pending is not None or self._cancel.is_set()): return None
            clock = self._clock
            if clock-stamp <= max(self.CATCHUP_LAG, 3./fps): return None
            now = time.monotonic()
            if now-self._last_catchup < self.CATCHUP_INTERVAL: return None
            last_frame = max(0., duration-1./fps)
            if clock >= last_frame: return None
            self._last_catchup = now
            # Prepare just ahead by the measured cold-start cost. Seeking
            # only to "now" can already be late when FFmpeg's first frame
            # arrives, particularly when composition is close to real time.
            lead = min(.5, max(1./fps, self._startup_seconds+1./fps))
            return min(last_frame, clock+lead)

    def _spawn(self, command, cancel, **kwargs):
        self._check(cancel)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, **kwargs)
        with self._condition:
            self._processes.add(process)
            self._process = process
            if cancel.is_set(): self._terminate_locked()
        return process

    def _reap(self, process):
        try:
            if process.poll() is None: process.terminate()
            try: process.wait(timeout=2.)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2.)
            if process.stdout: process.stdout.close()
            if process.stderr: process.stderr.close()
        except Exception as error:
            self._shutdown_error = f'图层预览进程未释放：{error}'
            raise
        else:
            with self._condition:
                self._processes.discard(process)
                if self._process is process: self._process = next(iter(self._processes), None)

    @contextmanager
    def _segment_stream(self, segment, project, media, width, height, playing, ass_path, rates, cancel):
        command, fps = preview_command(segment, project, self.ffmpeg, media,
            width, height, single_frame=not playing, max_fps=self.max_fps, ass_path=ass_path,
            source_frame_rates=rates)
        self._stream_layout = None
        if self.gpu_layers:
            from stack_gpu_plan import layer_atlas_command
            command, self._stream_layout = layer_atlas_command(command, fps, segment,
                project, width, height, ass_path)
        with tempfile.TemporaryFile() as error_log, tempfile.TemporaryDirectory(prefix='okk_stack_graph_') as graph_dir:
            # Script files also keep 36-input filter graphs below Windows'
            # command-line limit. Their owner stays alive until this process exits.
            graph_index = command.index('-filter_complex')
            graph_path = Path(graph_dir)/'preview.ffgraph'
            graph_path.write_text(command[graph_index+1], encoding='utf-8')
            command[graph_index:graph_index+2] = ['-/filter_complex', str(graph_path)]
            process = self._spawn(command, cancel, stdout=subprocess.PIPE, stderr=error_log)
            try: yield process, fps, error_log
            finally: self._reap(process)

    @staticmethod
    def _media_key(path):
        stat = Path(path).stat()
        return (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns)

    def _probe(self, path, cancel):
        key = self._media_key(path)
        if key in self._media: return self._media[key]
        command = _media_probe_command(self.ffprobe, path)
        entries = command.index('-show_entries')+1
        command[entries] = command[entries].replace('avg_frame_rate,r_frame_rate,', 'avg_frame_rate,r_frame_rate,time_base,')
        process = self._spawn(command, cancel,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic()+30
            while True:
                self._check(cancel)
                try: output, error = process.communicate(timeout=.05); break
                except subprocess.TimeoutExpired:
                    if time.monotonic()>deadline: raise RuntimeError('读取图层素材信息超时。')
            self._check(cancel)
            if process.returncode: raise RuntimeError(error.decode('utf-8', errors='replace'))
            payload = json.loads(output.decode('utf-8'))
            info = _media_info_from_payload(path, payload)
            self._media[key] = info
            try:
                stream = next(item for item in payload['streams'] if item.get('codec_type') == 'video'
                              and not (item.get('disposition') or {}).get('attached_pic'))
                average, nominal, base = (Fraction(str(stream[name])) for name in
                    ('avg_frame_rate', 'r_frame_rate', 'time_base'))
                ticks = 1 / average / base
                if average > 0 and average == nominal and base > 0 and ticks.denominator == 1 and ticks >= 1:
                    self._rates[key] = average
            except (KeyError, StopIteration, ValueError, ZeroDivisionError, TypeError): pass
            return info
        finally: self._reap(process)

    def _publish(self, generation, payload):
        with self._condition:
            if generation != self._generation or self._closed: return
            self._latest = payload
            if self._notified: return
            self._notified = True
        self.notify('stack_frame', None)

    def _render(self, job, cancel):
        generation, project, position, playing, request_id, ass_path, use_proxies, preview_epoch, *extra = job
        original_cameras = extra[1] if len(extra) > 1 else ()
        project = replace(project, fps=float(canonical_frame_rate(project.fps)), range_start=0., range_end=None)
        segments = preview_segments(project, position, single_frame=not playing)
        if extra and extra[0]: segments = cached_stack_segments(segments, extra[0], project.fps)
        media = {}
        if project.clips:
            reference_path = project.clips[0].path
            media[reference_path] = self._probe(reference_path, cancel)
        width, height = self.width, self.height
        if project.clips:
            reference = media[project.clips[0].path]
            from project_settings import timeline_dimensions
            canvas_width, canvas_height = timeline_dimensions(project, (reference.width, reference.height))
            ratio = min(width / canvas_width, height / canvas_height)
            width = max(2, round(canvas_width * ratio / 2) * 2)
            height = max(2, round(canvas_height * ratio / 2) * 2)
        candidates = {clip.path: clip.proxy_path for clip in project.clips}
        rates = {}
        prepare_cancel = _PrepareCancel(cancel)

        def covers_canvas(info):
            ratio = min(width/info.width, height/info.height)
            return (math.floor(info.width*ratio/2)*2 >= width
                    and math.floor(info.height*ratio/2)*2 >= height)

        def visible_segment(segment):
            # Probe the current picture top-down. A full-canvas opaque layer
            # hides every lower layer; transparent/letterboxed layers retain
            # their backgrounds. Future sources never delay the first frame.
            layers = []
            if getattr(project, 'multicam_outer_order', ()):
                from multicam_outer import resolve_compound
                segment = resolve_compound(segment, project, self.ffmpeg, self.ffprobe,
                    width, height, preview=True, cancel=prepare_cancel)
            for layer in segment.layers:
                self._check(prepare_cancel)
                for path in (layer.original_path, layer.path):
                    if path not in media: media[path] = self._probe(path, prepare_cancel)
                original, picture = media[layer.original_path], media[layer.path]
                alpha = layer.has_alpha or original.has_alpha or pixel_format_has_alpha(original.pixel_format)
                layers.append(layer)
                if segment.camera == -1: break
                if (not alpha and not picture.has_alpha and not pixel_format_has_alpha(picture.pixel_format)
                        and layer.placement == (0., 0., 1.)
                        and covers_canvas(picture)):
                    break
            segment = replace(segment, layers=tuple(layers))
            validate_stack_media((segment,), media)
            if use_proxies and segment.camera != -1:
                resolved = []
                for layer in segment.layers:
                    original = media[layer.original_path]
                    if not layer.proxy_disabled and layer.camera not in original_cameras and layer.path == layer.original_path and not (layer.has_alpha or original.has_alpha
                            or pixel_format_has_alpha(original.pixel_format)):
                        proxy = self._proxies(layer.original_path, candidates.get(layer.original_path, ''))
                        if proxy != layer.original_path:
                            try:
                                info = validate_proxy_media(self.ffprobe, layer.original_path, proxy,
                                    original.duration, project.fps, probe=lambda _tool,path:self._probe(path,prepare_cancel))
                                if abs(info.width/info.height-original.width/original.height) > 2/min(info.height,original.height):
                                    raise ValueError('代理画幅与原片不一致。')
                                if covers_canvas(original) and not covers_canvas(info):
                                    raise ValueError('代理画幅会露出已遮挡的背景，请使用原片预览。')
                            except PreviewCancelled: raise
                            except (ValueError, OSError, RuntimeError): self._proxies.reject(proxy)
                            else:
                                media[proxy] = info
                                layer = replace(layer, path=proxy)
                    resolved.append(layer)
                segment = replace(segment, layers=tuple(resolved))
            for layer in segment.layers:
                # Proxy validation resolves the file path, while the linked
                # proxy may use different Windows casing. Reuse the same
                # timing identity or a known CFR source looks unknown and
                # every seek decodes all preceding frames again.
                rate = self._rates.get(self._media_key(layer.path))
                if rate is not None: rates[layer.path] = rate
            return segment
        # Alpha identity comes from an actual probe, including old projects.
        # We intentionally use original/full-quality treatment paths, never an
        # opaque H264 preview proxy for a formerly transparent layer.
        last_status = time.monotonic()
        iterator = iter(segments)
        current = None
        pending = deque()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='stack-prewarm')
        exhausted = False

        def prepare(segment):
            owner = ExitStack()
            try:
                segment = visible_segment(segment)
                process, fps, error_log = owner.enter_context(self._segment_stream(
                    segment, project, media, width, height, playing, ass_path, rates, prepare_cancel))
                layout = getattr(self,'_stream_layout',None)
                backend = ('GPU Alpha 合成与显示' if layout else
                    'CPU 兼容合成 · GPU 显示' if self.gpu_layers else 'CPU 兼容合成与显示')
                if backend != getattr(self,'_last_backend',None):
                    self._last_backend = backend
                    self.notify('status', f'图层实时预览 · {width}×{height} · {backend} · 最高 {self.max_fps:g}fps')
                return segment, process, fps, error_log, owner, layout
            except BaseException:
                owner.close()
                raise

        def prewarm():
            nonlocal exhausted
            # Reserve the two slots before starting work: current + pending
            # (including an in-progress probe) never exceeds three decoders.
            while playing and not exhausted and len(pending) < 2:
                self._check(cancel)
                segment = next(iterator, None)
                if segment is None:
                    exhausted = True
                    break
                pending.append(executor.submit(prepare, segment))

        def await_ready(future):
            while True:
                self._check(cancel)
                try: return future.result(timeout=.05)
                except FutureTimeout:
                    if future.done(): return future.result()

        try:
            segment = next(iterator, None)
            opened_at = time.monotonic()
            first_decoded = False
            if segment is not None: current = prepare(segment)
            while current is not None:
                self._check(cancel)
                segment, process, fps, error_log, owner, layout = current
                # Successor metadata and decoder startup run separately from
                # frame delivery. Two ready successors cover very short cuts.
                frame_width = width * layout['columns'] if layout else width
                frame_height = height * layout['rows'] if layout else height
                frame_bytes = frame_width*frame_height*4; index = 0
                while True:
                    self._check(cancel)
                    if playing: self._wait_playing(cancel)
                    parts = process.stdout.read(frame_bytes)
                    if parts and len(parts) < frame_bytes:
                        remainder = bytearray(parts)
                        while len(remainder) < frame_bytes:
                            data = process.stdout.read(frame_bytes-len(remainder))
                            if not data: break
                            remainder.extend(data)
                        parts = bytes(remainder)
                    self._check(cancel)
                    if not parts: break
                    if len(parts) != frame_bytes: raise RuntimeError('图层预览返回了不完整的视频帧。')
                    stamp = segment.start + index/fps; index += 1
                    with self._condition:
                        if generation == self._generation:
                            self._decoded_time, self._decoded_fps = stamp, fps
                            if not first_decoded:
                                self._startup_seconds = min(.5, max(0., time.monotonic()-opened_at))
                    first_decoded = True
                    if playing:
                        # Audio remains the master. Never enqueue old frames
                        # if decoding or the Tk paint loop falls behind.
                        while True:
                            self._wait_playing(cancel)
                            with self._condition: clock = self._clock
                            if stamp <= clock + .5/fps: break
                            if cancel.wait(.004): raise PreviewCancelled()
                        if stamp < clock - 1.5/fps:
                            target = self._catchup_target(generation, stamp, fps, project.duration)
                            if target is not None: raise PreviewCatchUp(target)
                            if time.monotonic()-last_status > 3.:
                                self.notify('status', '图层预览解码跟不上播放时钟，正在跳过旧帧；可减少图层或使用保留透明通道的轻量素材。')
                                last_status = time.monotonic()
                            prewarm()
                            continue
                    self._publish(generation, {'request_id': request_id, 'time': stamp,
                        'width': frame_width, 'height': frame_height, 'rgba': parts, 'preview_epoch': preview_epoch,
                        **({'gpu_layers': layout} if layout else {}),
                        'fps': fps, 'playback_frame': playing})
                    prewarm()
                process.wait(timeout=3.)
                if process.returncode:
                    error_log.seek(0); raise RuntimeError(error_log.read(8192).decode('utf-8', errors='replace'))
                owner.close()
                current = None
                prewarm()
                if pending:
                    future = pending[0]
                    current = await_ready(future)
                    pending.popleft()
                    prewarm()
        finally:
            prepare_cancel.stopped.set()
            with self._condition: self._terminate_locked()
            executor.shutdown(wait=True, cancel_futures=True)
            owners = [current[4]] if current is not None else []
            for future in pending:
                if future.cancelled(): continue
                try: ready = future.result()
                except BaseException: continue
                owners.append(ready[4])
            errors = []
            for owner in owners:
                try: owner.close()
                except Exception as error: errors.append(error)
            if errors: raise errors[0]

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._closed: self._condition.wait()
                if self._closed: return
                job = self._pending; self._pending = None
                cancel = self._cancel = threading.Event()
            try: self._render(job, cancel)
            except (PreviewCancelled, ExportCancelled): pass
            except PreviewCatchUp as recovery:
                with self._condition:
                    if (not self._closed and not cancel.is_set() and self._playing
                            and job[0] == self._generation and self._pending is None):
                        self._latest = None
                        self._notified = False
                        self._decoded_time = None
                        self._catchup_count += 1
                        self._pending = (job[0], job[1], recovery.position, True, *job[4:])
                        recovered = True
                    else: recovered = False
                if recovered:
                    self.notify('status', '图层实时合成持续落后，已跳过旧画面追上播放时钟；缓存保持有界，复杂图层可能无法满帧播放。')
            except Exception as error:
                if not cancel.is_set(): self.notify('error', f'透明图层预览失败：{error}')
                if self._shutdown_error:
                    with self._condition: self._closed = True
                    return  # Keep the failed process owner for explicit close retries.
            finally:
                with self._condition:
                    if self._stream_generation == job[0] and self._pending is None:
                        self._stream_generation = None
