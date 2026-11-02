"""Verify the bundled CUDA/Vulkan device-to-device export bridge.

All contexts descend from one explicitly selected CUDA device. Initializing
Vulkan separately can import a recycled texture's CUDA semaphores into the
wrong context and fail after the first few frames. Deriving Vulkan also matches
the physical device by UUID and disables unsupported multiplane CUDA imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import threading


@dataclass(frozen=True, slots=True)
class ResidentSupport:
    supported: bool
    reason: str
    formats: tuple[str, ...] = ()
    frames: int = 0


_cache: dict[tuple, ResidentSupport] = {}
_lock = threading.Lock()
_FRAMES = 32


def global_options(gpu_index=0):
    if type(gpu_index) is not int or gpu_index < 0:
        raise ValueError('GPU index must be a nonnegative integer')
    return ('-init_hw_device', f'cuda=okk_cu:{gpu_index}',
            '-init_hw_device', 'vulkan=okk_vk@okk_cu',
            '-filter_hw_device', 'okk_vk')


def input_options():
    return ('-hwaccel', 'cuda', '-hwaccel_device', 'okk_cu',
            '-hwaccel_output_format', 'cuda')


def _probe(path, gpu_index):
    # A single process exercises both frame depths and enough pool reuse to
    # expose external-semaphore/context failures; no media or cache is written.
    chains = ['[0:v]split=2[in8][in10]']
    for depth, surface in ((8, 'nv12'), (10, 'p010le')):
        chains.append(
            f'[in{depth}]format={surface},hwupload=derive_device=cuda,'
            f'scale_cuda=w=160:h=90:format={surface},'
            'hwupload=derive_device=vulkan,'
            f'libplacebo=w=160:h=90:format={surface}:brightness=0.02:'
            'deband=0:peak_detect=0:apply_filmgrain=0:apply_dolbyvision=0:'
            f'dithering=none,hwupload=derive_device=cuda[out{depth}]')
    command = [path, '-hide_banner', '-nostdin', '-v', 'error',
               *global_options(gpu_index), '-filter_complex_threads', '1',
               '-f', 'lavfi', '-i', f'testsrc2=s=320x180:r=30:d={_FRAMES/30:.12f}',
               '-filter_complex', ';'.join(chains), '-map', '[out8]', '-map', '[out10]',
               '-frames:v', str(_FRAMES), '-an', '-c:v', 'hevc_nvenc', '-preset', 'p1',
               '-bf', '0', '-gpu', str(gpu_index), '-f', 'framecrc', 'pipe:1']
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=12, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return ResidentSupport(False, f'GPU interop probe failed: {type(exc).__name__}')
    if result.returncode:
        error = result.stderr.decode('utf-8', 'replace').strip()
        return ResidentSupport(False, 'GPU interop probe failed: ' + (error[:1200] or str(result.returncode)))
    counts = [0, 0]
    for line in result.stdout.decode('ascii', 'replace').splitlines():
        fields = line.split(',')
        if len(fields) >= 6 and fields[0].strip() in ('0', '1'):
            counts[int(fields[0])] += 1
    if counts != [_FRAMES, _FRAMES]:
        return ResidentSupport(False, f'GPU interop probe returned incomplete frames: {counts}')
    return ResidentSupport(True, 'CUDA/Vulkan GPU transfers and NVENC verified for NV12 and P010.',
                           ('nv12', 'p010le'), _FRAMES)


def report(ffmpeg_path, gpu_index=0):
    """Return a cached, nonthrowing runtime result for this binary and GPU."""
    if type(gpu_index) is not int or gpu_index < 0:
        return ResidentSupport(False, 'Invalid GPU index')
    try:
        path = Path(ffmpeg_path).resolve(strict=True)
        stat = path.stat()
        if not path.is_file():
            return ResidentSupport(False, 'FFmpeg executable is not a file')
        key = (str(path), stat.st_size, stat.st_mtime_ns, gpu_index)
    except (OSError, TypeError, ValueError):
        return ResidentSupport(False, 'FFmpeg executable is unavailable')
    with _lock:
        if key not in _cache:
            _cache[key] = _probe(str(path), gpu_index)
        return _cache[key]


def supported(ffmpeg_path, gpu_index=0):
    return report(ffmpeg_path, gpu_index).supported
