"""Choose composition only where an outer layer or placement is visible."""
import math
from multicam_frame_clock import canonical_frame_rate
from video_placement import placement_values


def needs_stack_at(project, position, *, native_placement=False):
    clips = getattr(project, 'clips', ())
    fps = float(canonical_frame_rate(getattr(project, 'fps', 30.)))
    frame = math.floor(float(position)*fps+.5)
    def active(clip):
        return (getattr(clip, 'enabled', True)
                and math.floor(clip.start*fps+.5) <= frame < math.floor(clip.end*fps+.5))
    enabled = getattr(project, 'video_stack_enabled', False)
    order = getattr(project, 'multicam_outer_order', ())
    if enabled:
        if not order or getattr(project, 'multicam_inner_stack', False):
            if native_placement and not order:
                cameras=set(project.stacked_cameras())
                visible={clip.camera for clip in clips if clip.camera in cameras and active(clip)}
                if visible=={project.camera_at(position)}: return False
            return True  # Ordinary layer stacks retain their existing selection rules.
        outside = set(order)-{0}
        hidden = set(getattr(project, 'video_stack_hidden', ()))
        if any(clip.camera in outside-hidden and active(clip) for clip in clips):
            return True
    # A placement belonging to an inactive/outside recording must not switch
    # an otherwise ordinary programme into the compositor for the whole movie.
    if native_placement: return False
    camera = project.camera_at(position) if clips else None
    clip = project.clip_at(camera, position) if camera is not None else None
    from video_placement import placement_at
    return clip is not None and placement_at(project,camera,position) != (0.,0.,1.)
