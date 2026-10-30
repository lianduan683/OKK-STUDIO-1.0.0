"""Continuous source-track audio, independent of camera picture switching.

Track 0 is the master recording; positive track IDs match video camera IDs.
Only original recordings are used.  Proxies and rendered picture effects never
replace the associated source audio.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import heapq
import json
import math
import os
from pathlib import Path
import subprocess


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _camera_ids(project):
    if _get(project, 'audio_overrides', {}):
        return sorted({_get(clip, 'camera') for clip in source_audio_clips(project)})
    cameras = _get(project, 'cameras')
    if cameras is not None:
        return sorted(cameras)
    return sorted({_get(clip, 'camera', 0) for clip in _get(project, 'clips', [])})


def audio_track_enabled(project, track: int) -> bool:
    """An untouched legacy project retains its master/first-camera soundtrack."""
    states = _get(project, 'audio_track_enabled', {}) or {}
    if track in states: return states[track]
    if str(track) in states: return states[str(track)]
    master = _get(project, 'master_audio', '')
    if track == 0: return bool(master)
    if states or master: return False
    candidates = [_get(clip, 'camera') for clip in source_audio_clips(project)
                  if _get(clip, 'has_audio') is not False]
    if not candidates: candidates = _camera_ids(project)
    return bool(candidates) and track == min(candidates)


@dataclass(frozen=True, slots=True)
class AudioSource:
    track: int
    path: str
    start: float
    duration: float
    source_start: float = 0.0
    clip_id: str = ''

    @property
    def end(self):
        return self.start + self.duration


def source_audio_clips(project):
    """Resolve independent audio, including audio whose picture was deleted."""
    clips = _get(project, 'clips', [])
    overrides = _get(project, 'audio_overrides', {}) or {}
    if not overrides:
        return clips
    ids = {_get(clip, 'id') for clip in clips}
    result = [overrides.get(_get(clip, 'id'), clip) for clip in clips]
    result.extend(clip for identifier, clip in overrides.items() if identifier not in ids)
    return [clip for clip in result if clip is not None]


def audio_sources(project, *, enabled_only=True) -> list[AudioSource]:
    """Resolve track clips against the timeline, with latest clip winning overlaps."""
    clips = source_audio_clips(project)
    duration = _get(project, 'duration')
    if duration is None:
        duration = max([float(_get(project, 'master_duration', 0.)),
                        *[float(_get(c, 'start', 0.))+float(_get(c, 'duration', 0.)) for c in clips]])
    duration = max(0., float(duration))
    result = []
    master = _get(project, 'master_audio', '')
    if master and (not enabled_only or audio_track_enabled(project, 0)):
        master_duration = float(_get(project, 'master_duration', 0.)) or duration
        if master_duration > 0:
            result.append(AudioSource(0, str(master), 0., min(duration, master_duration)))
    for camera in _camera_ids(project):
        if enabled_only and not audio_track_enabled(project, camera): continue
        track_clips = [clip for clip in clips if _get(clip, 'camera') == camera]
        # Keep both interval edges so an older recording resumes after a short
        # overlapping take, exactly as MulticamProject.clip_at does for video.
        points = sorted({0., duration, *[max(0., min(duration, value))
                        for clip in track_clips for value in
                        (float(_get(clip, 'start', 0.)),
                         float(_get(clip, 'start', 0.))+float(_get(clip, 'duration', 0.)))]})
        ranked = sorted(track_clips, key=lambda c: (float(_get(c, 'start', 0.)), str(_get(c, 'id', ''))))
        active = []
        next_clip = 0
        for left, right in zip(points, points[1:]):
            if right <= left: continue
            while next_clip < len(ranked) and float(_get(ranked[next_clip], 'start', 0.)) <= left:
                # Sorted rank preserves the same start/id tiebreak as picture
                # selection, with a max-heap emulated using negative ranks.
                heapq.heappush(active, (-next_clip, ranked[next_clip]))
                next_clip += 1
            while active:
                clip = active[0][1]
                if left < float(_get(clip, 'start', 0.))+float(_get(clip, 'duration', 0.)): break
                heapq.heappop(active)
            if not active: continue
            clip = active[0][1]
            if _get(clip, 'has_audio') is False: continue
            source = AudioSource(camera, str(_get(clip, 'path', '')), left, right-left,
                                 left-float(_get(clip, 'start', 0.)), str(_get(clip, 'id', '')))
            if (result and result[-1].track == source.track and result[-1].clip_id == source.clip_id
                    and abs(result[-1].end-source.start) < 1e-8):
                result[-1] = replace(result[-1], duration=result[-1].duration+source.duration)
            else: result.append(source)
    return result


def probe_audio_sources(ffprobe, sources, cancel_event=None, *, probe=None):
    """Skip genuinely silent media; a missing enabled recording remains an error."""
    info = {}
    durations = {}
    result = []
    for source in sources:
        if cancel_event is not None and cancel_event.is_set():
            from exporter_core import ExportCancelled
            raise ExportCancelled('音轨处理已取消')
        if source.path not in info:
            if not Path(source.path).is_file(): raise ValueError('音轨原始素材不存在：' + source.path)
            if probe is None:
                command = [str(ffprobe), '-v', 'error', '-select_streams', 'a:0',
                           '-show_entries', 'stream=codec_name,sample_rate,duration:format=duration',
                           '-of', 'json', source.path]
                completed = subprocess.run(command, capture_output=True, timeout=30,
                                           creationflags=0x08000000 if os.name == 'nt' else 0)
                if completed.returncode:
                    raise ValueError('无法读取音轨：' + source.path)
                payload = json.loads(completed.stdout.decode('utf-8', 'replace'))
                stream = next(iter(payload.get('streams', [])), {})
                try:
                    available = float(stream.get('duration') or payload.get('format', {}).get('duration') or 0.)
                except (ValueError, TypeError): available = 0.
            else:
                stream, available = probe(source.path)
            info[source.path] = stream
            durations[source.path] = available if math.isfinite(available) and available > 0 else None
        if not info[source.path]: continue
        available = durations[source.path]
        if available is not None:
            length = min(source.duration, available-source.source_start)
            if length <= 0: continue
            source = replace(source, duration=length)
        result.append(source)
    return result, info


def build_audio_mix_args(sources, start: float, duration: float, *, sample_rate=48000, input_offset=0):
    """Return FFmpeg input args, a filter graph, and its output label.

    The graph is exactly the requested timeline range, including leading,
    trailing, and between-recording silence.  Inputs must have an audio stream;
    use probe_audio_sources when importing old projects with unknown metadata.
    """
    if not math.isfinite(start) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('音轨混音范围无效')
    if type(sample_rate) is not int or not 8000 <= sample_rate <= 384000:
        raise ValueError('音轨采样率无效')
    inputs, filters, labels = [], [], []
    active = [(source, max(start, source.start), min(start+duration, source.end)) for source in sources
              if min(start+duration, source.end)-max(start, source.start) > 1e-9]
    multiple = len(active) > 1
    for source, left, right in active:
        index = input_offset+len(labels)
        # No -ss 0: keeping the original demux start preserves AAC priming.
        offset = source.source_start+left-source.start
        if offset > 1e-9: inputs += ['-ss', f'{offset:.9f}']
        inputs += ['-i', source.path]
        label = f'okk_audio_{len(labels)}'
        delay = max(0, round((left-start)*sample_rate))
        channel_format = ',aformat=sample_fmts=fltp:channel_layouts=stereo' if multiple else ''
        filters.append(f'[{index}:a:0]atrim=duration={right-left:.9f},asetpts=PTS-STARTPTS,'
                       f'aresample={sample_rate}{channel_format},adelay={delay}S:all=1[{label}]')
        labels.append(f'[{label}]')
    if labels:
        mix = (''.join(labels)+f'amix=inputs={len(labels)}:duration=longest:'
               'dropout_transition=0:normalize=0,' if len(labels) > 1 else labels[0])
        # A zero-latency hard bound keeps summed tracks from overflowing without
        # normalizing or changing single-track gain.
        clipping = (',aeval=clip(val(0)\\,-1\\,1)|clip(val(1)\\,-1\\,1),'
                    'aformat=channel_layouts=stereo' if multiple else '')
        filters.append(mix + f'apad,atrim=duration={duration:.9f}' + clipping + '[okk_audio_mix]')
    else:
        index = input_offset
        inputs += ['-f', 'lavfi', '-i', f'anullsrc=r={sample_rate}:cl=stereo']
        filters.append(f'[{index}:a:0]atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[okk_audio_mix]')
    return inputs, ';'.join(filters), '[okk_audio_mix]'


def render_audio_mix(project, output, ffmpeg, ffprobe, run_process=None, *, cancel_event=None,
                     start=0., duration=None, sample_rate=48000):
    """Render the identical track mix to PCM/FLAC for previews or inspection."""
    sources, _ = probe_audio_sources(ffprobe, audio_sources(project), cancel_event)
    duration = float(_get(project, 'duration', 0.))-start if duration is None else duration
    inputs, graph, label = build_audio_mix_args(sources, start, duration, sample_rate=sample_rate)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [str(ffmpeg), '-hide_banner', '-y', '-loglevel', 'error', *inputs,
               '-filter_complex', graph, '-map', label, '-vn', '-ar', str(sample_rate)]
    command += (['-c:a', 'pcm_s16le', '-rf64', 'auto'] if destination.suffix.lower() == '.wav'
                else ['-c:a', 'flac'])
    command += ['-t', f'{duration:.9f}', str(destination)]
    if run_process is None:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=0x08000000 if os.name == 'nt' else 0)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate(); process.communicate()
                from exporter_core import ExportCancelled
                raise ExportCancelled('音轨混音已取消')
            try:
                _, errors = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired: pass
        code, details = process.returncode, errors.decode('utf-8', 'replace')
    else:
        code, details = run_process(command, lambda _: None)
    if code or not destination.is_file() or destination.stat().st_size == 0:
        raise ValueError('音轨混音失败：' + str(details)[-2000:])
    return str(destination)
