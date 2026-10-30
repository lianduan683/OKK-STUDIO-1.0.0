"""Non-destructive clip enable edits on the existing source and audio clocks."""
from copy import deepcopy

from program_edits import program_spans
from timeline_audio import master_audio_clips, source_audio_clips
from timeline_link import edit_keys, preserve_source_audio
from timeline_split import split_timeline


def toggle_timeline_enabled(project, keys):
    """Return a private edit, affected instance count, state and selection.

    A mixed selection is disabled together; an entirely disabled selection is
    enabled together. Programme selections address only their covered source
    ranges, splitting source instances with the existing lossless edit path.
    """
    selected = set(keys)
    candidate = deepcopy(project)
    media_kinds = {'clip', 'camera_audio', 'audio'}
    explicit = {key for key in selected if key[0] in media_kinds}
    ranges = []
    for kind, bounds in selected:
        if kind != 'program': continue
        for span in program_spans(project):
            start, end = max(bounds[0], span.start), min(bounds[1], span.end)
            if start >= end: continue
            cameras = project.stacked_cameras() if project.video_stack_enabled else [span.camera]
            ranges.extend((camera, start, end) for camera in cameras)

    def programme_keys():
        return {('clip', clip.id) for clip in candidate.clips
                if any(clip.camera == camera and clip.start < end-1e-8 and clip.end > start+1e-8
                       for camera, start, end in ranges)}

    for boundary in sorted({value for _, start, end in ranges for value in (start, end)}):
        # Retain explicitly selected source descendants as well as programme
        # ranges when both are part of a multi-selection.
        if explicit:
            candidate, _, explicit = split_timeline(candidate, boundary, explicit)
        programme = programme_keys()
        if programme:
            candidate, _, _ = split_timeline(candidate, boundary, programme)

    targets = edit_keys(candidate, explicit | programme_keys())
    video_ids = {identifier for kind, identifier in targets if kind == 'clip'}
    audio_ids = {identifier for kind, identifier in targets if kind == 'camera_audio'}
    # Freeze detached sound before a picture edit can change its implicit state.
    preserve_source_audio(candidate, video_ids-audio_ids)
    preserve_source_audio(candidate, audio_ids-video_ids)
    videos = [clip for clip in candidate.clips if clip.id in video_ids]
    audio = [clip for clip in source_audio_clips(candidate)
             if clip.id in audio_ids and clip.has_audio is not False]
    master = [clip for clip in master_audio_clips(candidate) if ('audio', clip.id) in targets]
    instances = [*videos, *audio, *master]
    if not instances: return project, 0, True, selected
    enabled = not any(clip.enabled for clip in instances)
    for clip in instances: clip.enabled = enabled
    if master and not candidate.master_audio_clips:
        candidate.master_audio_clips = master_audio_clips(candidate)
        for clip in candidate.master_audio_clips:
            if ('audio', clip.id) in targets: clip.enabled = enabled
    candidate.validate()
    selection = {key for key in selected if key[0] not in media_kinds} | explicit
    return candidate, len(instances), enabled, selection
