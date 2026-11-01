"""User-facing camera numbers follow source-library order, without rewriting IDs."""
from __future__ import annotations


def source_camera_order(project):
    cameras = list(project.cameras)
    stacked = getattr(project, 'stacked_cameras', None)
    order = stacked(include_hidden=True) if stacked else cameras
    available = set(cameras)
    return [camera for camera in order if camera in available]


def camera_number(project, camera):
    if camera is None or camera == 0: return camera
    order = source_camera_order(project)
    stacked = getattr(project, 'stacked_cameras', None)
    if stacked:
        order += [value for value in stacked(include_hidden=True) if value not in order]
    return order.index(camera)+1 if camera in order else camera


def camera_id_for_number(project, number):
    try:
        index = int(number)-1
    except (ValueError, TypeError):
        raise ValueError('请选择有效的机位标号。') from None
    order = source_camera_order(project)
    if not 0 <= index < len(order): raise ValueError('这个机位还没有素材。')
    return order[index]
