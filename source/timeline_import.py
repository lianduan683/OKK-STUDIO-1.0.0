"""Read editorial interchange files without touching media or the current edit.

All readers produce source instances in seconds, then share the same project
builder. No renderer, shell command, or external XML entity is executed here.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
import json
import heapq
import math
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET


FILE_TYPES = [('时间线交换文件', '*.xml *.fcpxml *.fcpxmld *.edl *.otio *.mlt *.kdenlive *.aaf'),
              ('Final Cut / Premiere / Resolve XML', '*.xml *.fcpxml *.fcpxmld'),
              ('CMX 3600 EDL', '*.edl'), ('OpenTimelineIO', '*.otio'),
              ('Shotcut / Kdenlive MLT', '*.mlt *.kdenlive'), ('AAF', '*.aaf'), ('所有文件', '*.*')]
LIMIT = 64 * 1024 * 1024


@dataclass
class ImportedClip:
    path: str
    track: str
    start: float
    duration: float
    source_in: float = 0.
    kind: str = 'video'
    selected: bool = True
    enabled: bool = True
    has_audio: bool = False
    priority: int = 0
    expected_track: str = ''


@dataclass
class ImportedTimeline:
    name: str
    fps: float = 30.
    clips: list[ImportedClip] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    markers: list[float] = field(default_factory=list)
    duration: float = 0.
    origin: float = 0.
    multicam: bool = False

    def warn(self, text):
        if text not in self.warnings: self.warnings.append(text)

    def check(self):
        if not math.isfinite(self.fps) or not 0 < self.fps <= 1000:
            raise ValueError('时间线帧率无效。')
        if not self.clips: raise ValueError('没有可导入的音视频素材。')
        if len(self.clips) > 100_000: raise ValueError('时间线素材数量过多。')
        for c in self.clips:
            if (not c.path or '\x00' in c.path or not c.track or c.kind not in ('video', 'audio')
                    or any(not math.isfinite(x) for x in (c.start, c.duration, c.source_in))
                    or c.start < 0 or c.source_in < -1e-6 or c.duration <= 0):
                raise ValueError('时间线包含无效的素材路径、位置、入点或时长。')
            c.source_in = max(0., c.source_in)
            if c.kind == 'video' and not c.enabled:
                self.warn('已停用的视频片段未导入节目及机位轨道。')
        self.duration = max(self.duration, *(c.start+c.duration for c in self.clips))
        if not math.isfinite(self.duration) or self.duration <= 0:
            raise ValueError('时间线时长无效。')
        return self


def seconds(value, default=0.):
    if value is None or value == '': return default
    return float(Fraction(str(value).removesuffix('s')))


def media_path(value, base):
    value = str(value or '')
    if value.lower().startswith('file:'):
        url = urlsplit(value)
        value = unquote(url.path)
        if url.netloc and url.netloc.lower() != 'localhost': value = '//'+url.netloc+value
        if re.match(r'^/[A-Za-z]:', value): value = value[1:]
    elif '://' in value:
        raise ValueError('不支持网络素材地址，请先在原剪辑软件中链接到本地媒体。')
    if not value: return ''
    return str(Path(value) if Path(value).is_absolute() else (base/value).resolve())


def _xml(data):
    # Real FCPXML documents carry an external DTD declaration. ElementTree
    # ignores it; reject entity declarations so internal expansion is impossible.
    if re.search(br'<!\s*ENTITY\b', data, re.I): raise ValueError('时间线 XML 包含不支持的实体声明。')
    root = ET.fromstring(data)
    for node in root.iter(): node.tag = node.tag.rsplit('}', 1)[-1]
    return root


def _rate(node, default=30.):
    if node is None: return default
    rate = node if node.tag == 'rate' else node.find('rate')
    if rate is None: return default
    value = float(rate.findtext('timebase', str(default)))
    return value*1000/1001 if rate.findtext('ntsc', 'FALSE').upper() == 'TRUE' else value


def _crop(clips, left, duration, target):
    result = []
    for c in clips:
        start, end = max(left, c.start), min(left+duration, c.start+c.duration)
        if end > start+1e-9:
            result.append(replace(c, start=target+start-left, duration=end-start,
                                  source_in=c.source_in+start-c.start))
    return result


def _fcp7(root, base, sequence_index):
    sequences = root.findall('./sequence') or root.findall('./project/children/sequence')
    if not sequences: sequences = root.findall('.//sequence')
    if not sequences: raise ValueError('XML 中没有时间线 sequence。')
    if not 0 <= sequence_index < len(sequences): raise ValueError('时间线序号无效。')
    seq = sequences[sequence_index]
    result = ImportedTimeline(seq.findtext('name', 'XML 时间线'), _rate(seq))
    definitions = {n.get('id'): n for n in root.iter() if n.get('id') and len(n)}

    def resolve(node):
        return definitions.get(node.get('id'), node) if node is not None and not len(node) else node

    def walk(sequence, prefix='', depth=0):
        if depth > 24: raise ValueError('嵌套时间线层级过深或存在循环。')
        fps = _rate(sequence, result.fps)
        found = []
        for kind in ('video', 'audio'):
            for index, track in enumerate(sequence.findall('media/'+kind+'/track'), 1):
                key = prefix+('V' if kind == 'video' else 'A')+str(index)
                enabled = track.findtext('enabled', 'TRUE').upper() != 'FALSE'
                for item in track.findall('clipitem'):
                    if item.find('filter') is not None:
                        for effect in item.findall('filter/effect'):
                            if 'timeremap' in effect.findtext('effectid', '').lower():
                                raise ValueError('XML 包含变速片段，请在原软件中渲染变速后再导入。')
                        result.warn('调色、滤镜、变换和音量自动化未转换；仅导入剪辑结构。')
                    start, end = float(item.findtext('start', '0')), float(item.findtext('end', '0'))
                    if start < 0 or end < 0:
                        raise ValueError('XML 含依赖转场的 -1 入出点，请先在原软件中将转场展开为切点再导出。')
                    if end <= start: continue
                    start, duration = start/fps, (end-start)/fps
                    file = resolve(item.find('file'))
                    source_fps = _rate(item, _rate(file, fps))
                    source_in = float(item.findtext('in', '0'))/source_fps
                    nested = resolve(item.find('sequence'))
                    if nested is not None:
                        result.multicam = True
                        items = _crop(walk(nested, key+' / ', depth+1), source_in, duration, start)
                        for c in items: c.enabled &= enabled and item.findtext('enabled', 'TRUE').upper() != 'FALSE'
                        found.extend(items)
                        continue
                    if file is None:
                        result.warn('未转换 XML 中的生成器或无媒体片段。'); continue
                    # File timecode is not a media in-point: <in> is already
                    # expressed relative to the source file at its own rate.
                    path = media_path(file.findtext('pathurl') or file.findtext('name') or item.findtext('name'), base)
                    found.append(ImportedClip(path, key, start, duration, source_in, kind,
                        enabled=enabled and item.findtext('enabled', 'TRUE').upper() != 'FALSE',
                        has_audio=False, priority=index))
                if track.find('transitionitem') is not None: result.warn('转场按切点导入，转场效果未转换。')
        return found

    result.clips = walk(seq)
    result.duration = float(seq.findtext('duration', '0'))/result.fps
    result.markers = [float(m.findtext('in', '0'))/result.fps for m in seq.findall('marker')]
    return result


def _fcpx(root, base, sequence_index):
    resources = {n.get('id'): n for n in root.findall('./resources/*')}
    projects = root.findall('.//project')
    if not projects: raise ValueError('FCPXML 中没有项目时间线。')
    if not 0 <= sequence_index < len(projects): raise ValueError('时间线序号无效。')
    project = projects[sequence_index]
    seq = project.find('sequence')
    if seq is None: raise ValueError('FCPXML 项目中没有 sequence。')
    fmt = resources.get(seq.get('format'))
    fps = 1/seconds(fmt.get('frameDuration')) if fmt is not None and fmt.get('frameDuration') else 30.
    result = ImportedTimeline(project.get('name', 'FCPXML 时间线'), fps,
                              duration=seconds(seq.get('duration')), origin=seconds(seq.get('tcStart')))

    def walk(container, prefix='', depth=0):
        if depth > 24: raise ValueError('嵌套时间线层级过深或存在循环。')
        found = []
        cursor = 0.
        for node in container:
            tag = node.tag
            if tag in ('metadata', 'mc-source', 'note', 'marker', 'chapter-marker', 'keyword', 'rating'):
                continue
            if tag == 'transition': result.warn('转场按切点导入，转场效果未转换。'); continue
            if tag not in ('asset-clip', 'clip', 'video', 'audio', 'mc-clip', 'ref-clip', 'sync-clip', 'spine', 'gap', 'title'):
                continue
            start = seconds(node.get('offset'), cursor)
            source = seconds(node.get('start'))
            length = seconds(node.get('duration'))
            cursor = max(cursor, start+length)
            lane = node.get('lane', '0')
            key = prefix+'机位 '+lane
            if node.find('timeMap') is not None:
                raise ValueError('FCPXML 包含变速片段，请渲染变速后再导入。')
            if any(n.tag.startswith(('adjust-', 'filter-')) for n in node):
                result.warn('调色、滤镜、变换和音量自动化未转换；仅导入剪辑结构。')
            if tag == 'spine':
                found.extend(walk(node, prefix, depth+1)); continue
            ref = resources.get(node.get('ref'))
            if tag == 'mc-clip' and ref is not None:
                mc = ref.find('multicam')
                if mc is None: raise ValueError('FCPXML 多机位引用缺失。')
                result.multicam = True
                selectors = node.findall('mc-source')
                video_id = next((s.get('angleID') for s in selectors if s.get('srcEnable', 'all') in ('all', 'video')), None)
                audio_id = next((s.get('angleID') for s in selectors if s.get('srcEnable', 'all') in ('all', 'audio')), None)
                angles = mc.findall('mc-angle')
                video_id = video_id or (angles[0].get('angleID') if angles else '')
                audio_id = audio_id or video_id
                mc_clips = []
                expected = ''
                for angle in angles:
                    angle_clips = walk(angle, prefix+ref.get('name', '多机位')+' / '+angle.get('name', angle.get('angleID', ''))+' / ', depth+1)
                    if angle.get('angleID') == video_id:
                        expected = next((c.track for c in angle_clips if c.kind == 'video'), '')
                    for c in _crop(angle_clips, source, length, start):
                        c.selected = angle.get('angleID') == video_id
                        if c.kind == 'audio': c.enabled &= angle.get('angleID') == audio_id
                        c.enabled &= node.get('enabled', '1') != '0'
                        mc_clips.append(c)
                if not expected: raise ValueError('多机位选中的画面角度没有可用的视频引用。')
                for c in mc_clips:
                    if c.kind == 'video': c.expected_track = expected
                found.extend(mc_clips)
            elif tag == 'ref-clip' and ref is not None:
                nested = ref.find('sequence')
                if nested is None: raise ValueError('FCPXML 嵌套时间线引用缺失。')
                found.extend(_crop(walk(nested.find('spine') if nested.find('spine') is not None else nested,
                                        key+' / ', depth+1), source, length, start))
            elif tag in ('gap', 'sync-clip', 'clip'):
                inner = walk(node, prefix, depth+1)
                found.extend(_crop(inner, source, length, start))
            elif tag in ('asset-clip', 'video', 'audio'):
                if ref is None or ref.tag != 'asset': raise ValueError('FCPXML 素材引用缺失：'+str(node.get('ref')))
                rep = next((n for n in ref.findall('media-rep') if n.get('kind') == 'original-media'), None)
                path = media_path(ref.get('src') or (rep.get('src') if rep is not None else '') or ref.get('name'), base)
                source_in = source-seconds(ref.get('start'))
                if source_in < -1e-6: raise ValueError('FCPXML 素材入点早于媒体起始时间码。')
                enabled = node.get('enabled', '1') != '0'
                audio = ref.get('hasAudio', '0') == '1'
                video = tag != 'audio' and ref.get('hasVideo', '1') == '1'
                if length > 0:
                    if video: found.append(ImportedClip(path, key, start, length, source_in,
                                                        enabled=enabled, priority=int(lane)))
                    if audio or tag == 'audio':
                        audio_enabled = node.get('srcEnable', 'all') not in ('video', 'none')
                        for setting in node.findall('audio-channel-source'):
                            if setting.get('enabled') == '0': audio_enabled = False
                        found.append(ImportedClip(path, key, start, length, source_in, 'audio', enabled=enabled and audio_enabled))
                # Connected clips are positioned in the parent's source clock.
                found.extend(_crop(walk(node, prefix, depth+1), source, length, start))
            elif tag == 'title': result.warn('标题、发生器和字幕样式未转换。')
            for marker in node.findall('marker'):
                result.markers.append(max(0., start+seconds(marker.get('start'))-source))
        return found

    spine = seq.find('spine')
    result.clips = walk(spine if spine is not None else seq)
    return result


def _rational(node):
    return float(node['value'])/float(node['rate'])


def _otio(payload, base, sequence_index=0):
    if str(payload.get('OTIO_SCHEMA', '')).startswith('SerializableCollection'):
        payload = payload['children'][sequence_index]
    result = ImportedTimeline(payload.get('name') or 'OTIO 时间线')
    rates = []

    def walk(node, prefix='', depth=0):
        if depth > 24: raise ValueError('OTIO 嵌套层级过深。')
        schema = node.get('OTIO_SCHEMA', '').split('.')[0]
        sr = node.get('source_range')
        source = _rational(sr['start_time']) if sr else 0.
        length = _rational(sr['duration']) if sr else None
        if sr: rates.append(float(sr['duration']['rate']))
        if schema == 'Timeline': return walk(node['tracks'], prefix, depth+1)
        if schema in ('Track', 'Stack'):
            clips, cursor, extent = [], 0., 0.
            for index, child in enumerate(node.get('children', []), 1):
                if child.get('OTIO_SCHEMA', '').startswith('Transition'):
                    raise ValueError('OTIO 含重叠转场，请先转换为切点后导入。')
                items, duration = walk(child, prefix+str(index)+' / ' if schema == 'Stack' else prefix, depth+1)
                offset = cursor if schema == 'Track' else 0.
                for item in items:
                    item.start += offset
                    if schema == 'Track' and node.get('kind') == 'Audio': item.kind = 'audio'
                    item.enabled &= node.get('enabled', True)
                clips.extend(items)
                cursor += duration
                extent = max(extent, offset+duration)
            for marker in node.get('markers', []):
                if marker.get('marked_range'): result.markers.append(_rational(marker['marked_range']['start_time']))
            return (_crop(clips, source, length, 0.), length) if length is not None else (clips, extent)
        if schema == 'Gap': return [], length or 0.
        if schema != 'Clip': raise ValueError('不支持的 OTIO 对象：'+schema)
        if node.get('effects'):
            raise ValueError('OTIO 包含效果或变速，请在源软件中渲染/移除后导入。')
        ref = node.get('media_reference')
        if ref is None:
            ref = node.get('media_references', {}).get(node.get('active_media_reference_key', 'DEFAULT_MEDIA'))
        ref = ref or {}
        if not str(ref.get('OTIO_SCHEMA', '')).startswith('ExternalReference'):
            raise ValueError('OTIO 包含缺少外部媒体引用的片段，请先链接媒体。')
        available = ref.get('available_range')
        media_origin = _rational(available['start_time']) if available else 0.
        if length is None:
            if not available: raise ValueError('OTIO 片段缺少时长。')
            source, length = media_origin, _rational(available['duration'])
        path = media_path(ref.get('target_url'), base)
        return [ImportedClip(path, prefix or 'V1', 0., length, source-media_origin,
                             enabled=node.get('enabled', True))], length

    result.clips, result.duration = walk(payload)
    global_start = payload.get('global_start_time')
    result.fps = float(global_start['rate']) if global_start else (rates[0] if rates else 30.)
    result.origin = _rational(global_start) if global_start else 0.
    return result


def _timecode(value, fps, drop=False):
    h, m, s, f = map(int, re.split('[:;]', value))
    nominal = round(fps)
    if m >= 60 or s >= 60 or f >= nominal: raise ValueError('EDL 时间码无效：'+value)
    count = ((h*60+m)*60+s)*nominal+f
    if drop or ';' in value:
        if nominal not in (30, 60) or abs(fps-nominal*1000/1001) > .001:
            raise ValueError('丢帧时间码仅支持 29.97 / 59.94 fps。')
        dropped = 2 if nominal == 30 else 4
        if m % 10 and s == 0 and f < dropped: raise ValueError('EDL 使用了被丢弃的帧号。')
        minutes = h*60+m
        count -= dropped*(minutes-minutes//10)
    return count/fps


def _edl(text, base, fps):
    title = re.search(r'^TITLE:\s*(.*)', text, re.M)
    result = ImportedTimeline(title.group(1).strip() if title else 'EDL 时间线', fps)
    drop = bool(re.search(r'FCM:\s*DROP FRAME', text))
    pending = []
    event = re.compile(r'^\s*\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+(.*)$')
    for line in text.splitlines():
        match = event.match(line)
        if match:
            reel, channel, transition, rest = match.groups()
            if transition != 'C': raise ValueError('EDL 含转场，请先导出仅含切点的 EDL。')
            codes = re.findall(r'\d{2}:\d{2}:\d{2}[:;]\d{2}', rest)
            if len(codes) != 4: raise ValueError('EDL 事件缺少四个时间码。')
            src, src_end, start, end = [_timecode(c, fps, drop) for c in codes]
            if end <= start or abs((src_end-src)-(end-start)) > 1/fps:
                raise ValueError('EDL 包含倒放、变速或无效时长。')
            if reel.upper() in ('BL', 'BLACK'): pending = []; continue
            pending = []
            for kind in (['video', 'audio'] if 'V' in channel and 'A' in channel else ['video' if 'V' in channel else 'audio']):
                clip = ImportedClip(media_path(reel, base), 'V1' if kind == 'video' else channel,
                                    start, end-start, src, kind)
                result.clips.append(clip); pending.append(clip)
        elif pending and re.match(r'\*\s*(FROM CLIP NAME|SOURCE FILE|SOURCE FILE PATH):', line, re.I):
            path = media_path(line.split(':', 1)[1].strip(), base)
            for clip in pending: clip.path = path
        elif re.match(r'\s*M2\s', line): raise ValueError('EDL 包含 M2 变速事件，请渲染后导入。')
    if result.clips:
        result.origin = min(c.start for c in result.clips)
        for c in result.clips: c.start -= result.origin
    result.warn('EDL 不携带可靠的素材时间码起点；入点暂按文件相对时间解释。非零源时间码请改用 XML/OTIO。')
    return result


def _mlt(root, base):
    profile = root.find('profile')
    fps = float(profile.get('frame_rate_num', '30'))/float(profile.get('frame_rate_den', '1')) if profile is not None else 30.
    result = ImportedTimeline(root.get('producer', 'MLT 时间线'), fps)
    nodes = {n.get('id'): n for n in root if n.get('id')}

    def prop(node, key):
        return next((p.text or '' for p in node.findall('property') if p.get('name') == key), '')

    def mtime(value):
        if value is None: return 0.
        if ':' in value:
            h, m, s = value.split(':'); return int(h)*3600+int(m)*60+float(s)
        return float(value)/fps

    def walk(identifier, track, kind='video', depth=0):
        if depth > 24: raise ValueError('MLT 时间线存在循环引用。')
        node = nodes.get(identifier)
        if node is None: raise ValueError('MLT 缺少媒体引用：'+identifier)
        if node.tag in ('producer', 'chain'):
            service = prop(node, 'mlt_service')
            if service in ('color', 'colour', 'qtext', 'kdenlivetitle'):
                result.warn('MLT 发生器、背景和标题未转换。'); return []
            if service == 'timewarp': raise ValueError('MLT 包含变速，请渲染后导入。')
            resource = prop(node, 'resource')
            if not resource: return []
            if node.find('filter') is not None: result.warn('MLT 滤镜、调色和音量自动化未转换。')
            length = mtime(node.get('out'))+1/fps if node.get('out') else mtime(prop(node, 'length') or '1')
            return [ImportedClip(media_path(resource, base), track, 0., length, kind=kind,
                                 has_audio=prop(node, 'audio_index') != '-1')]
        if node.tag == 'playlist':
            found, cursor = [], 0.
            for entry in node:
                if entry.tag == 'blank': cursor += mtime(entry.get('length')); continue
                if entry.tag != 'entry': continue
                left, end = mtime(entry.get('in')), mtime(entry.get('out'))+1/fps
                if end <= left: raise ValueError('MLT 片段入出点无效。')
                found.extend(_crop(walk(entry.get('producer'), track, kind, depth+1), left, end-left, cursor))
                cursor += end-left
            return found
        if node.tag == 'tractor':
            found = []
            for index, entry in enumerate(node.findall('track')):
                if entry.get('hide') == 'both': continue
                audio = entry.get('hide') == 'video'
                name = prop(nodes.get(entry.get('producer'), ET.Element('x')), 'kdenlive:track_name')
                items = walk(entry.get('producer'), track+'/'+(name or str(index)), 'audio' if audio else kind, depth+1)
                for c in items: c.priority = index
                if entry.get('hide') == 'audio':
                    for c in items: c.has_audio = False
                found.extend(items)
            if node.find('transition') is not None: result.warn('MLT 合成和转场效果未转换；按上层视频优先生成节目切点。')
            return found
        raise ValueError('不支持的 MLT 节点：'+node.tag)

    tractors = root.findall('tractor')
    target = nodes.get(root.get('producer'))
    if target is None or target.tag not in ('tractor', 'playlist'):
        target = tractors[-1] if tractors else root.find('playlist')
    if target is None: raise ValueError('MLT 文件中没有时间线。')
    result.clips = walk(target.get('id'), '')
    return result


def sequence_names(path):
    path = Path(path)
    if path.is_dir(): path = path/'Info.fcpxml'
    if path.stat().st_size > LIMIT: raise ValueError('时间线文件超过 64 MB。')
    if path.suffix.lower() == '.otio':
        payload = json.loads(path.read_text('utf-8-sig'))
        if str(payload.get('OTIO_SCHEMA', '')).startswith('SerializableCollection'):
            return [p.get('name', f'时间线 {i+1}') for i, p in enumerate(payload.get('children', []))]
    if path.suffix.lower() not in ('.xml', '.fcpxml'): return [path.stem]
    root = _xml(path.read_bytes())
    if root.tag == 'fcpxml': return [p.get('name', '时间线') for p in root.findall('.//project')]
    if root.tag == 'xmeml':
        seqs = root.findall('./sequence') or root.findall('./project/children/sequence') or root.findall('.//sequence')
        return [s.findtext('name', '时间线') for s in seqs]
    return [path.stem]


def read_timeline(path, *, fps=30., sequence_index=0):
    path = Path(path).resolve()
    if path.is_dir(): path = path/'Info.fcpxml'
    if path.stat().st_size > LIMIT: raise ValueError('时间线文件超过 64 MB。')
    ext, base = path.suffix.lower(), path.parent
    try:
        if ext == '.aaf':
            try:
                import opentimelineio as otio
                timeline = otio.adapters.read_from_file(str(path), adapter_name='AAF')
                payload = json.loads(otio.adapters.write_to_string(timeline, adapter_name='otio_json'))
            except ImportError as error:
                raise ValueError('此版本未安装 AAF 适配器。请从 Avid/Resolve/Premiere 导出 XML、FCPXML 或 OTIO 后导入。') from error
            except Exception as error:
                raise ValueError('AAF 读取失败，请改用 XML/OTIO：'+str(error)) from error
            result = _otio(payload, base, sequence_index)
            result.warn('AAF 通过 OpenTimelineIO 适配器导入；嵌套和效果取决于适配器的转换结果。')
        elif ext == '.otio': result = _otio(json.loads(path.read_text('utf-8-sig')), base, sequence_index)
        elif ext == '.edl':
            data = path.read_bytes()
            try: content = data.decode('utf-8-sig')
            except UnicodeDecodeError: content = data.decode('gb18030')
            result = _edl(content, base, fps)
        elif ext in ('.xml', '.fcpxml', '.mlt', '.kdenlive'):
            root = _xml(path.read_bytes())
            if root.tag == 'xmeml': result = _fcp7(root, base, sequence_index)
            elif root.tag == 'fcpxml': result = _fcpx(root, base, sequence_index)
            elif root.tag == 'mlt': result = _mlt(root, base)
            else: raise ValueError('无法识别 XML 时间线。请使用 FCP7 XML、FCPXML 或 MLT 交换格式。')
        else:
            raise ValueError('不支持该原生工程格式。请在原剪辑软件中导出 XML、FCPXML、EDL 或 OTIO。')
        return result.check()
    except (ET.ParseError, KeyError, TypeError, ZeroDivisionError, OverflowError, IndexError, json.JSONDecodeError) as error:
        raise ValueError('时间线格式损坏或缺少必要字段：'+str(error)) from error


def missing_media(timeline):
    return sorted({c.path for c in timeline.clips if not Path(c.path).is_file()})


def relink_media(timeline, folder):
    """Only unique filename matches are automatic; ambiguous media stays offline."""
    wanted = {Path(p).name.casefold() for p in missing_media(timeline)}
    matches = {name: [] for name in wanted}
    for directory, _, files in os.walk(folder):
        for name in files:
            if name.casefold() in matches: matches[name.casefold()].append(str(Path(directory)/name))
    count = 0
    for c in timeline.clips:
        if Path(c.path).is_file(): continue
        choices = matches.get(Path(c.path).name.casefold(), [])
        if len(choices) == 1: c.path = choices[0]; count += 1
    return count


def build_project(timeline, *, collapsed=False):
    from multicam_core import CameraClip, Cut, MulticamProject
    timeline.check()
    project = MulticamProject(fps=timeline.fps, timeline_multicam_collapsed=collapsed,
                              master_duration=timeline.duration)
    keys = list(dict.fromkeys(c.track for c in timeline.clips))
    cameras = {key: index for index, key in enumerate(keys, 1)}
    project.camera_names = {cameras[key]: ' '.join(key.split())[:80] or '机位' for key in keys}
    videos = []
    for c in timeline.clips:
        camera = cameras[c.track]
        clip = CameraClip(uuid.uuid4().hex, camera, c.path, c.start, c.duration,
                          has_audio=c.has_audio, source_in=c.source_in)
        if c.kind == 'video':
            if not c.enabled:
                timeline.warn('已停用的视频片段未导入节目及机位轨道。')
                continue
            project.clips.append(clip)
            if not c.has_audio: project.audio_overrides[clip.id] = None
            if c.enabled: videos.append((c, clip))
        else:
            clip.has_audio = True
            project.audio_overrides[clip.id] = clip
            project.audio_track_enabled[camera] = project.audio_track_enabled.get(camera, False) or c.enabled
            if not c.enabled: clip.has_audio = False
    # Explicit audio instances prevent duplicate linked sound from video files.
    for c, clip in videos:
        if clip.camera not in project.audio_track_enabled:
            project.audio_track_enabled[clip.camera] = bool(c.has_audio and c.enabled)
    points = sorted({0., timeline.duration, *(v for c, _ in videos for v in (c.start, c.start+c.duration))})
    previous = None
    ranked = sorted(videos, key=lambda pair: pair[0].start)
    active, cursor = [], 0
    available_video = set(project.cameras)
    for left, right in zip(points, points[1:]):
        sample = (left+right)/2
        while cursor < len(ranked) and ranked[cursor][0].start <= sample:
            c, clip = ranked[cursor]
            heapq.heappush(active, (-int(c.selected), -c.priority, -cameras[c.track], -c.start, cursor, c, clip))
            cursor += 1
        while active and active[0][5].start+active[0][5].duration <= sample: heapq.heappop(active)
        if not active: continue
        c, clip = active[0][5:]
        chosen = cameras.get(c.expected_track, clip.camera) if c.expected_track and not c.selected else clip.camera
        if chosen not in available_video:
            raise ValueError('选中的多机位角度在导入范围内没有视频，请先在原软件检查该角度。')
        if chosen != previous:
            project.cuts.append(Cut(left, chosen)); previous = chosen
    # Keep exact programme edit boundaries, even if adjacent edits use one camera.
    project.edit_points = sorted({v for c in timeline.clips if c.kind == 'video' and c.enabled
                                 for v in (c.start, c.start+c.duration) if 0 < v < timeline.duration})
    project.timeline_markers = sorted({m for m in timeline.markers if math.isfinite(m) and 0 <= m <= timeline.duration})
    video_ids = set(project.cameras)
    project.timeline_removed_tracks = [f'V{n}' for n in cameras.values() if n not in video_ids]
    project.validate()
    return project
