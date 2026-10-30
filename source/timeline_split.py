"""Atomic source-instance splits preserving the original media clock."""
from copy import deepcopy
from dataclasses import replace
import math
import uuid

from timeline_audio import source_audio_clips, master_audio_clips
from timeline_link import edit_keys
from program_edits import split_at


def split_timeline(project, position, keys=()):
    """Return a validated private edit, split count and retained selection."""
    fps = project.fps
    cut = math.floor(float(position)*fps+.5)/fps
    selected = set(keys)
    all_tracks = not selected
    targets = edit_keys(project, selected)
    if all_tracks:
        targets = {('clip', clip.id) for clip in project.clips}
        targets.update(('camera_audio', clip.id) for clip in source_audio_clips(project)
                       if clip.has_audio is not False)
        targets.update(('audio', clip.id) for clip in master_audio_clips(project))
        targets.add(('program', None))
        targets.update(('subtitle', cue.id) for cue in project.subtitles.cues)
    candidate = deepcopy(project)
    audio_before = {clip.id: deepcopy(clip) for clip in source_audio_clips(project)}
    pairs = {}
    count = 0
    result = set(selected)

    def inside(clip):
        return math.floor(clip.start*fps+.5) < round(cut*fps) < math.floor(clip.end*fps+.5)

    def halves(clip, right_id):
        return (replace(clip, duration=cut-clip.start),
                replace(clip, id=right_id, start=cut, duration=clip.end-cut,
                        source_in=clip.source_in+cut-clip.start))

    pictures = []
    for clip in candidate.clips:
        if ('clip', clip.id) in targets and inside(clip):
            right_id = uuid.uuid4().hex
            pairs[clip.id] = right_id
            pictures.extend(halves(clip, right_id))
            if ('clip', clip.id) in result: result.add(('clip', right_id))
            count += 1
        else: pictures.append(clip)
    candidate.clips = pictures
    # Materialize only detached pairs. Untouched recordings remain implicit.
    for identifier, clip in audio_before.items():
        split_audio = ('camera_audio', identifier) in targets and inside(clip) and clip.has_audio is not False
        right_video = pairs.get(identifier)
        if split_audio:
            right_id = right_video or uuid.uuid4().hex
            left, right = halves(clip, right_id)
            if right_video and identifier not in project.audio_overrides:
                pass  # Both pictures already provide the same implicit sound.
            else:
                candidate.audio_overrides[identifier] = left
                candidate.audio_overrides[right_id] = right
            if ('camera_audio', identifier) in result: result.add(('camera_audio', right_id))
            count += 1
        elif right_video:
            candidate.audio_overrides[identifier] = clip
            candidate.audio_overrides[right_video] = None
    for identifier, right_id in pairs.items():
        if identifier not in audio_before: candidate.audio_overrides[right_id] = None

    # Both halves retain the original cache source range and temporal handles.
    for field in ('stabilizations', 'library_overlays'):
        records = []
        for item in getattr(candidate, field):
            right_id = pairs.get(item.source_id)
            if not right_id:
                records.append(item)
                continue
            if item.start < cut:
                records.append(replace(item, end=min(item.end, cut)))
            if item.end > cut:
                records.append(replace(item, source_id=right_id, start=max(item.start, cut)))
        setattr(candidate, field, records)
    if pairs and candidate.library_overlays:
        from library_overlay import _signature, overlay_cache_matches
        for original in project.library_overlays:
            if original.source_id in pairs and not overlay_cache_matches(original, project, strict=True):
                raise ValueError('素材叠加缓存已失效，请重新应用后再切开。')
        affected = set(pairs) | set(pairs.values())
        for item in candidate.library_overlays:
            if item.source_id in affected: item.signature = _signature(candidate, item)

    master = []
    master_changed = False
    for clip in master_audio_clips(project):
        if ('audio', clip.id) in targets and inside(clip):
            right_id = uuid.uuid4().hex
            master.extend(halves(clip, right_id))
            if ('audio', clip.id) in result: result.add(('audio', right_id))
            master_changed = True
            count += 1
        else: master.append(deepcopy(clip))
    if master_changed: candidate.master_audio_clips = master

    cues = []
    offset = project.subtitles.offset
    for cue in candidate.subtitles.cues:
        if (('subtitle', cue.id) in targets and
                math.floor((cue.start+offset)*fps+.5) < round(cut*fps) < math.floor((cue.end+offset)*fps+.5)):
            right_id = uuid.uuid4().hex
            cues.extend((replace(cue, end=cut-offset), replace(cue, id=right_id, start=cut-offset)))
            if ('subtitle', cue.id) in result: result.add(('subtitle', right_id))
            count += 1
        else: cues.append(cue)
    candidate.subtitles.cues = cues
    if any(kind == 'program' for kind, _ in targets):
        count += bool(split_at(candidate, cut))
    if count: candidate.validate()
    return candidate, count, result
