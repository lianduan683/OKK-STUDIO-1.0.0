"""Source-space 16:9 crop rectangles shared by the editor and saved framing.

The source canvas uses square display pixels (including source SAR/rotation).
Coordinates never refer to an already cropped or padded programme picture.
"""
from __future__ import annotations

import math
from clip_transform import ClipTransform, validate_transform

ASPECT = 16 / 9
MAX_ZOOM = 4.0


def _size(size):
    width, height = map(float, size)
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise ValueError('裁切原画面尺寸无效。')
    return width, height


def maximum_crop(size):
    width, height = _size(size)
    crop_width = min(width, height * ASPECT)
    return crop_width, crop_width / ASPECT


def crop_at(size, width, center):
    """Clamp an exact-aspect rectangle, preserving its size while moving it."""
    sw, sh = _size(size)
    maximum, _ = maximum_crop(size)
    if not all(math.isfinite(float(v)) for v in (width, *center)):
        raise ValueError('裁切框位置无效。')
    width = min(maximum, max(maximum / MAX_ZOOM, float(width)))
    height = width / ASPECT
    left = min(sw-width, max(0., center[0]-width/2))
    top = min(sh-height, max(0., center[1]-height/2))
    return left, top, left+width, top+height


def rect_from_transform(transform, size):
    validate_transform(transform)
    sw, sh = _size(size)
    width, height = maximum_crop(size)
    width /= transform.zoom
    height /= transform.zoom
    left = (sw-width) * (transform.x+1) / 2
    top = (sh-height) * (transform.y+1) / 2
    return left, top, left+width, top+height


def transform_from_rect(rect, size):
    sw, sh = _size(size)
    left, top, right, bottom = map(float, rect)
    width, height = right-left, bottom-top
    maximum, _ = maximum_crop(size)
    tolerance = max(sw, sh) * 1e-6
    if (not all(math.isfinite(v) for v in rect) or width <= 0 or height <= 0
            or min(left, top) < -tolerance or right > sw+tolerance or bottom > sh+tolerance
            or abs(width / height-ASPECT) > 1e-5
            or width > maximum+tolerance or width < maximum/MAX_ZOOM-tolerance):
        raise ValueError('请选择画面内的 16:9 裁切区域。')
    zoom = min(MAX_ZOOM, max(1., maximum/width))
    x = 2*left/(sw-width)-1 if sw-width > tolerance else 0.
    y = 2*top/(sh-height)-1 if sh-height > tolerance else 0.
    result = ClipTransform('fill', zoom, min(1., max(-1., x)), min(1., max(-1., y)))
    validate_transform(result)
    return result


def move_crop(rect, delta, size):
    left, top, right, bottom = rect
    return crop_at(size, right-left, ((left+right)/2+delta[0], (top+bottom)/2+delta[1]))


def zoom_crop(rect, zoom, size):
    if not math.isfinite(float(zoom)) or not 1 <= zoom <= MAX_ZOOM:
        raise ValueError('缩放须在 100% 到 400% 之间。')
    maximum, _ = maximum_crop(size)
    return crop_at(size, maximum/zoom, ((rect[0]+rect[2])/2, (rect[1]+rect[3])/2))


def resize_crop(rect, handle, point, size):
    """Resize from any handle, keeping its opposite edge/corner anchored."""
    if handle not in ('nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'):
        raise ValueError('裁切框控制点无效。')
    sw, sh = _size(size)
    left, top, right, bottom = rect
    cx, cy = (left+right)/2, (top+bottom)/2
    maximum, _ = maximum_crop(size)
    minimum = maximum / MAX_ZOOM
    px, py = point
    if len(handle) == 2:
        sx = -1 if 'w' in handle else 1
        sy = -1 if 'n' in handle else 1
        ax, ay = (right if sx < 0 else left), (bottom if sy < 0 else top)
        dx, dy = sx*(px-ax), sy*(py-ay)
        desired = (dx+dy/ASPECT)/(1+1/ASPECT**2)
        available = min(sw-ax if sx > 0 else ax, (sh-ay if sy > 0 else ay)*ASPECT)
        width = min(available, max(minimum, desired))
        return crop_at(size, width, (ax+sx*width/2, ay+sy*width/(2*ASPECT)))
    if handle in ('e', 'w'):
        sx = 1 if handle == 'e' else -1
        ax = left if sx > 0 else right
        available = min(sw-ax if sx > 0 else ax, 2*min(cy, sh-cy)*ASPECT)
        width = min(available, max(minimum, sx*(px-ax)))
        return crop_at(size, width, (ax+sx*width/2, cy))
    sy = 1 if handle == 's' else -1
    ay = top if sy > 0 else bottom
    available = min(2*min(cx, sw-cx), (sh-ay if sy > 0 else ay)*ASPECT)
    width = min(available, max(minimum, sy*(py-ay)*ASPECT))
    return crop_at(size, width, (cx, ay+sy*width/(2*ASPECT)))


def draw_crop(anchor, point, size):
    width = max(abs(point[0]-anchor[0]), abs(point[1]-anchor[1])*ASPECT)
    return crop_at(size, width, ((point[0]+anchor[0])/2, (point[1]+anchor[1])/2))


def automatic_crop(size, bounds=None):
    """Fit the largest 16:9 rectangle inside the stable, detected content."""
    sw, sh = _size(size)
    left, top, right, bottom = bounds or (0., 0., sw, sh)
    maximum, _ = maximum_crop(size)
    width = min(right-left, (bottom-top)*ASPECT)
    width = max(maximum/MAX_ZOOM, min(maximum, width))
    return crop_at(size, width, ((left+right)/2, (top+bottom)/2))
