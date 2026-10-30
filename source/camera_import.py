"""Atomic batch imports: every selected video becomes one distinct camera."""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

from exporter_core import ExportCancelled, MediaInfo, probe_media


VIDEO_SUFFIXES = frozenset({'.mp4', '.mov', '.mkv', '.mxf', '.avi', '.mts', '.m2ts',
                            '.webm', '.mpg', '.mpeg', '.m4v', '.wmv', '.ts', '.m2v'})
DERIVED_DIRECTORIES = frozenset({'proxy', 'proxies', 'proxymedia', 'proxy media',
                                'cacheclip', 'optimized media', 'optimizedmedia'})


def _natural_key(path):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r'(\d+)', os.fspath(path)))


def collect_video_groups(entries, *, separate_files=False, separate_folder_files=False,
                         cancel_event=None, max_files=50000):
    """A folder is one camera; its naturally ordered parts remain separate clips.

    Walk on the import worker, never the Tk thread. Do not follow directory
    links or include non-video sidecars. A failed folder leaves the project intact.
    """
    groups, loose, seen = [], [], set()
    examined = 0

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled('已取消导入机位')

    def accept(path):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return False
        seen.add(key)
        return True

    def flush_loose():
        if loose:
            groups.append(sorted(loose, key=_natural_key))
            loose.clear()

    for value in entries:
        check()
        path = Path(value)
        if not path.exists():
            raise ValueError(f'找不到所选文件或文件夹：{path}')
        if path.is_dir():
            flush_loose()
            videos = []
            def walk_error(error):
                raise ValueError(f'无法读取文件夹：{error.filename}') from error
            for base, directories, files in os.walk(path, followlinks=False, onerror=walk_error):
                check()
                directories[:] = sorted((name for name in directories
                    if name.casefold() not in DERIVED_DIRECTORIES
                    and not Path(base, name).is_symlink()
                    and not getattr(Path(base, name), 'is_junction', lambda: False)()), key=_natural_key)
                for name in files:
                    check()
                    examined += 1
                    if examined > max_files:
                        raise ValueError(f'文件夹超过 {max_files} 个文件，请选择具体的机位文件夹。')
                    video = Path(base, name)
                    if video.suffix.casefold() in VIDEO_SUFFIXES and video.is_file():
                        videos.append(video)
            if not videos:
                raise ValueError(f'文件夹内没有支持的视频：{path.name}')
            videos = [str(video) for video in sorted(videos, key=lambda p: _natural_key(p.relative_to(path)))
                      if accept(video)]
            if separate_folder_files:
                groups.extend([video] for video in videos)
            elif videos:
                groups.append(videos)
        elif path.is_file():
            if not accept(path):
                continue
            if separate_files:
                groups.append([str(path)])
            else:
                loose.append(str(path))
        else:
            raise ValueError(f'无法读取所选文件：{path}')
    flush_loose()
    check()
    return groups


def grouped_import(ffprobe, entries, existing_cameras, *, separate_files=False,
                   separate_folder_files=False, camera=None, cancel_event=None, progress=None):
    groups = collect_video_groups(entries, separate_files=separate_files,
        separate_folder_files=separate_folder_files, cancel_event=cancel_event)
    if not groups:
        return []
    if camera is not None:
        if type(camera) is not int or camera <= 0:
            raise ValueError('机位编号必须为正整数')
        groups = [[source for group in groups for source in group]]
        cameras = [camera]
    else:
        cameras = assign_camera_ids(existing_cameras, len(groups))
    # Probe the entire batch before publishing a single undoable UI operation.
    flat = [source for group in groups for source in group]
    infos = [info for _, info in batch_import(ffprobe, flat, (),
        cancel_event=cancel_event, progress=progress)]
    result, index = [], 0
    for camera_id, group in zip(cameras, groups):
        result.append((camera_id, infos[index:index + len(group)]))
        index += len(group)
    return result


def assign_camera_ids(existing, count: int) -> list[int]:
    """Allocate the lowest unused positive ids without a nine-camera cap."""
    if type(count) is not int or not 0 <= count <= 100000:
        raise ValueError('导入数量必须是 0 到 100000 之间的整数')
    used = set(existing)
    if any(type(value) is not int or value <= 0 for value in used):
        raise ValueError('已有机位编号必须为正整数')
    result = []
    candidate = 1
    while len(result) < count:
        if candidate not in used:
            result.append(candidate)
            used.add(candidate)
        candidate += 1
    return result


def batch_import(ffprobe, paths, existing_cameras, *, cancel_event=None, progress=None) -> list[tuple[int, MediaInfo]]:
    """Probe and validate the complete selection before returning any imports.

    This function owns no project, so a bad/cancelled file cannot leave a
    half-imported camera list.  Existing ids and the input source files are
    never changed. ``progress(percent, text)`` runs on the importing worker.
    """
    sources = [os.fspath(path) for path in paths]
    cameras = assign_camera_ids(existing_cameras, len(sources))
    if not sources:
        return []
    if not ffprobe:
        raise ValueError('未找到 ffprobe，无法读取视频信息')

    def cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled('已取消批量导入机位')

    for source in sources:
        cancelled()
        if not source or any(character in source for character in '\x00\r\n') or not Path(source).is_file():
            raise ValueError('无法读取所选视频：'+str(source))
    result = []
    for index, (camera, source) in enumerate(zip(cameras, sources)):
        cancelled()
        if progress: progress(index/len(sources)*100, f'读取机位 {index+1}/{len(sources)}：{Path(source).name}')
        try:
            info = probe_media(str(ffprobe), source)
        except Exception as error:
            cancelled()
            raise ValueError(f'无法读取视频 {Path(source).name}：{error}') from error
        if (info.width <= 0 or info.height <= 0 or not math.isfinite(info.duration)
                or info.duration <= 0):
            raise ValueError(f'所选文件没有可读取的视频画面或时长：{Path(source).name}')
        result.append((camera, info))
        if progress: progress((index+1)/len(sources)*100, f'已读取机位 {index+1}/{len(sources)}')
    cancelled()
    return result
