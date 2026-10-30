"""Harness-independent, non-destructive editing and frame-clock inspection.

No model, network service, Tk root, or MCP dependency is needed by this module.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from multicam_core import MulticamProject
from program_edits import set_camera_on_range, split_at
from timeline_edits import delete_source_clips, move_source_clips
try:
    from timeline_split import split_timeline
except ImportError:
    def split_timeline(*args, **kwargs):
        raise ValueError('Source splitting requires the current OKK editor source format')


BASE = Path(__file__).resolve().parent
MAX_PROJECT_BYTES = 32 * 1024 * 1024


def publish_new_file(temporary, destination):
    """Publish on the same volume without overwriting a concurrent writer."""
    if os.name == 'nt':
        os.rename(temporary, destination)  # Windows rename fails if destination exists.
    else:
        os.link(temporary, destination)


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    return float(value)


def frame(value, fps):
    return math.floor(number(value, 'time') * fps + .5)


def revision(project):
    payload = project.to_payload(BASE)
    payload.pop('monitor_layout', None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def snapshot(project, **metadata):
    return {'revision': revision(project), 'base_directory': str(BASE),
            'project': project.to_payload(BASE), **metadata}


def statistics(project):
    """Count nominal programme frames; never count effect boundaries as cuts."""
    project.validate()
    fps = project.fps
    first = frame(project.range_start, fps)
    last = frame(project.range_end if project.range_end is not None else project.duration, fps)
    cuts = {frame(c.time, fps): c.camera for c in sorted(project.cuts, key=lambda c: c.time)}
    explicit_edges = {frame(p, fps) for p in project.edit_points}
    edges = {first, last}
    edges.update(cuts)
    edges.update(explicit_edges)
    for clip in project.clips:
        edges.update((frame(clip.start, fps), frame(clip.end, fps)))
    edges = sorted(e for e in edges if first <= e <= last)
    by_camera = defaultdict(list)
    for clip in project.clips:
        by_camera[clip.camera].append(clip)
    # Sweep source edges once per camera instead of scanning every recording
    # for every output segment (large multi-file concert projects).
    schedules = {}
    active = defaultdict(list)
    cursors = defaultdict(int)
    for chosen, clips in by_camera.items():
        ranked = sorted(clips, key=lambda c: (c.start, c.id))
        schedules[chosen] = sorted([(frame(c.start, fps), -rank, frame(c.end, fps), c)
                                    for rank, c in enumerate(ranked)], key=lambda item: (item[0], item[1]))
    camera = min(project.cameras, default=None)
    ordered_cuts = sorted(cuts.items())
    index = 0
    used = defaultdict(int)
    ranges = defaultdict(list)
    segments = []
    gaps = 0
    for left, right in zip(edges, edges[1:]):
        while index < len(ordered_cuts) and ordered_cuts[index][0] <= left:
            camera = ordered_cuts[index][1]
            index += 1
        selected = project.stacked_cameras() if project.video_stack_enabled else [camera]
        layers = []
        for chosen in selected:
            schedule, heap = schedules.get(chosen, []), active[chosen]
            cursor = cursors[chosen]
            while cursor < len(schedule) and schedule[cursor][0] <= left:
                _, rank, stop, candidate = schedule[cursor]
                heapq.heappush(heap, (rank, stop, candidate))
                cursor += 1
            cursors[chosen] = cursor
            while heap and heap[0][1] < right:
                heapq.heappop(heap)
            clip = heap[0][2] if heap else None
            if clip is None:
                continue
            source_start = max(0., left / fps - getattr(clip, 'source_origin', clip.start))
            item = {'clip_id': clip.id, 'camera': chosen, 'path': clip.path,
                    'start': left / fps, 'end': right / fps,
                    'source_start': source_start, 'source_end': source_start + (right-left)/fps}
            layers.append(item)
            used[clip.id] += right-left
            previous = ranges[clip.id]
            if previous and frame(previous[-1]['end'], fps) == left:
                previous[-1]['end'] = right/fps
                previous[-1]['source_end'] = item['source_end']
            else:
                previous.append(dict(item))
            # Lower opaque layers cannot contribute to the nominal picture.
            if project.video_stack_enabled and not clip.has_alpha:
                break
        if not layers:
            gaps += right-left
        identity = tuple(item['clip_id'] for item in layers)
        explicit = left in explicit_edges
        if segments and tuple(i['clip_id'] for i in segments[-1]['layers']) == identity and not explicit:
            segments[-1]['end'] = right/fps
            segments[-1]['frames'] += right-left
            for old, new in zip(segments[-1]['layers'], layers):
                old['end'], old['source_end'] = new['end'], new['source_end']
        else:
            segments.append({'start': left/fps, 'end': right/fps, 'frames': right-left,
                             'layers': layers})
    switch_frames = []
    previous = min(project.cameras, default=None)
    for position, chosen in ordered_cuts:
        if chosen != previous and first < position < last:
            switch_frames.append(position)
        previous = chosen
    explicit_frames = {frame(p, fps) for p in project.edit_points if first < frame(p, fps) < last}
    program_frames = sorted(set(switch_frames) | explicit_frames)
    source_groups = defaultdict(list)
    for clip in project.clips:
        source_groups[(clip.camera, clip.path, frame(getattr(clip, 'source_origin', clip.start), fps))].append(clip)
    source_splits = set()
    source_split_positions = set()
    for key, clips_in_group in source_groups.items():
        starts = {frame(c.start, fps) for c in clips_in_group}
        for clip in clips_in_group:
            position = frame(clip.end, fps)
            if first < position < last and position in starts:
                source_splits.add((*key, position))
                source_split_positions.add(position)
    camera_frames = defaultdict(int)
    file_frames = defaultdict(int)
    clips = []
    for clip in project.clips:
        count = used[clip.id]
        camera_frames[clip.camera] += count
        file_frames[clip.path] += count
        clips.append({'id': clip.id, 'camera': clip.camera, 'path': clip.path,
                      'timeline_start': clip.start, 'timeline_end': clip.end,
                      'source_in': getattr(clip, 'source_in', 0.), 'used_frames': count,
                      'used_seconds': count/fps, 'used_ranges': ranges[clip.id],
                      'online': Path(clip.path).is_file()})
    return {'fps': fps, 'range_start': first/fps, 'range_end': last/fps,
            'duration_frames': max(0, last-first), 'duration_seconds': max(0, last-first)/fps,
            'mode': 'stack' if project.video_stack_enabled else 'multicam',
            'program_cut_count': len(program_frames), 'program_cut_times': [p/fps for p in program_frames],
            'camera_switch_count': len(switch_frames), 'explicit_split_count': len(explicit_frames),
            'source_split_boundary_count': len(source_splits),
            'current_cut_boundary_count': len(set(program_frames) | source_split_positions),
            'cut_count_note': 'current_cut_boundary_count 是节目切点与可识别原片分割的去重时间位置数；'
                              'source_split_boundary_count 按机位及原片分别计数，均不代表历史操作次数。',
            'program_cuts_affect_output': not project.video_stack_enabled,
            'historical_cut_action_count': None,
            'historical_cut_note': '工程只保存当前状态，无法还原历史累计剪切操作次数。',
            'source_instance_count': len(project.clips), 'gap_frames': gaps, 'gap_seconds': gaps/fps,
            'coverage_note': '使用帧时钟统计当前成片范围；转场按名义切点分配，不含混合余量。'
                             '叠加模式统计可能可见图层，透明图层累计时长可超过成片时长。',
            'clips': clips,
            'cameras': [{'camera': c, 'name': project.camera_names.get(c, f'机位 {c}'),
                         'used_frames': camera_frames[c], 'used_seconds': camera_frames[c]/fps}
                        for c in project.cameras],
            'files': [{'path': p, 'used_frames': count, 'used_seconds': count/fps}
                      for p, count in sorted(file_frames.items())], 'segments': segments}


def _keys(operation, required, optional=()):
    missing = set(required)-operation.keys()
    extra = operation.keys()-{'op', *required, *optional}
    if missing or extra:
        raise ValueError(f'Invalid {operation.get("op")} fields: missing={sorted(missing)}, extra={sorted(extra)}')


def build_edit(project, operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= 500:
        raise ValueError('operations must contain 1 to 500 edits')
    candidate = deepcopy(project)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError('Each operation must be an object')
        op = operation.get('op')
        if op == 'split_program':
            _keys(operation, ('time',))
            split_at(candidate, number(operation['time'], 'time'))
        elif op == 'split_source':
            _keys(operation, ('time', 'clip_ids'))
            ids = operation['clip_ids']
            _clip_ids(candidate, ids)
            position = number(operation['time'], 'time')
            if not 0 < position < candidate.duration:
                raise ValueError('Split time must be inside the timeline')
            candidate, _, _ = split_timeline(candidate, position, [('clip', i) for i in ids])
        elif op == 'set_camera':
            _keys(operation, ('start', 'end', 'camera'))
            if candidate.video_stack_enabled:
                raise ValueError('set_camera requires multicam mode; use set_stack to disable stacking first')
            start, end = number(operation['start'], 'start'), number(operation['end'], 'end')
            chosen = operation['camera']
            _require_coverage(candidate, start, end, chosen)
            set_camera_on_range(candidate, start, end, chosen)
        elif op == 'set_range':
            _keys(operation, ('start', 'end'))
            candidate.range_start = frame(operation['start'], candidate.fps)/candidate.fps
            candidate.range_end = frame(operation['end'], candidate.fps)/candidate.fps
        elif op == 'rename_camera':
            _keys(operation, ('camera', 'name'))
            candidate.set_camera_name(operation['camera'], operation['name'])
        elif op == 'add_marker':
            _keys(operation, ('time',))
            position = frame(operation['time'], candidate.fps)/candidate.fps
            if not 0 <= position <= candidate.duration:
                raise ValueError('Marker time is outside timeline')
            candidate.timeline_markers = sorted(set(candidate.timeline_markers) | {position})
        elif op == 'set_audio_enabled':
            _keys(operation, ('track', 'enabled'))
            track, enabled = operation['track'], operation['enabled']
            if type(track) is not int or (track != 0 and track not in candidate.timeline_cameras) or type(enabled) is not bool:
                raise ValueError('Invalid audio track or enabled flag')
            candidate.audio_track_enabled[track] = enabled
        elif op in {'delete_clips', 'move_clips'}:
            _keys(operation, ('clip_ids', 'delta') if op == 'move_clips' else ('clip_ids',))
            _clip_ids(candidate, operation['clip_ids'])
            if op == 'delete_clips':
                delete_source_clips(candidate, operation['clip_ids'])
            else:
                move_source_clips(candidate, operation['clip_ids'], number(operation['delta'], 'delta'))
        elif op == 'set_stack':
            _keys(operation, ('enabled',), ('order', 'hidden'))
            if type(operation['enabled']) is not bool:
                raise ValueError('enabled must be a boolean')
            candidate.video_stack_enabled = operation['enabled']
            for field in ('order', 'hidden'):
                if field in operation:
                    setattr(candidate, f'video_stack_{field}', deepcopy(operation[field]))
        elif op == 'set_subtitles':
            _keys(operation, ('cues',))
            from subtitle_core import SubtitleCue
            if not isinstance(operation['cues'], list) or len(operation['cues']) > 10000:
                raise ValueError('cues must be a list with at most 10000 items')
            candidate.subtitles.cues = [SubtitleCue(**cue) for cue in operation['cues']]
            candidate.subtitles.offset = 0.
            candidate.subtitles.source_path = ''
        else:
            raise ValueError(f'Unknown edit operation: {op}')
        candidate.validate()
    return candidate


def _clip_ids(project, identifiers):
    if (not isinstance(identifiers, list) or not identifiers or
            any(not isinstance(i, str) for i in identifiers) or
            len(set(identifiers)) != len(identifiers) or
            set(identifiers)-{c.id for c in project.clips}):
        raise ValueError('clip_ids must be a nonempty list of existing, unique clip IDs')


def _require_coverage(project, start, end, camera):
    if type(camera) is not int or camera not in project.cameras:
        raise ValueError('Camera does not exist')
    fps = project.fps
    left, right = frame(start, fps), frame(end, fps)
    covered = left
    for clip in sorted((c for c in project.clips if c.camera == camera), key=lambda c: c.start):
        a, b = frame(clip.start, fps), frame(clip.end, fps)
        if a > covered:
            break
        if b > covered:
            covered = b
        if covered >= right:
            return
    raise ValueError('Requested camera has a recording gap in this range')


class EditSession:
    """One project per connection; revisions prevent stale harness writes."""
    def __init__(self, project=None):
        self.project = project or MulticamProject()
        self.project_path = ''
        self.envelope = None
        self.undo_stack = []
        self.redo_stack = []
        self.plans = {}

    def get_project(self):
        return self.project

    def get_snapshot(self):
        return snapshot(self.get_project(), project_path=self.project_path, mode='file')

    def check_revision(self, expected):
        if not expected or expected != revision(self.get_project()):
            raise ValueError('Project changed; read a fresh snapshot and preview again')

    def open_project(self, path, expected_revision):
        self.check_revision(expected_revision)
        path = Path(path).expanduser().resolve()
        if path.stat().st_size > MAX_PROJECT_BYTES:
            raise ValueError('Project exceeds 32 MiB')
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        envelope = None
        if isinstance(data, dict) and data.get('format') == 'rtx-studio-project':
            from project_file import read_project_file
            envelope = read_project_file(path)
            embedded = envelope['state'].get('okk_multicam')
            if not embedded:
                raise ValueError('Full project has no embedded OKK timeline; open/save it in OKK STUDIO first')
            project = MulticamProject.from_payload(embedded['project'], embedded['base_directory'])
        else:
            project = MulticamProject.from_payload(data, path.parent)
        self.project, self.envelope, self.project_path = project, envelope, str(path)
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.plans.clear()
        return self.get_snapshot()

    def preview(self, operations, expected_revision):
        self.check_revision(expected_revision)
        original = self.get_project()
        candidate = build_edit(original, operations)
        identifier = uuid.uuid4().hex
        self.plans[identifier] = (expected_revision, candidate, time.monotonic())
        while len(self.plans) > 20:
            del self.plans[next(iter(self.plans))]
        before, after = statistics(original), statistics(candidate)
        return {'plan_id': identifier, 'base_revision': expected_revision,
                'changed': revision(candidate) != expected_revision,
                'operations': operations, 'before': before, 'after': after,
                'new_clip_ids': sorted({c.id for c in candidate.clips}-{c.id for c in original.clips})}

    def apply(self, plan_id, expected_revision):
        self.check_revision(expected_revision)
        entry = self.plans.get(plan_id)
        if not entry or entry[0] != expected_revision or time.monotonic()-entry[2] > 1800:
            raise ValueError('Plan missing, expired, or based on a different revision; preview again')
        candidate = deepcopy(entry[1])
        candidate.validate()
        if revision(candidate) != expected_revision:
            self.publish(candidate)
        self.plans.clear()
        return self.get_snapshot()

    def publish(self, candidate):
        self.undo_stack.append(deepcopy(self.project))
        self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()
        self.project = candidate

    def history(self, action, expected_revision):
        self.check_revision(expected_revision)
        if action not in {'undo', 'redo'}:
            raise ValueError('action must be undo or redo')
        source, target = ((self.undo_stack, self.redo_stack) if action == 'undo'
                          else (self.redo_stack, self.undo_stack))
        if not source:
            raise ValueError(f'Nothing to {action}')
        target.append(deepcopy(self.project))
        self.project = source.pop()
        self.plans.clear()
        return self.get_snapshot()

    def save_copy(self, path, expected_revision, overwrite=False):
        self.check_revision(expected_revision)
        project = self.get_project()
        destination = Path(path).expanduser().resolve()
        if destination.suffix.lower() != '.rtxproj' and not destination.name.lower().endswith('.multicam.json'):
            raise ValueError('Use .rtxproj or .multicam.json for project output')
        protected = {Path(c.path).resolve() for c in project.clips}
        protected.update(Path(c.proxy_path).resolve() for c in project.clips if c.proxy_path)
        if project.master_audio:
            protected.add(Path(project.master_audio).resolve())
        if destination in protected:
            raise ValueError('Project output cannot overwrite media')
        if destination.suffix.lower() == '.rtxproj':
            payload = deepcopy(self.envelope) if self.envelope else {
                'format': 'rtx-studio-project', 'schema': 2, 'state': {}}
            payload['state']['okk_multicam'] = {
                'base_directory': str(destination.parent),
                'project': project.to_payload(destination.parent),
                'settings': deepcopy((payload['state'].get('okk_multicam') or {}).get('settings', {}))}
            payload['project_path'] = str(destination)
            payload['saved_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        else:
            payload = project.to_payload(destination.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = ''
        if destination.exists():
            if not overwrite:
                raise FileExistsError('Output exists; choose another path or set overwrite=true')
            backup = str(destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.bak'))
            with open(backup, 'xb') as stream:
                stream.write(destination.read_bytes())
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent,
                                             prefix='.okk_mcp_', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if overwrite:
                os.replace(temporary, destination)
            else:
                publish_new_file(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {'path': str(destination), 'backup_path': backup, 'revision': revision(project)}
