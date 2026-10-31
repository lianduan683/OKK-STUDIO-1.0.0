"""Independent, frame-clock programme framing and square-pixel FFmpeg filters.

Ranges are half open. Crop coordinates describe the available crop travel:
zero is centred, -1 selects the left/top edge and +1 the right/bottom edge.
This module has no dependency on the project model or any preview UI.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Mapping

from program_edits import _clock, _frame, _number, _preserve_boundaries, _range


@dataclass(slots=True)
class ClipTransform:
    mode: str = "fit"
    zoom: float = 1.0
    x: float = 0.0
    y: float = 0.0


@dataclass(slots=True)
class ClipTransformRange:
    start: float
    end: float
    transform: ClipTransform


def validate_transform(transform: ClipTransform) -> None:
    if not isinstance(transform, ClipTransform) or transform.mode not in ("fit", "fill"):
        raise ValueError("片段构图模式必须是 fit 或 fill")
    for name, low, high in (("zoom", 1, 4), ("x", -1, 1), ("y", -1, 1)):
        value = _number(getattr(transform, name), "片段构图 " + name)
        if not low <= value <= high:
            raise ValueError(f"片段构图 {name} 必须在 {low} 到 {high} 之间")


def transform_is_identity(transform: ClipTransform) -> bool:
    validate_transform(transform)
    return (transform.mode == "fit" and transform.zoom == 1
            and transform.x == 0 and transform.y == 0)


def validate_clip_transforms(project) -> None:
    items = getattr(project, "clip_transforms", [])
    if not isinstance(items, list) or len(items) > 100_000:
        raise ValueError("片段构图必须是列表且数量不能超过 100000")
    ranges = []
    for item in items:
        if not isinstance(item, ClipTransformRange):
            raise ValueError("片段构图范围结构无效")
        first, stop, _ = _range(project, item.start, item.end)
        validate_transform(item.transform)
        ranges.append((first, stop))
    previous = -1
    for first, stop in sorted(ranges):
        if first < previous:
            raise ValueError("片段构图范围不能重叠")
        previous = stop


def transform_at(project, time: float) -> ClipTransform:
    """Return an independent value; missing/out-of-programme frames use fit."""
    fps, last = _clock(project)
    time = _number(time, "构图时间")
    frame = _frame(time, fps)
    if time >= 0 and 0 <= frame < last:
        for item in getattr(project, "clip_transforms", []):
            if _frame(item.start, fps) <= frame < _frame(item.end, fps):
                return deepcopy(item.transform)
    return ClipTransform()


def set_transform_on_range(project, start: float, end: float, transform: ClipTransform) -> None:
    """Replace only the selected frames, preserving both outside fragments.

    Identity clears local framing. No colour or source metadata is changed.
    Explicit edit boundaries remain available to the programme inspector.
    """
    from program_edits import validate_program_edits

    first, stop, fps = _range(project, start, end)
    validate_transform(transform)
    validate_program_edits(project)
    result = []
    for item in getattr(project, "clip_transforms", []):
        left, right = _frame(item.start, fps), _frame(item.end, fps)
        if right <= first or left >= stop:
            result.append(deepcopy(item))
            continue
        if left < first:
            result.append(ClipTransformRange(left / fps, first / fps, deepcopy(item.transform)))
        if right > stop:
            result.append(ClipTransformRange(stop / fps, right / fps, deepcopy(item.transform)))
    if not transform_is_identity(transform):
        result.append(ClipTransformRange(first / fps, stop / fps, deepcopy(transform)))
    project.clip_transforms = sorted(result, key=lambda item: item.start)
    _preserve_boundaries(project, (first, stop), fps, _frame(project.duration, fps))


def clip_transforms_to_dict(project) -> list[dict]:
    validate_clip_transforms(project)
    return [asdict(item) for item in getattr(project, "clip_transforms", [])]


def clip_transforms_from_dict(value) -> list[ClipTransformRange]:
    if not isinstance(value, list) or len(value) > 100_000:
        raise ValueError("片段构图必须是列表且数量不能超过 100000")
    result = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"start", "end", "transform"}:
            raise ValueError("片段构图包含未知字段或缺少范围")
        raw = item["transform"]
        if not isinstance(raw, Mapping) or set(raw) - {"mode", "zoom", "x", "y"}:
            raise ValueError("片段构图设置必须是对象且不能包含未知字段")
        transform = ClipTransform(**raw)
        validate_transform(transform)
        result.append(ClipTransformRange(_number(item["start"], "片段开始"),
                                         _number(item["end"], "片段结束"), transform))
    return result


def framing_filter(transform: ClipTransform, width: int, height: int, *, pad_color: str = "black") -> str:
    """Return scale/crop/pad to an even target canvas with output SAR 1:1.

    Identity retains the previous fit normalization verbatim. Active framing
    measures input *display* aspect (including anamorphic SAR) before setting
    square pixels. Fit scales the whole picture, then zooms/crops any excess
    and pads the remaining canvas; fill scales to cover without stretching.
    Place this chain before colour, subtitles and composited overlays. Stack
    layers can request ``pad_color='black@0'`` to retain transparent edges.
    """
    validate_transform(transform)
    if any(type(value) is not int or value < 2 or value % 2 for value in (width, height)):
        raise ValueError("构图目标宽高必须是至少 2 的偶数")
    if pad_color not in ("black", "black@0"):
        raise ValueError("构图填充颜色必须是 black 或 black@0")
    pad = f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={pad_color},setsar=1"
    if transform_is_identity(transform):
        return (f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                + pad)
    zoom = format(float(transform.zoom), ".12g")
    # Invalid/unspecified input SAR is interpreted as square pixels.
    dar = "(a*if(gt(sar,0),sar,1))"
    if transform.mode == "fill":
        scaled_w = f"ceil(max({width},{height}*{dar})*{zoom}/2)*2"
        scaled_h = f"ceil(max({height},{width}/{dar})*{zoom}/2)*2"
    else:
        scaled_w = f"max(2,trunc(min({width},{height}*{dar})*{zoom}/2)*2)"
        scaled_h = f"max(2,trunc(min({height},{width}/{dar})*{zoom}/2)*2)"
    crop_x = format((float(transform.x) + 1) / 2, ".12g")
    crop_y = format((float(transform.y) + 1) / 2, ".12g")
    return (f"scale=w='{scaled_w}':h='{scaled_h}',setsar=1,"
            f"crop=w='min(iw,{width})':h='min(ih,{height})':"
            f"x='(iw-ow)*{crop_x}':y='(ih-oh)*{crop_y}':exact=1," + pad)
