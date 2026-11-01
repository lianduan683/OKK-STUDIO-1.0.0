"""Per-instance translation and uniform scale, after programme framing."""
import math


def placement_values(value):
    value = getattr(value, 'placement', value)
    if not isinstance(value, dict) or set(value)-{'x', 'y', 'scale'}:
        raise ValueError('视频位置设置包含未知字段或结构无效。')
    result = tuple(value.get(key, default) for key, default in (('x', 0.), ('y', 0.), ('scale', 1.)))
    for number, low, high in zip(result, (-2., -2., .01), (2., 2., 4.)):
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or not low <= number <= high):
            raise ValueError('位置须在画幅正负两倍范围内，缩放须在 1%–400% 之间。')
    return result


def has_placement(project):
    return (any(placement_values(clip) != (0., 0., 1.) for clip in getattr(project, 'clips', ()))
            or any(item.transform.placement for item in getattr(project,'clip_transforms',())))


def placement_at(project, camera, time):
    if camera == project.camera_at(time) and getattr(project,'clip_transforms',()):
        from clip_transform import transform_at
        value = transform_at(project,time).placement
        if value: return placement_values(value)
    clip = project.clip_at(camera, time)
    return placement_values(clip) if clip is not None else (0., 0., 1.)


def set_program_placement(project,start,end,value):
    """Replace a shot's placement while retaining its independent framing."""
    from copy import deepcopy
    from clip_transform import transform_at, set_transform_on_range
    from program_edits import _range, _frame
    first,last,fps=_range(project,start,end)
    values=placement_values(value)
    edges={first,last}
    for item in project.clip_transforms:
        edges.update(f for f in (_frame(item.start,fps),_frame(item.end,fps)) if first<f<last)
    edges=sorted(edges)
    for left,right in zip(edges,edges[1:]):
        transform=transform_at(project,left/fps)
        transform.placement=dict(zip(('x','y','scale'),values))
        set_transform_on_range(project,left/fps,right/fps,transform)


def placement_filter(values, width, height, *, pad_color='black'):
    """Frame-sized input is scaled about its centre, then translated.

    Positions are fractions of the output canvas, so preview and export at
    different resolutions retain the same composition. Padding happens before
    the final crop, preserving all available picture and transparent edges.
    """
    x, y, scale = values
    placement_values(dict(x=x, y=y, scale=scale))
    if values == (0., 0., 1.): return 'null'
    dx, dy = round(x*width), round(y*height)
    sw, sh = max(2, round(width*scale/2)*2), max(2, round(height*scale/2)*2)
    pw, ph = max(sw, width)+2*abs(dx), max(sh, height)+2*abs(dy)
    return (f'scale={sw}:{sh},pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:color={pad_color},'
            f'crop={width}:{height}:(iw-ow)/2-({dx}):(ih-oh)/2-({dy}):exact=1,setsar=1')
