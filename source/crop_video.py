"""Bounded continuous video playback for the crop canvas, with no random sampling."""
from __future__ import annotations

from collections import deque
import math
import queue
import subprocess
import threading
import time

from crop_frame import ppm_pixels
from exporter_core import CREATE_NO_WINDOW


def _read_picture(stream):
    first = stream.readline(64)
    if not first: return None
    if first.strip() != b'P6': raise ValueError('视频预览帧格式无效。')
    dimensions = stream.readline(64)
    maximum = stream.readline(64)
    width, height = map(int, dimensions.split())
    if not 1 <= width <= 1280 or not 1 <= height <= 900 or maximum.strip() != b'255':
        raise ValueError('视频预览帧尺寸无效。')
    remaining = width*height*3
    parts = []
    while remaining:
        data = stream.read(remaining)
        if not data: raise ValueError('视频预览帧不完整。')
        parts.append(data)
        remaining -= len(data)
    return ppm_pixels(first+dimensions+maximum+b''.join(parts))


class CropVideoStream:
    """One sequential decoder, one latest-frame slot; close stops only this decoder."""

    def __init__(self, ffmpeg, path, start, duration, *, fps=15):
        if not all(math.isfinite(v) and v >= 0 for v in (start, duration)) or duration <= 0:
            raise ValueError('视频预览播放范围无效。')
        self.ffmpeg, self.path = str(ffmpeg), str(path)
        self.start, self.duration, self.fps = start, duration, fps
        self.latest = queue.Queue(maxsize=1)
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.error = ''
        self.frames = 0
        self._process = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name='okk-crop-video', daemon=True)
        self._thread.start()

    def _run(self):
        process = None
        errors = deque(maxlen=12)
        try:
            command = [self.ffmpeg, '-hide_banner', '-v', 'error', '-threads', '4',
                '-ss', f'{self.start:.9f}', '-i', self.path,
                '-an', '-sn', '-dn', '-t', f'{self.duration:.9f}', '-filter_threads', '1',
                '-vf', f'fps={self.fps},scale=1280:900:force_original_aspect_ratio=decrease:'
                       'force_divisible_by=2:reset_sar=1,format=rgb24',
                '-c:v', 'ppm', '-f', 'image2pipe', 'pipe:1']
            with self._lock:
                if self.cancel.is_set(): return
                process = self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
            def read_errors():
                try:
                    for line in process.stderr: errors.append(line.decode('utf-8', 'replace').strip())
                except (OSError, ValueError):
                    pass
            threading.Thread(target=read_errors, name='okk-crop-video-log', daemon=True).start()
            anchor = None
            while not self.cancel.is_set():
                pixels = _read_picture(process.stdout)
                if pixels is None: break
                if anchor is None: anchor = time.monotonic()
                if self.cancel.wait(max(0., anchor+self.frames/self.fps-time.monotonic())): break
                seconds = self.start+self.frames/self.fps
                self.frames += 1
                if self.latest.full():
                    try: self.latest.get_nowait()
                    except queue.Empty: pass
                try: self.latest.put_nowait((seconds, pixels))
                except queue.Full: pass
            code = process.wait(timeout=2)
            if code and not self.cancel.is_set():
                raise ValueError('视频预览播放失败：'+' '.join(errors)[-500:])
        except Exception as error:
            if not self.cancel.is_set(): self.error = str(error)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                if process.stdout: process.stdout.close()
                if process.stderr: process.stderr.close()
            self.done.set()

    def read(self):
        try: return self.latest.get_nowait()
        except queue.Empty: return None

    def close(self):
        self.cancel.set()
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                try: process.kill()
                except OSError: pass
        self._thread.join(timeout=.5)
