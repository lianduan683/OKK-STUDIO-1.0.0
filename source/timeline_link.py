"""Sparse audio edits preserve implicit video/audio pairs in legacy projects."""
from copy import deepcopy

from timeline_audio import source_audio_clips


def edit_keys(project, keys):
    """Expand linked pairs without affecting the continuous master recording."""
    keys = set(keys)
    if not project.timeline_link_enabled:
        return keys
    videos = {clip.id for clip in project.clips}
    audio = {clip.id for clip in source_audio_clips(project) if clip.has_audio is not False}
    for kind, identifier in tuple(keys):
        if kind == 'clip' and identifier in audio:
            keys.add(('camera_audio', identifier))
        elif kind == 'camera_audio' and identifier in videos:
            keys.add(('clip', identifier))
    return keys


def preserve_source_audio(project, identifiers):
    """Freeze audio before a picture-only edit, leaving other pairs implicit."""
    for clip in source_audio_clips(project):
        if clip.id in identifiers and clip.has_audio is not False and clip.id not in project.audio_overrides:
            project.audio_overrides[clip.id] = deepcopy(clip)


def move_source_audio(project, identifiers, delta, cameras=None):
    preserve_source_audio(project, identifiers)
    for identifier in identifiers:
        clip = project.audio_overrides.get(identifier)
        if clip is not None:
            clip.start += delta
            if cameras and identifier in cameras:
                clip.camera = cameras[identifier]


def delete_source_audio(project, identifiers):
    present = {clip.id for clip in source_audio_clips(project)}
    for identifier in identifiers:
        project.audio_overrides[identifier] = None
    return len(present.intersection(identifiers))
