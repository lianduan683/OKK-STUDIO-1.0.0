"""Small, cancellable library demonstrations using the actual effect filters."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from PIL import Image, ImageDraw

from creative_core import ColorGrade, grade_filter
from creative_render import TransitionSegment, build_transition_command
from effects_library import asset_path, transition_mask_path
from exporter_core import CREATE_NO_WINDOW, ExportCancelled, ExportSettings, probe_media
from multicam_core import CameraClip, Cut, MulticamProject

WIDTH, HEIGHT, FPS = 480, 270, 30
PREVIEW_KINDS = frozenset(('grade', 'reset', 'transition', 'mask', 'overlay'))


@dataclass(frozen=True)
class EffectPreviewResult:
    path: str
    duration: float
    title: str
    width: int = WIDTH
    height: int = HEIGHT


def _check_cancel(cancel):
    if cancel.is_set():
        raise ExportCancelled('已取消效果预览')


def _run(command, cancel, folder, *, timeout=90):
    _check_cancel(cancel)
    with tempfile.TemporaryFile(dir=folder) as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=errors, creationflags=CREATE_NO_WINDOW)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                _check_cancel(cancel)
                if time.monotonic() > deadline:
                    raise ValueError('效果预览处理超时，请重试。')
                cancel.wait(.04)
            _check_cancel(cancel)
            if process.returncode:
                errors.seek(0)
                raise ValueError('效果预览生成失败：' + errors.read().decode('utf-8', 'replace')[-1200:])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)


def _sample_picture(path, scene):
    """Original, procedural A/B scenery; no user media or external assets."""
    picture = Image.new('RGB', (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(picture)
    top, bottom = ((40, 95, 155), (247, 171, 105)) if scene == 0 else ((15, 25, 61), (62, 99, 136))
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        color = tuple(round(a + (b - a) * ratio) for a, b in zip(top, bottom))
        draw.line((0, y, WIDTH, y), fill=color)
    if scene == 0:
        draw.ellipse((325, 40, 391, 106), fill='#ffe3a0')
        draw.polygon([(0, 190), (87, 82), (182, 196), (294, 111), (480, 213), (480, 270), (0, 270)], fill='#526778')
        draw.polygon([(0, 241), (132, 133), (274, 249), (399, 167), (480, 232), (480, 270), (0, 270)], fill='#293e4c')
        draw.rectangle((0, 241, WIDTH, HEIGHT), fill='#83a0a3')
        for y in range(247, 270, 7):
            draw.line((0, y, WIDTH, y), fill='#afc4bf')
    else:
        draw.ellipse((73, 32, 111, 70), fill='#e5ecf4')
        for i in range(28):
            x, y = (i * 83 + 29) % WIDTH, (i * 37 + 11) % 125
            draw.ellipse((x, y, x + 2, y + 2), fill='#cdd9e8')
        for i in range(11):
            x, roof = i * 48 - 10, 127 + (i * 29) % 75
            draw.rectangle((x, roof, x + 40, HEIGHT), fill='#142536')
            for wy in range(roof + 9, 260, 18):
                for wx in range(x + 7, x + 35, 12):
                    draw.rectangle((wx, wy, wx + 4, wy + 6), fill='#e4bc78' if (wx + wy) % 3 else '#80c7d5')
    draw.rounded_rectangle((14, 14, 45, 45), radius=7, fill='#f0f2f5')
    draw.text((26, 24), 'A' if scene == 0 else 'B', fill='#263242')
    picture.save(path)


def render_effect_preview(item, strength, duration, ffmpeg, ffprobe, folder, *, cancel_event=None):
    """Return a cached demo movie. This API never receives an editing project."""
    cancel = cancel_event or threading.Event()
    _check_cancel(cancel)
    if item.kind not in PREVIEW_KINDS:
        raise ValueError('此工具没有独立示例预览，请打开对应设置。')
    strength, duration = float(strength), float(duration)
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('预览强度必须在 0% 至 100% 之间。')
    if not math.isfinite(duration) or not .05 <= duration <= 5:
        raise ValueError('转场时长请输入 0.05–5 秒。')
    resource = transition_mask_path(item.asset) if item.kind == 'mask' else asset_path(item.asset) if item.kind == 'overlay' else ''
    stamp = None
    if resource:
        info = Path(resource).stat()
        stamp = (resource, info.st_size, info.st_mtime_ns)
    payload = dict(version=1, key=item.key, kind=item.kind, action=item.action,
                   changes=item.changes, strength=strength, duration=duration, resource=stamp)
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / (key + '.mp4')
    if output.is_file():
        try:
            media = probe_media(ffprobe, str(output))
            if media.width == WIDTH and media.height == HEIGHT and media.duration > 0:
                return EffectPreviewResult(str(output), media.duration, item.name)
        except (ValueError, OSError, RuntimeError):
            pass
    transition_frames = max(2, math.floor(duration * FPS + .5))
    effect_duration = transition_frames / FPS
    total = effect_duration + 1.2 if item.kind in ('transition', 'mask') else 3.
    base = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y']
    encode = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '2']
    with tempfile.TemporaryDirectory(prefix='effect-preview-', dir=folder) as temporary:
        work = Path(temporary)
        count = 2 if item.kind in ('transition', 'mask') else 1
        movies = []
        for scene in range(count):
            picture, movie = work / f'scene-{scene}.png', work / f'scene-{scene}.mp4'
            _sample_picture(picture, scene)
            _run(base + ['-loop', '1', '-framerate', str(FPS), '-i', str(picture), '-an',
                         '-t', str(total), '-pix_fmt', 'yuv420p', *encode, str(movie)], cancel, work)
            movies.append(str(movie))
        rendered = work / 'preview.mp4'
        if item.kind in ('transition', 'mask'):
            project = MulticamProject(clips=[CameraClip(str(i), i + 1, path, 0., total)
                                             for i, path in enumerate(movies)],
                                      cuts=[Cut(0., 1), Cut(effect_duration / 2, 2)], fps=FPS)
            job = TransitionSegment(camera=1, path=movies[0], source_start=0., start=0.,
                duration=effect_duration, frames=transition_frames, other_camera=2,
                other_path=movies[1], other_source_start=0., kind=item.action if item.kind == 'transition' else 'fade',
                full_frames=transition_frames, trim_start_frames=0, full_start=0., mask_path=resource)
            transition = work / 'transition.mp4'
            command = build_transition_command(job, project, base, ExportSettings(codec='libx264', audio='none'),
                WIDTH, HEIGHT, 'yuv420p', str(FPS), encode, transition)
            _run(command, cancel, work)
            _run(base + ['-i', str(transition), '-vf', 'tpad=start_duration=0.6:stop_duration=0.6:start_mode=clone:stop_mode=clone',
                         '-an', *encode, str(rendered)], cancel, work)
        elif item.kind == 'overlay':
            from library_overlay import LibraryOverlayRange, _command
            media = probe_media(ffprobe, movies[0])
            record = LibraryOverlayRange(0., total, 1, 'demo', resource, item.asset.kind, strength,
                                        0., total, str(rendered), '0' * 64, WIDTH, HEIGHT, FPS)
            command, _ = _command(ffmpeg, movies[0], 0., record, rendered, media, 'libx264')
            _run(command, cancel, work)
        else:
            from effects_browser import preset_grade
            grade = preset_grade(item, ColorGrade(), strength)
            effect = grade_filter(grade)
            filters = (effect + ',' if effect else '') + 'format=yuv420p'
            _run(base + ['-i', movies[0], '-vf', filters, '-an', *encode, str(rendered)], cancel, work)
        _check_cancel(cancel)
        media = probe_media(ffprobe, str(rendered))
        if media.width != WIDTH or media.height != HEIGHT or media.duration <= 0:
            raise ValueError('效果预览尺寸或时长校验失败。')
        _check_cancel(cancel)
        os.replace(rendered, output)
    return EffectPreviewResult(str(output), media.duration, item.name)
