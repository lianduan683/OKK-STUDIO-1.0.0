"""Bounded, cancellable envelope decoding from original media audio.

Peaks represent real decoded samples. The worker never touches Tk and a
missing audio stream returns an empty result rather than a synthetic signal.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import subprocess
import tempfile

import numpy as np

from exporter_core import CREATE_NO_WINDOW


SAMPLE_RATE = 8000
SAMPLE_CHANNELS = 2
BLOCK_SIZE = 256
PEAK_RATE = SAMPLE_RATE / BLOCK_SIZE
MAX_PEAKS = 2_000_000


@dataclass(frozen=True)
class Waveform:
    peaks: np.ndarray
    rate: float = PEAK_RATE
    duration: float = 0.
    state: str = 'ready'


def waveform_key(path):
    """Invalidate a cached envelope when the original media is replaced."""
    try:
        source = Path(path).resolve()
        stat = source.stat()
        if not source.is_file(): return None
        return str(source), stat.st_size, stat.st_mtime_ns
    except (OSError, ValueError):
        return None


def _cache_path(key):
    try:
        from cache_paths import cache_dir
        digest = hashlib.sha256(repr((key, SAMPLE_RATE, SAMPLE_CHANNELS, BLOCK_SIZE)).encode('utf-8')).hexdigest()
        return cache_dir('audio-waveforms') / (digest + '.npz')
    except (OSError, ValueError):
        return None


def _read_cache(path):
    if path is None or not path.is_file(): return None
    try:
        with np.load(path, allow_pickle=False) as data:
            peaks = np.asarray(data['peaks'], dtype=np.float32)
            duration = float(data['duration'])
            if (peaks.ndim != 1 or not 0 < len(peaks) <= MAX_PEAKS
                    or not np.isfinite(peaks).all() or not math.isfinite(duration) or duration <= 0):
                return None
            return Waveform(peaks, duration=duration)
    except (OSError, ValueError, KeyError, EOFError):
        return None


def decode_waveform(ffmpeg, path, cancel, *, process_started=None, process_finished=None,
                    use_cache=True):
    """Decode low-resolution PCM without retaining the full recording.

    Hooks let the owner terminate all live FFmpeg processes immediately during
    workspace shutdown, including a process currently blocked on its pipe.
    """
    if cancel.is_set(): return None
    key = waveform_key(path)
    if key is None: return Waveform(np.empty(0, dtype=np.float32), state='offline')
    cache = _cache_path(key) if use_cache else None
    cached = _read_cache(cache)
    if cached is not None: return cached
    process = None
    error_log = tempfile.TemporaryFile()
    blocks = []
    peak_count = 0
    count = 0
    pending = b''
    try:
        process = subprocess.Popen([str(ffmpeg), '-nostdin', '-v', 'error', '-i', str(path),
            '-map', '0:a:0', '-vn', '-sn', '-dn', '-ac', str(SAMPLE_CHANNELS), '-ar', str(SAMPLE_RATE),
            '-f', 'f32le', 'pipe:1'], stdout=subprocess.PIPE, stderr=error_log,
            creationflags=CREATE_NO_WINDOW)
        if process_started is not None: process_started(process)
        assert process.stdout is not None
        while not cancel.is_set():
            chunk = process.stdout.read(BLOCK_SIZE * SAMPLE_CHANNELS * 4 * 128)
            if not chunk: break
            pending += chunk
            stride = BLOCK_SIZE * SAMPLE_CHANNELS * 4
            complete = len(pending) // stride * stride
            if complete:
                values = np.frombuffer(pending[:complete], dtype='<f4').reshape(-1, BLOCK_SIZE*SAMPLE_CHANNELS)
                blocks.append(np.max(np.abs(values), axis=1))
                peak_count += len(values)
                count += complete // (4*SAMPLE_CHANNELS)
                pending = pending[complete:]
                if peak_count > MAX_PEAKS:
                    process.terminate()
                    return Waveform(np.empty(0, dtype=np.float32), state='too_long')
        if cancel.is_set():
            if process.poll() is None: process.terminate()
            return None
        if pending:
            values = np.frombuffer(pending[:len(pending)//4*4], dtype='<f4')
            if len(values):
                blocks.append(np.array([np.max(np.abs(values))], dtype=np.float32))
                count += len(values) // SAMPLE_CHANNELS
        code = process.wait(timeout=10)
        error_log.seek(0)
        errors = error_log.read(16384).decode('utf-8', 'replace')
        if code or not blocks:
            absent = ('matches no streams' in errors or 'does not contain any stream' in errors
                      or 'Stream map' in errors)
            return Waveform(np.empty(0, dtype=np.float32), state='no_audio' if absent else 'unavailable')
        peaks = np.nan_to_num(np.concatenate(blocks).astype(np.float32), nan=0., posinf=1., neginf=0.)
        result = Waveform(peaks, duration=count / SAMPLE_RATE)
        if cache is not None and not cancel.is_set():
            try:
                # A cache is disposable; write atomically so two workspaces can share it.
                import uuid
                temporary = cache.with_name(cache.stem + '-' + uuid.uuid4().hex + '.npz')
                np.savez_compressed(temporary, peaks=peaks, duration=result.duration)
                temporary.replace(cache)
            except OSError:
                pass
        return result
    except (OSError, subprocess.SubprocessError, ValueError):
        return None if cancel.is_set() else Waveform(np.empty(0, dtype=np.float32), state='unavailable')
    finally:
        if process is not None:
            if process.poll() is None:
                try: process.terminate()
                except OSError: pass
            try: process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try: process.kill()
                except OSError: pass
            for stream in (process.stdout, process.stderr):
                if stream is not None: stream.close()
            if process_finished is not None: process_finished(process)
        error_log.close()


def envelope_points(waveform, source_start, source_end, columns):
    """Maximum sample peak for each visible pixel column, preserving transients."""
    columns = max(1, min(2048, int(columns)))
    if waveform.state != 'ready' or not len(waveform.peaks) or source_end <= source_start:
        return np.empty(0, dtype=np.float32)
    edges = np.linspace(max(0., source_start)*waveform.rate,
                        max(0., source_end)*waveform.rate, columns+1)
    result = np.zeros(columns, dtype=np.float32)
    peaks = waveform.peaks
    for index in range(columns):
        start = max(0, min(len(peaks), int(edges[index])))
        end = max(start+1, int(math.ceil(edges[index+1])))
        if start < len(peaks): result[index] = min(1., float(np.max(peaks[start:min(len(peaks), end)])))
    return result
