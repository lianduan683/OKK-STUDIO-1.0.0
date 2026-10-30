"""Paired, display-referred camera matching with measured candidate validation.

Matching edits the existing camera grade, so preview, render, undo and project
files use exactly the same transforms. No additional render layer is required.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path

from auto_color import analyze_rgb_samples, _sample_pixels
from creative_core import ColorGrade, GRADE_RANGES, WB_RANGES, grade_filter, validate_grade
from exporter_core import ExportCancelled, probe_media
from program_edits import grade_at, program_spans
from reference_color import _anchor, white_balance_plan

SAMPLE_WIDTH, SAMPLE_HEIGHT = 256, 144
METHODS = {'anchors': '中性色 / 肤色优先', 'overall': '整体画面（相似构图）'}
COHORTS = ('bright_neutral', 'neutral', 'skin')


@dataclass(frozen=True)
class CameraMatchResult:
    camera: int
    grade: ColorGrade | None
    samples: int = 0
    before: float = 0.
    after: float = 0.
    message: str = ''
    reference_pixels: bytes = b''
    target_pixels: bytes = b''
    matched_pixels: bytes = b''

    @property
    def improvement(self):
        return max(0., 100 * (1 - self.after / self.before)) if self.before > 1e-8 else 0.


def paired_sample_times(project, reference, target, start, end, maximum=5):
    """Stratify on real overlapping camera coverage, including recording gaps."""
    if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= project.duration + 1e-7:
        raise ValueError('机位匹配取样范围无效。')
    intervals = []
    fps = project.fps
    for first in project.clips:
        if first.camera != reference:
            continue
        for second in project.clips:
            if second.camera != target:
                continue
            left = max(start, first.start, second.start)
            right = min(end, first.start + first.duration, second.start + second.duration)
            a, b = math.ceil(left * fps - 1e-7), math.floor(right * fps + 1e-7)
            if b > a:
                intervals.append((a, b))
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    total = sum(b - a for a, b in merged)
    if not total:
        return ()
    count = min(maximum, total)
    positions = []
    for index in range(count):
        offset = min(total - 1, int((index + .5) * total / count))
        for a, b in merged:
            if offset < b - a:
                positions.append((a + offset) / fps)
                break
            offset -= b - a
    return tuple(sorted(set(positions)))


def reference_grade_time(project, camera, requested):
    if project.camera_at(requested) == camera:
        return requested
    spans = [span for span in program_spans(project) if span.camera == camera]
    if not spans:
        return None
    span = min(spans, key=lambda item: abs((item.start + item.end) / 2 - requested))
    return (span.start + span.end) / 2


def camera_filter(project, camera, time, *, override=None, reference_time=None):
    camera_grade = override if override is not None else project.camera_grades.get(camera)
    if reference_time is not None:
        local_time = reference_grade_time(project, camera, reference_time)
    else:
        local_time = time if project.camera_at(time) == camera else None
    local = grade_at(project, local_time) if local_time is not None else None
    return ','.join(value for grade in (camera_grade, local, project.global_grade)
                    if grade is not None for value in (grade_filter(grade),) if value)


def _read_frame(project, camera, time, ffmpeg, event, *, override=None, reference_time=None):
    if event is not None and event.is_set():
        raise ExportCancelled('已取消标准机位匹配')
    media = project.media_at(camera, time, use_proxies=False)
    if media is None or not Path(media[0]).is_file():
        raise ValueError('原片缺失或该时刻没有画面。')
    filters = camera_filter(project, camera, time, override=override, reference_time=reference_time)
    filters = ','.join(value for value in (filters,
        'scale=256:144:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=256:144:(ow-iw)/2:(oh-ih)/2:black') if value)
    pixels = _sample_pixels(ffmpeg, media[0], media[1], event, filters)
    if len(pixels) != SAMPLE_WIDTH * SAMPLE_HEIGHT * 3:
        raise ValueError('机位匹配抽样画面尺寸无效。')
    return pixels


def _luma(rgb):
    return sum(value * weight for value, weight in zip(rgb, (.2126, .7152, .0722)))


def _mean_rgb(analysis):
    count = sum(item[3] for item in analysis.rgb_bins)
    return tuple(sum(item[channel] * item[3] for item in analysis.rgb_bins) / max(1, count)
                 for channel in range(3))


def _common(target, reference):
    return [(name, _anchor(target, name), _anchor(reference, name)) for name in COHORTS
            if _anchor(target, name) is not None and _anchor(reference, name) is not None]


def _score(target, reference, names, method):
    if not target.usable:
        return float('inf')
    if method == 'overall':
        values = [(getattr(target, key), getattr(reference, key))
                  for key in ('luma_p10', 'luma_p50', 'luma_p90')]
        tone = sum(abs(a - b) for a, b in values) / 3
        first, second = _mean_rgb(target), _mean_rgb(reference)
        return tone + sum(abs(a / max(.03, _luma(first)) - b / max(.03, _luma(second)))
                          for a, b in zip(first, second)) / 6
    scores = []
    for name in names:
        first, second = _anchor(target, name), _anchor(reference, name)
        if first is None or second is None:
            return float('inf')
        y1, y2 = max(.02, _luma(first)), max(.02, _luma(second))
        color = sum(abs(a / y1 - b / y2) for a, b in zip(first, second)) / 3
        scores.append(abs(math.log2(y1 / y2)) * .35 + color)
    return sum(scores) / len(scores) if scores else float('inf')


def propose_grade(base, target, reference, *, strength=1., method='anchors', tone=True, anchor=None):
    if isinstance(strength, bool) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('机位匹配强度必须在 0 到 1 之间。')
    if method not in METHODS:
        raise ValueError('机位匹配方式无效。')
    validate_grade(base, check_lut=False)
    if strength == 0:
        return replace(base)
    base = base if base.enabled else ColorGrade()
    shared = _common(target, reference)
    if method == 'anchors':
        if not shared:
            raise ValueError('缺少共同中性色或肤色样本；可换取样范围，或在构图相似时使用整体画面匹配。')
        if anchor is not None:
            shared.sort(key=lambda item: item[0] != anchor)
            masked = {name + '_count': 0 for name in COHORTS if name != anchor}
            plan = white_balance_plan(replace(target, **masked), replace(reference, **masked))
        else:
            plan = white_balance_plan(target, reference)
        if not plan.used_reference:
            raise ValueError('标准机位有效样本不足，保留原调色。')
        gains = plan.gains
        _name, first, second = shared[0]
    else:
        first, second = _mean_rgb(target), _mean_rgb(reference)
        ratios = [b / max(.02, a) for a, b in zip(first, second)]
        ceiling = max(ratios)
        gains = tuple(max(.72, min(1., value / ceiling)) for value in ratios)
    balanced = tuple(a * b for a, b in zip(first, gains))
    exposure = math.log2(max(.02, _luma(second)) / max(.02, _luma(balanced)))
    gamma, contrast, saturation = 1., 1., 1.
    if tone and method == 'anchors' and len(shared) > 1:
        anchors = sorted((_luma(tuple(a * b for a, b in zip(x, gains))), _luma(y))
                         for _, x, y in shared)
        low, high = anchors[0], anchors[-1]
        if .03 < low[0] < high[0] - .08 and .03 < low[1] < high[1] - .08:
            gamma = max(.75, min(1.3, math.log(high[0] / low[0]) / math.log(high[1] / low[1])))
            exposure = math.log2(max(.02, low[1]) ** gamma / max(.02, low[0]))
        skin = next(((x, y) for name, x, y in shared if name == 'skin'), None)
        if skin:
            balanced_skin = tuple(a * b for a, b in zip(skin[0], gains))
            s1 = (max(balanced_skin) - min(balanced_skin)) / max(.02, max(balanced_skin))
            s2 = (max(skin[1]) - min(skin[1])) / max(.02, max(skin[1]))
            if s1 > .08:
                saturation = max(.9, min(1.12, s2 / s1))
    elif tone and method == 'overall':
        spread = target.luma_p90 - target.luma_p10
        if spread > .1:
            contrast = max(.85, min(1.18, (reference.luma_p90 - reference.luma_p10) / spread))
    # This is a candidate, not an unverified promise: the caller measures the
    # actual saved/rendered transform and only keeps improvements.
    updates = dict(exposure=base.exposure + max(-1.5, min(1.5, exposure)) * strength,
                   gamma=base.gamma * gamma ** strength,
                   contrast=base.contrast * contrast ** strength,
                   saturation=base.saturation * saturation ** strength, enabled=True)
    for key, gain in zip(WB_RANGES, gains):
        updates[key] = max(.5, min(2., getattr(base, key) * gain ** strength))
    for key in tuple(updates):
        if key in GRADE_RANGES:
            low, high = GRADE_RANGES[key]
            updates[key] = max(low, min(high, updates[key]))
    result = replace(base, **updates)
    validate_grade(result, check_lut=False)
    return result


def match_camera(project, reference, target, start, end, ffmpeg, ffprobe='', *,
                 strength=1., method='anchors', reference_time=0., cancel_event=None,
                 progress=None, reference_cache=None):
    if reference == target or reference not in project.cameras or target not in project.cameras:
        raise ValueError('标准机位与目标机位必须是不同的有效机位。')
    times = paired_sample_times(project, reference, target, start, end)
    if not times:
        return CameraMatchResult(target, None, message='取样范围内与标准机位没有同时录制的画面，已跳过。')
    cache = reference_cache if reference_cache is not None else {}
    first_frames, target_frames, used_times, notes = [], [], [], []
    for index, time in enumerate(times):
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled('已取消标准机位匹配')
        try:
            if ffprobe:
                for camera in (reference, target):
                    media = project.media_at(camera, time, use_proxies=False)
                    if media and ('probe', media[0]) not in cache:
                        cache['probe', media[0]] = probe_media(ffprobe, media[0])
                    info = cache.get(('probe', media[0])) if media else None
                    if info and info.color_transfer in {'smpte2084', 'arib-std-b67'}:
                        raise ValueError('HDR 素材需要先统一色彩空间，再进行机位匹配。')
            key = reference, time, reference_time
            if key not in cache:
                cache[key] = _read_frame(project, reference, time, ffmpeg, cancel_event,
                                         reference_time=reference_time)
            pixels = _read_frame(project, target, time, ffmpeg, cancel_event)
            first_frames.append(cache[key]); target_frames.append(pixels); used_times.append(time)
        except ExportCancelled:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            notes.append(str(error).split('\n')[0][:180])
        if progress:
            progress(40 * (index + 1) / len(times), f'同时间画面抽样 {index + 1}/{len(times)}')
    if len(used_times) < min(3, len(times)):
        return CameraMatchResult(target, None, samples=len(used_times),
            message='有效同步样本不足，已跳过。' + (' ' + notes[0] if notes else ''))
    kwargs = dict(sample_times=used_times, samples_requested=len(used_times))
    reference_analysis = analyze_rgb_samples(first_frames, **kwargs)
    target_analysis = analyze_rgb_samples(target_frames, **kwargs)
    if not reference_analysis.usable or not target_analysis.usable:
        return CameraMatchResult(target, None, samples=len(used_times), message='全黑、过曝或空白画面，保留原调色。')
    names = [name for name, _, _ in _common(target_analysis, reference_analysis)]
    before = _score(target_analysis, reference_analysis, names, method)
    if not math.isfinite(before):
        return CameraMatchResult(target, None, samples=len(used_times), message='缺少共同中性色或肤色样本，已跳过。')
    middle = len(used_times) // 2
    preview = dict(reference_pixels=first_frames[middle], target_pixels=target_frames[middle])
    if before < .012 or strength == 0:
        return CameraMatchResult(target, None, samples=len(used_times), before=before, after=before,
                                 message='与标准效果已接近，保留原调色。', matched_pixels=target_frames[middle], **preview)
    base = project.camera_grades.get(target, ColorGrade())
    if not base.enabled:
        base = ColorGrade()
    best_grade, best_score, best_frames = None, before, target_frames

    def measure(candidate):
        frames = [_read_frame(project, target, time, ffmpeg, cancel_event, override=candidate) for time in used_times]
        measured = analyze_rgb_samples(frames, **kwargs)
        score = _score(measured, reference_analysis, names, method)
        if (measured.rgb_peak_p995 >= .998 and target_analysis.rgb_peak_p995 < .98
                and reference_analysis.rgb_peak_p995 < .98):
            score = float('inf')
        return score, frames

    candidates = [(strength, True, None), (strength, False, None), (strength * .5, False, None)]
    if method == 'anchors':
        # Cohort membership can shift as a camera tint changes. Measure several
        # anchors against the complete common-cohort score instead of assuming
        # the brightest patch always represents the same object in both views.
        for name in names[1:]:
            candidates.extend(((strength, False, name), (strength, True, name)))
    for attempt, (amount, tone, anchor) in enumerate(candidates):
        candidate = propose_grade(base, target_analysis, reference_analysis, strength=amount,
                                  method=method, tone=tone, anchor=anchor)
        score, frames = measure(candidate)
        if score < best_score:
            best_grade, best_score, best_frames = candidate, score, frames
        if progress:
            progress(40 + 35 * (attempt + 1) / len(candidates), f'核对实际调色效果 {attempt + 1}/{len(candidates)}')
        if best_score < before * .28:
            break
    # Statistics change slightly when a corrected pixel enters or leaves a
    # neutral/skin cohort. A bounded coordinate search evaluates the real
    # transform, avoiding reliance on a single analytical exposure estimate.
    bounds = {'gamma': (.75, 1.3), 'contrast': (.9, 1.12), 'saturation': (.9, 1.12)}
    steps = (('gamma', .12), ('contrast', .05), ('exposure', .12), ('saturation', .06))
    refinement = 0
    if best_score > max(.012, before * .12):
        for scale in (1., .5):
            for key, step in steps:
                center = best_grade or base
                for direction in (-1, 1):
                    original, value = getattr(base, key), getattr(center, key)
                    if key == 'exposure':
                        value += direction * step * scale * strength
                        low, high = max(-4., original - 1.5 * strength), min(4., original + 1.5 * strength)
                    else:
                        value *= math.exp(direction * step * scale * strength)
                        low, high = (original * factor ** strength for factor in bounds[key])
                        allowed = GRADE_RANGES[key]
                        low, high = max(low, allowed[0]), min(high, allowed[1])
                    candidate = replace(center, **{key: max(low, min(high, value)), 'enabled': True})
                    score, frames = measure(candidate)
                    if score < best_score:
                        best_grade, best_score, best_frames = candidate, score, frames
                    refinement += 1
                    if progress:
                        progress(75 + 25 * refinement / 16, f'精细核对曝光、对比度与色彩 {refinement}/16')
                if best_score < max(.008, before * .10):
                    break
            if best_score < max(.008, before * .10):
                break
    improved = best_grade is not None and best_score < before * .97
    result = CameraMatchResult(target, best_grade if improved else None, len(used_times), before, best_score,
        ('已匹配；差异指标仅反映抽样画面。' if improved else '未找到可靠改善，保留原调色。') +
        (f' 部分样本跳过：{notes[0]}' if notes else ''),
        matched_pixels=best_frames[middle] if improved else target_frames[middle], **preview)
    return result
