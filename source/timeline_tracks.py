"""Independent, persistent removal of video and audio timeline rows."""
from timeline_audio import source_audio_clips, audio_track_enabled
from timeline_link import delete_source_audio
from timeline_edits import delete_source_clips, finalize_source_edit


def video_tracks(project):
    occupied = {clip.camera for clip in project.clips}
    return [track for track in project.stacked_cameras(include_hidden=True)
            if f'V{track}' not in project.timeline_removed_tracks or track in occupied]


def audio_tracks(project, cameras):
    if not getattr(project, 'timeline_removed_tracks', ()):
        return ([0] if project.master_audio else []) + list(cameras)
    occupied = {clip.camera for clip in source_audio_clips(project) if clip.has_audio is not False}
    # Audio survives deletion of the corresponding picture row.
    order = list(dict.fromkeys([*cameras, *project.stacked_cameras(include_hidden=True)]))
    return ([0] if project.master_audio else []) + [track for track in order
        if f'A{track}' not in project.timeline_removed_tracks or track in occupied]


def track_empty(project, kind, track):
    if kind == 'V':
        return not any(clip.camera == track for clip in project.clips)
    if track == 0:
        return not bool(project.master_audio)
    return not any(clip.camera == track and clip.has_audio is not False
                   for clip in source_audio_clips(project))


def remove_tracks(project, kind, tracks):
    """Mutate a caller-owned candidate; preserve the other media type."""
    tracks = set(tracks)
    if not tracks: return
    states = {track: audio_track_enabled(project, track)
              for track in audio_tracks(project, video_tracks(project))}
    if kind == 'V':
        identifiers = {clip.id for clip in project.clips if clip.camera in tracks}
        linked = project.timeline_link_enabled
        project.timeline_link_enabled = False
        try:
            delete_source_clips(project, identifiers)
        finally:
            project.timeline_link_enabled = linked
        project.timeline_empty_tracks = [track for track in project.timeline_empty_tracks if track not in tracks]
    elif kind == 'A':
        delete_source_audio(project, {clip.id for clip in source_audio_clips(project) if clip.camera in tracks})
        if 0 in tracks:
            project.master_audio, project.master_duration = '', 0.
    else:
        raise ValueError('未知轨道类型')
    project.timeline_removed_tracks = sorted(set(project.timeline_removed_tracks) |
                                             {f'{kind}{track}' for track in tracks})
    # Retain the sound state of every surviving row, including legacy fallback.
    project.audio_track_enabled.update({track: enabled for track, enabled in states.items()
                                       if kind != 'A' or track not in tracks})
    if kind == 'A':
        for track in tracks: project.audio_track_enabled.pop(track, None)
    finalize_source_edit(project)
