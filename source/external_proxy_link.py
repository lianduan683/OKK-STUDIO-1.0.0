"""Conservative filename matching and cancellable validation of existing proxies."""
from __future__ import annotations

import os
from pathlib import Path
import re
import unicodedata

from exporter_core import ExportCancelled
from proxy_media import validate_proxy_media
from quick_proxy import _probe_media_cancel

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.mxf', '.avi', '.mts', '.m2ts', '.webm',
                    '.m4v', '.lrv', '.mpg', '.mpeg', '.wmv', '.ts'}
# Only strip explicit proxy/size markers at filename boundaries. Keep every
# intermediate spelling so an original actually named A_proxy remains visible
# as an ambiguity instead of accidentally binding A_proxy_proxy to A.
_MARKER = r'(?:proxy|proxies|prx|代理|低清|预览|(?:1920[x×]1080|1280[x×]720)|(?:1080|720|540|360)p?)'
_SUFFIX = re.compile(r'(?:[ _.-]+' + _MARKER + r'|[（(\[]' + _MARKER + r'[）)\]])$', re.I)
_PREFIX = re.compile(r'^(?:' + _MARKER + r'[ _.-]+|[（(\[]' + _MARKER + r'[）)\]][ _.-]*)', re.I)
_PROXY_DIRECTORIES = {'proxy', 'proxies', 'proxymedia', '代理', '代理文件', '代理媒体', '1080p', '720p'}


def identity(path):
    return os.path.normcase(os.path.abspath(path))


def _name(value):
    return unicodedata.normalize('NFKC', value).casefold()


def proxy_stems(path):
    stem = _name(Path(path).stem)
    names, pending = {stem}, [stem]
    while pending:
        current = pending.pop()
        variants = {_SUFFIX.sub('', current), _PREFIX.sub('', current)}
        # Some transcoding tools append the proxy suffix to the full original
        # filename, for example A.MOV_proxy.mp4.
        for extension in VIDEO_EXTENSIONS:
            if current.endswith(extension):
                variants.add(current[:-len(extension)])
        for shorter in variants:
            if shorter and shorter not in names:
                names.add(shorter)
                pending.append(shorter)
    return names


def _directory_score(source, proxy):
    """Match an explicit mirrored camera-folder suffix, never duration alone."""
    def parts(path):
        return [_name(part) for part in Path(path).parent.parts
                if _name(part) not in _PROXY_DIRECTORIES]
    source_parts, proxy_parts = parts(source), parts(proxy)
    score = 0
    for original, candidate in zip(reversed(source_parts), reversed(proxy_parts)):
        if original != candidate:
            break
        score += 1
    return score


def match_proxy_folder(sources, folder, cancel_event=None, *, exclude_paths=()):
    """Match names and unambiguous mirrored camera directories; never guess."""
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelled('已取消链接代理。')
    if not Path(folder).is_dir():
        raise ValueError('代理文件夹不存在或无法访问。')
    unique = {identity(path): str(path) for path in sources}
    excluded = set(unique) | {identity(path) for path in exclude_paths}
    by_stem = {}
    for path in unique.values():
        by_stem.setdefault(_name(Path(path).stem), []).append(path)
    candidates = {path: set() for path in unique.values()}
    scanned = 0
    for directory, subdirs, names in os.walk(folder, followlinks=False):
        subdirs[:] = sorted(name for name in subdirs if not Path(directory, name).is_symlink())
        for name in sorted(names):
            if cancel_event is not None and cancel_event.is_set():
                raise ExportCancelled('已取消链接代理。')
            scanned += 1
            if scanned > 50000:
                raise ValueError('目录包含超过 50000 个文件，请选择更具体的代理目录。')
            path = Path(directory, name)
            if path.suffix.lower() not in VIDEO_EXTENSIONS or identity(path) in excluded:
                continue
            for stem in proxy_stems(path):
                for source in by_stem.get(stem, ()):
                    candidates[source].add(str(path))
    # Same camera file names are normal (C0001, DJI_0001). A mirrored camera
    # folder is usable only when it picks one unique source and proxy. Keep
    # non-disambiguated edges so a shared proxy cannot slip through elsewhere.
    owners = {}
    for source, choices in candidates.items():
        for proxy in choices:
            owners.setdefault(proxy, set()).add(source)
    for proxy, sources_for_proxy in owners.items():
        if len(sources_for_proxy) < 2:
            continue
        scores = {source: _directory_score(source, proxy) for source in sources_for_proxy}
        best = max(scores.values())
        winners = [source for source, score in scores.items() if score == best]
        if best > 0 and len(winners) == 1:
            for source in sources_for_proxy - set(winners):
                candidates[source].discard(proxy)
    for source, choices in candidates.items():
        if len(choices) < 2:
            continue
        scores = {proxy: _directory_score(source, proxy) for proxy in choices}
        best = max(scores.values())
        winners = {proxy for proxy, score in scores.items() if score == best}
        if best > 0 and len(winners) == 1:
            candidates[source] = winners
    counts = {}
    for choices in candidates.values():
        for proxy in choices:
            counts[identity(proxy)] = counts.get(identity(proxy), 0) + 1
    matched, issues = [], []
    for source, choices in candidates.items():
        if len(choices) != 1:
            issues.append((source, '发现多个候选代理，请手动选择。' if choices else
                           '未找到对应代理；支持同名、Proxy/PRX/代理前后缀及分辨率后缀，也可手动选择不同名称的代理。'))
            continue
        proxy = next(iter(choices))
        if counts[identity(proxy)] != 1:
            issues.append((source, '同名原片或同一代理匹配了多个原片；请保留机位子文件夹结构，或逐个手动链接。'))
        else:
            matched.append((source, proxy))
    return matched, issues


def validate_existing_proxy(ffprobe, source, proxy, cancel_event=None):
    """Check against the full original, independent of trims on the timeline."""
    probe = lambda binary, path: _probe_media_cancel(binary, path, cancel_event=cancel_event)
    original = probe(ffprobe, source)
    return validate_proxy_media(ffprobe, source, proxy, original.duration,
                                fps=original.fps, probe=probe)
