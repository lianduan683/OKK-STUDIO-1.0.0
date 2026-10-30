"""Small cached codec probe for preview backend selection, never transcoding."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import subprocess


# These editing codecs do not use the D3D11VA/NVDEC texture path. MPV can
# decode them on CPU while retaining its D3D11 renderer and GPU grading.
SOFTWARE_EDITING_CODECS = frozenset({
    'dnxhd', 'prores', 'cfhd', 'qtrle', 'png', 'rawvideo', 'ffv1',
    'huffyuv', 'ffvhuff', 'utvideo', 'v210', 'v308', 'v408', 'v410',
})


@lru_cache(maxsize=256)
def _read_codec(ffprobe, media, size, mtime_ns):
    try:
        result = subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name', '-of', 'json', media],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=3,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode:
            return ''
        streams = json.loads(result.stdout).get('streams', [])
        return str(streams[0].get('codec_name', '')).lower() if streams else ''
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return ''  # The ordinary player load retains the final error path.


def software_preview_codec(mpv_executable, media_path):
    """Return a known software editing codec, or '' for the ordinary GPU path.

    Probe once per actual file version. Do not infer codecs from MOV/MXF
    extensions: those containers also commonly contain hardware H.264/HEVC.
    """
    try:
        media = Path(media_path).resolve()
        info = media.stat()
        ffprobe = Path(mpv_executable).resolve().with_name('ffprobe.exe')
        if not media.is_file() or not ffprobe.is_file():
            return ''
        codec = _read_codec(str(ffprobe), str(media), info.st_size, info.st_mtime_ns)
        return codec if codec in SOFTWARE_EDITING_CODECS else ''
    except (OSError, ValueError, TypeError):
        return ''
