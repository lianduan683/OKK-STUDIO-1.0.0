"""User-selected proxy formats and destinations, encoded on a worker thread."""
from dataclasses import dataclass, asdict
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from cache_publish import publish_completed
from proxy_cache import register_proxy
from proxy_media import validate_proxy_media
from quick_proxy import (_cancelled, _source_stamp, _probe_media_cancel, _run_encode,
                         _build_command, _proxy_modes, ProxyEncodeError)
from exporter_core import ExportCancelled

FORMATS = {'H.264 / MP4': 'h264', 'DNxHR LB / MOV': 'dnxhr_lb', 'ProRes Proxy / MOV': 'prores_proxy'}
SIZES = {'1920 × 1080': (1920, 1080), '1280 × 720': (1280, 720), '960 × 540': (960, 540)}


@dataclass(frozen=True)
class ProxyOptions:
    folder: str
    codec: str = 'h264'
    width: int = 1920
    height: int = 1080
    fps_limit: int = 30  # 0 retains the original frame rate.

    def validate(self):
        if self.codec not in FORMATS.values() or (self.width, self.height) not in SIZES.values():
            raise ValueError('请选择支持的代理格式与分辨率。')
        if type(self.fps_limit) is not int or self.fps_limit not in (0, 30):
            raise ValueError('请选择原始帧率或最高 30 fps。')
        if not isinstance(self.folder, str) or not self.folder.strip():
            raise ValueError('请选择代理保存目录。')


def build_proxy_command(ffmpeg, source, output, media, options, mode='cpu'):
    options.validate()
    rate = Fraction(str(media.fps)).limit_denominator(1_000_000)
    if options.fps_limit and rate > options.fps_limit: rate = Fraction(options.fps_limit)
    rate_text = f'{rate.numerator}/{rate.denominator}'
    if options.codec == 'h264':
        command = _build_command(ffmpeg, source, output, media, mode)
        index = command.index('-vf')+1
        graph = command[index].replace('1920', str(options.width)).replace('1080', str(options.height))
        old_rate = graph.split('fps=fps=', 1)[1].split(':', 1)[0]
        command[index] = graph.replace('fps=fps='+old_rate+':', 'fps=fps='+rate_text+':')
        command[command.index('-g')+1] = str(max(1, math.floor(float(rate))))
        return command
    codec = (['-c:v', 'dnxhd', '-profile:v', 'dnxhr_lb', '-pix_fmt', 'yuv422p']
             if options.codec == 'dnxhr_lb' else
             ['-c:v', 'prores_ks', '-profile:v', '0', '-pix_fmt', 'yuv422p10le'])
    graph = (f'setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,'
             f"scale=w='min(iw,{options.width})':h='min(ih,{options.height})':"
             'force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1')
    return [ffmpeg, '-hide_banner', '-y', '-nostdin', '-loglevel', 'error', '-xerror',
            '-stats_period', '0.25', '-progress', 'pipe:1', '-nostats', '-filter_threads', '2',
            '-threads', '2', '-i', source, '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn',
            '-vf', graph, '-fps_mode', 'cfr', *codec, '-threads', '2',
            '-c:a', 'pcm_s16le', '-ac', '2', '-ar', '48000',
            '-af', 'aresample=async=1:first_pts=0', '-t', f'{media.duration:.9f}',
            '-map_metadata', '0', '-movflags', '+faststart', str(output)]


def prepare_configured_proxy(source, ffmpeg, ffprobe, caps, options, cancel_event=None, progress=None):
    options.validate()
    _cancelled(cancel_event)
    stamp = _source_stamp(source)
    source = stamp['path']
    media = _probe_media_cancel(ffprobe, source, cancel_event=cancel_event)
    if not math.isfinite(media.duration) or media.duration <= 0 or media.width <= 0 or media.height <= 0:
        raise ValueError('原片没有有效的视频尺寸或时长。')
    settings = asdict(options); settings.pop('folder')
    key = hashlib.sha256(json.dumps({'source': stamp, 'settings': settings, 'version': 1},
                                    sort_keys=True).encode()).hexdigest()[:16]
    folder = Path(options.folder).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    suffix = '.mp4' if options.codec == 'h264' else '.mov'
    destination = folder/(Path(source).stem[:80]+'_'+key+'_proxy'+suffix)

    def validate(path):
        result = validate_proxy_media(ffprobe, source, str(path), media.duration, media.fps,
            probe=lambda binary, name: _probe_media_cancel(binary, name, cancel_event=cancel_event))
        codec = {'h264': 'h264', 'dnxhr_lb': 'dnxhd', 'prores_proxy': 'prores'}[options.codec]
        rate = min(media.fps, options.fps_limit) if options.fps_limit else media.fps
        if (result.video_codec != codec or result.width > options.width or result.height > options.height
                or abs(result.fps-rate) > .01 or (media.has_audio and not result.has_audio)):
            raise ValueError('代理格式、尺寸、帧率或音频与所选设置不一致。')
        return result

    if destination.is_file():
        try: validate(destination)
        except ExportCancelled: raise
        except (ValueError, OSError, RuntimeError): pass
        else:
            _cancelled(cancel_event)
            if _source_stamp(source) != stamp: raise ValueError('原片在读取期间发生变化。')
            register_proxy(source, str(destination))
            if progress: progress(100., '复用已验证代理')
            return str(destination)
    descriptor, name = tempfile.mkstemp(prefix='.'+key+'.', suffix='.pending'+suffix, dir=folder)
    os.close(descriptor)
    temporary = Path(name)
    try:
        modes = _proxy_modes(caps) if options.codec == 'h264' else ['cpu']
        for index, mode in enumerate(modes):
            _cancelled(cancel_event)
            try:
                _run_encode(build_proxy_command(ffmpeg, source, temporary, media, options, mode),
                            media.duration, cancel_event=cancel_event, progress=progress, phase='生成代理')
                validate(temporary)
                break
            except (ProxyEncodeError, ValueError):
                if index == len(modes)-1: raise
        _cancelled(cancel_event)
        if _source_stamp(source) != stamp: raise ValueError('原片在转码期间发生变化，请重新生成代理。')
        publish_completed(temporary, destination, cancel_event=cancel_event)
        if _source_stamp(source) != stamp: raise ValueError('原片已变化，未绑定旧版本代理，请重试。')
        register_proxy(source, str(destination))
        if progress: progress(100., '代理已完成')
        return str(destination)
    finally:
        temporary.unlink(missing_ok=True)
