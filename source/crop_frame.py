"""Bounded local source-frame decoding and conservative black-border sampling."""
from __future__ import annotations

import math
import json
from pathlib import Path
import subprocess
import time

import numpy as np
from exporter_core import CREATE_NO_WINDOW, _media_probe_command, _media_info_from_payload


class CropCancelled(Exception):
    pass


def ppm_pixels(data):
    # FFmpeg emits P6, but accept comments/whitespace in other PPM producers.
    import re
    header = re.match(rb'P6\s+(?:#[^\n]*\n\s*)*(\d+)\s+(\d+)\s+255[\r]?\n', data)
    if header is None:
        raise ValueError('无法读取裁切预览帧。')
    width, height = map(int, header.groups())
    if not 1 <= width <= 1280 or not 1 <= height <= 900:
        raise ValueError('裁切预览尺寸超出限制。')
    pixels = np.frombuffer(data, dtype=np.uint8, offset=header.end())
    if pixels.size != width*height*3:
        raise ValueError('裁切预览帧不完整。')
    return pixels.reshape(height, width, 3).copy()


def pixels_ppm(pixels):
    height, width = pixels.shape[:2]
    return f'P6\n{width} {height}\n255\n'.encode('ascii') + np.ascontiguousarray(pixels, dtype=np.uint8).tobytes()


def resize_pixels(pixels, width, height):
    """Bounded display-only sampling. Export continues using the source media."""
    sw, sh = pixels.shape[1], pixels.shape[0]
    xs = np.minimum(sw-1, np.arange(max(1, int(width)))*sw//max(1, int(width)))
    ys = np.minimum(sh-1, np.arange(max(1, int(height)))*sh//max(1, int(height)))
    return pixels[ys[:, None], xs[None, :]].copy()


def _run_crop_command(command, cancel, timeout, label):
    if cancel.is_set(): raise CropCancelled()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=CREATE_NO_WINDOW)
    deadline = time.monotonic()+timeout
    try:
        while True:
            if cancel.is_set(): raise CropCancelled()
            if time.monotonic() > deadline:
                raise ValueError(label+'超时，请换一帧重试。')
            try:
                output, error = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            detail = error.decode('utf-8', 'replace').strip()[-500:]
            raise ValueError(label+'失败：'+detail)
        return output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        if process.stdout: process.stdout.close()
        if process.stderr: process.stderr.close()


def decode_frame(ffmpeg, path, position, cancel, *, timeout=15):
    if not math.isfinite(position) or position < 0:
        raise ValueError('裁切预览位置无效。')
    command = [str(ffmpeg), '-hide_banner', '-v', 'error', '-threads', '4',
        '-ss', f'{position:.9f}', '-i', str(path), '-an', '-sn', '-dn',
        '-filter_threads', '1', '-vf',
        'scale=1280:900:force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1,format=rgb24',
        '-frames:v', '1', '-c:v', 'ppm', '-f', 'image2pipe', 'pipe:1']
    return ppm_pixels(_run_crop_command(command, cancel, timeout, '读取裁切画面'))


def _crop_media_info(ffprobe, path, cancel):
    command = _media_probe_command(str(ffprobe), str(path))
    entries = command.index('-show_entries')+1
    command[entries] = command[entries].replace('stream_tags=timecode,alpha_mode:',
        'stream_tags=timecode,alpha_mode,rotate:')+':stream_side_data=rotation'
    payload = json.loads(_run_crop_command(command, cancel, 4, '校验预览媒体'))
    media = _media_info_from_payload(str(path), payload)
    stream = next(s for s in payload['streams'] if s.get('codec_type') == 'video'
                  and not (s.get('disposition') or {}).get('attached_pic'))
    rotation = next((float(item['rotation']) for item in stream.get('side_data_list', []) if 'rotation' in item),
                    float((stream.get('tags') or {}).get('rotate', 0)))
    try:
        numerator, denominator = map(float, media.sample_aspect_ratio.replace('/', ':').split(':'))
        sar = numerator/denominator
        if not math.isfinite(sar) or sar <= 0: sar = 1.
    except (ValueError, ZeroDivisionError):
        sar = 1.
    aspect = media.width*sar/media.height
    if round(rotation) % 180 == 90: aspect = 1/aspect
    if not math.isfinite(aspect) or aspect <= 0 or media.duration <= 0:
        raise ValueError('预览媒体尺寸或时长无效。')
    return media, aspect


def choose_preview_source(ffprobe, source, proxy, cancel):
    """Use only a complete, matching-display-aspect proxy for source framing.

    Source trims are addressed in original source seconds, so proxies must
    cover the complete untrimmed recording. Treatments/caches are passed by
    the caller without a proxy candidate. This choice never alters export.
    """
    source = str(source)
    if (not proxy or not ffprobe or not Path(proxy).is_file()
            or Path(proxy).resolve() == Path(source).resolve()):
        return source, '原片预览'
    try:
        original, aspect = _crop_media_info(ffprobe, source, cancel)
        candidate, candidate_aspect = _crop_media_info(ffprobe, proxy, cancel)
        rates = [value for value in (original.fps, candidate.fps) if math.isfinite(value) and value > 0]
        tolerance = max(.12, *(2/value for value in rates)) if rates else .12
        if abs(original.duration-candidate.duration) > tolerance:
            return source, '代理时长不符，使用原片预览'
        if not math.isclose(aspect, candidate_aspect, rel_tol=.002):
            return source, '代理画幅不符，使用原片预览'
        return str(proxy), '代理预览（导出使用原片）'
    except CropCancelled:
        raise
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, ZeroDivisionError, StopIteration):
        return source, '代理无法校验，使用原片预览'


def content_bounds(frames):
    """Union content across samples, so temporary dark edges cannot overcrop.

    Only almost uniformly black contiguous edge rows/columns are removed.
    Blank/dark samples are ignored; aggressive or unstable detections fall
    back to the full image. This detects borders, not people or subjects.
    """
    if not frames: raise ValueError('缺少自动裁切抽样画面。')
    height, width = frames[0].shape[:2]
    boxes = []
    for pixels in frames:
        if pixels.shape != (height, width, 3):
            return 0., 0., float(width), float(height)
        bright = np.max(pixels, axis=2) > 20
        if np.mean(bright) < .05: continue
        rows = np.flatnonzero(np.mean(bright, axis=1) >= .01)
        columns = np.flatnonzero(np.mean(bright, axis=0) >= .01)
        if not len(rows) or not len(columns): continue
        box = (int(columns[0]), int(rows[0]), int(columns[-1])+1, int(rows[-1])+1)
        if ((box[2]-box[0]) < width*.5 or (box[3]-box[1]) < height*.5):
            return 0., 0., float(width), float(height)
        boxes.append(box)
    if not boxes: return 0., 0., float(width), float(height)
    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
    right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
    # A single-pixel decoder/rounding fringe is not treated as a real bar.
    if left < 3: left = 0
    if top < 3: top = 0
    if width-right < 3: right = width
    if height-bottom < 3: bottom = height
    return float(left), float(top), float(right), float(bottom)
