"""Frame-clock planning and alpha-preserving final rendering of camera layers.

This mode deliberately ignores programme camera cuts and their transitions.
Every active camera contributes a layer; original media and full-quality
treatments are used, never preview proxies. The final delivery remains opaque.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from pathlib import Path

from creative_core import grade_filter
from clip_transform import transform_at, transform_is_identity, framing_filter
from gyroflow_core import cache_matches
from library_overlay import overlay_cache_matches
from program_edits import grade_at
from subtitle_core import subtitle_filter
from video_stack import pixel_format_has_alpha
from video_placement import placement_values, placement_filter, placement_at


def _frame(seconds, fps):
    return math.floor(float(seconds) * fps + .5)


@dataclass(frozen=True, slots=True)
class StackLayer:
    camera: int
    path: str
    original_path: str
    source_start: float
    phase_origin: float
    has_alpha: bool = False
    programme: bool = False
    placement: tuple = (0., 0., 1.)
    proxy_disabled: bool = False


@dataclass(frozen=True, slots=True)
class StackSegment:
    start: float
    duration: float
    frames: int
    layers: tuple[StackLayer, ...]  # top to bottom
    # The common export scheduler treats these as a complex picture job.
    camera: int = 0
    path: None = None
    source_start: float = 0.0
    phase_origin: None = None

    @property
    def end(self):
        return self.start + self.duration


def _layer(project, clip, position):
    source = max(0.0, position - clip.source_origin)
    picture = clip.path
    origin = max(0.0, _frame(clip.source_origin, project.fps) / project.fps)
    stabilized = project.stabilization_at(clip.camera, position, clip.id)
    if stabilized is not None:
        if not Path(stabilized.cache_path).is_file() or not cache_matches(stabilized, clip.path):
            raise ValueError('防抖缓存缺失或失效，请重新生成或移除所选片段防抖。')
        source = max(0.0, source - stabilized.source_start)
        picture = stabilized.cache_path
        origin = max(0.0, clip.source_origin + stabilized.source_start)
    overlay = project.library_overlay_at(clip.camera, position, clip.id)
    if overlay is not None:
        if not overlay_cache_matches(overlay, project, strict=True):
            raise ValueError('素材叠加缓存失效，请重新应用或移除所选素材特效。')
        source = max(0.0, position - clip.source_origin - overlay.source_start)
        picture = overlay.cache_path
        origin = max(0.0, clip.source_origin + overlay.source_start)
    return StackLayer(clip.camera, picture, clip.path, source, origin, clip.has_alpha,
                      placement=placement_at(project,clip.camera,position), proxy_disabled=clip.proxy_disabled)


def plan_stack_segments(project, *, for_export=False):
    """Open only active layers, with half-open boundaries on one frame clock."""
    if getattr(project, 'multicam_outer_order', ()):
        from multicam_outer import plan_outer_segments
        return plan_outer_segments(project, continuous_preview=for_export)
    if not project.clips:
        return []
    fps = float(project.fps)
    end = project.duration if project.range_end is None else project.range_end
    first, last = _frame(project.range_start, fps), _frame(end, fps)
    if first >= last:
        raise ValueError('成片范围不足一帧')
    cameras = project.stacked_cameras()
    by_camera = {camera: [clip for clip in project.clips if clip.enabled and clip.camera == camera]
                 for camera in cameras}
    edges = {first, last}
    for clips in by_camera.values():
        for clip in clips:
            edges.update((_frame(clip.start, fps), _frame(clip.end, fps)))
    for items in (project.clip_grades, getattr(project, 'clip_transforms', []), project.stabilizations, project.library_overlays):
        for item in items:
            edges.update((_frame(item.start, fps), _frame(item.end, fps)))
    boundaries = sorted(value for value in edges if first <= value <= last)
    result = []
    for left, right in zip(boundaries, boundaries[1:]):
        layers = []
        for camera, clips in by_camera.items():
            clip = max((clip for clip in clips if _frame(clip.start, fps) <= left
                        and _frame(clip.end, fps) >= right),
                       key=lambda clip: (clip.start, clip.id), default=None)
            if clip is not None:
                layers.append(_layer(project, clip, left / fps))
        result.append(StackSegment(left / fps, (right-left) / fps, right-left, tuple(layers)))
    return result


def validate_stack_media(segments, media):
    """Do not flatten a transparent camera via an opaque treatment cache."""
    for segment in segments:
        for layer in segment.layers:
            original, treated = media[layer.original_path], media[layer.path]
            alpha = (layer.has_alpha or getattr(original, 'has_alpha', False)
                     or pixel_format_has_alpha(original.pixel_format))
            if alpha and layer.path != layer.original_path and not (
                    getattr(treated, 'has_alpha', False) or pixel_format_has_alpha(treated.pixel_format)):
                raise ValueError(f'机位 {layer.camera} 的防抖/素材特效缓存没有透明通道。'
                                 '请移除该处理或重新生成保留透明通道的素材后再叠加导出。')


def build_stack_gpu_command(segment, project, base, settings, caps, width, height,
                            pixel_format, rate_text, encode_args, chunk, ass_path=None, *,
                            source_frame_rates=None, media_by_path=None, gpu_ffmpeg='', shader_dir=None):
    """Bypass the alpha graph only for a verified, full-canvas opaque layer.

    Keep the CPU graph as the authority for unsupported composition. No proxy,
    grade, crop, placement, subtitle, or frame-phase adjustment may be omitted.
    """
    from creative_core import grade_is_identity
    from gpu_export import GpuExportUnsupported, build_gpu_video_plan, _signal_options
    from multicam_core import _cuda_segment_filter
    if not (settings.decode_mode == 'cuda' and settings.codec.endswith('_nvenc') and caps.cuda_decode):
        raise GpuExportUnsupported('此编码设置保留原合成路径。')
    if len(segment.layers) != 1:
        raise GpuExportUnsupported('多层/黑场保留完整合成。')
    layer = segment.layers[0]
    media = media_by_path or {}
    info = media.get(layer.path)
    original = media.get(layer.original_path, info)
    if info is None or any(getattr(value, 'has_alpha', False) or pixel_format_has_alpha(
            getattr(value, 'pixel_format', '')) for value in (info, original)) or layer.has_alpha:
        raise GpuExportUnsupported('透明画面保留完整合成。')
    if (info.video_codec not in {'h264', 'hevc', 'av1', 'vp9'}
            or info.pixel_format not in {'yuv420p', 'yuv420p10le', 'nv12', 'p010le'}
            or info.width <= 0 or info.height <= 0 or info.width*height != info.height*width
            or info.sample_aspect_ratio not in ('1:1', '1/1')
            or getattr(info, 'has_display_transform', False)
            or getattr(info, 'field_order', 'unknown') not in ('progressive', 'unknown')):
        raise GpuExportUnsupported('素材格式、补边或方向保留原处理。')
    # Preserve the source range. The reference CPU stack retains pc for these
    # full-range cameras; converting their GPU output to tv changes the look.
    if ((info.color_primaries, info.color_transfer, info.color_space)
            != ('bt709', 'bt709', 'bt709') or info.color_range not in ('tv', 'pc')):
        raise GpuExportUnsupported('此色彩空间保留已验证的原处理。')
    if tuple(layer.placement) != (0., 0., 1.):
        raise GpuExportUnsupported('画面位置/缩放保留完整合成。')
    outer = bool(getattr(project, 'multicam_outer_order', ()))
    if layer.programme or not outer:
        transform = transform_at(project, segment.start)
        # Fit/fill with no zoom have the same full canvas for equal aspects.
        # Equal display aspects and unit zoom leave no excess to crop. Stored
        # x/y offsets therefore have no visual effect and must not force an
        # otherwise supported opaque picture through the CPU alpha graph.
        if transform.zoom != 1:
            raise GpuExportUnsupported('片段构图保留完整合成。')
    grades = [project.camera_grades.get(layer.camera)]
    if layer.programme:
        grades.append(grade_at(project, segment.start))
    if not outer:
        grades.append(grade_at(project, segment.start))
    grades.append(project.global_grade)
    rate = Fraction(rate_text)
    offset = max(0, _frame(segment.start-layer.phase_origin, project.fps))
    source_rate = (source_frame_rates or {}).get(layer.path)
    quantum = (rate/source_rate).numerator if source_rate else None
    prefix = offset % quantum if quantum else offset
    seek = max(0., layer.source_start-prefix/project.fps)
    duration = segment.duration+prefix/project.fps
    neutral = all(grade is None or grade_is_identity(grade) for grade in grades)
    chain = (_cuda_segment_filter(info, settings, caps, width, height,
                10 if pixel_format.endswith('10le') else 8, rate_text,
                segment.frames, duration, prefix)
             if neutral and ass_path is None and info.color_range == 'tv' else None)
    if chain is not None:
        command = list(base)
        input_options = ['-hwaccel', 'cuda', '-hwaccel_device', str(settings.gpu_index),
                         '-hwaccel_output_format', 'cuda']
        color_options = _signal_options(info)[1]
        output_format = []
        backend = 'cuda_pipeline'
    else:
        if not gpu_ffmpeg:
            raise GpuExportUnsupported('GPU 调色组件不可用。')
        plan = build_gpu_video_plan(grades, ffmpeg_path=gpu_ffmpeg, width=width, height=height,
            pixel_format=pixel_format, rate_text=rate_text, frames=segment.frames, duration=duration,
            media=info, prefix_frames=prefix, decode_mode='cuda', shader_dir=shader_dir,
            gpu_index=settings.gpu_index, resident=ass_path is None)
        command = [gpu_ffmpeg, *base[1:], *plan.global_options, '-filter_threads', '1']
        input_options, color_options = plan.input_options, plan.color_options
        chain = plan.filter_chain
        if ass_path is not None:
            chain += (f',setpts=PTS+{segment.start:.12g}/TB,'+subtitle_filter(ass_path)
                      + ',setpts=PTS-STARTPTS')
        output_format = [] if getattr(plan,'gpu_resident',False) else ['-pix_fmt', pixel_format]
        backend = 'gpu_resident_grade' if getattr(plan,'gpu_resident',False) else 'gpu_stack_grade'
    command += [*input_options, '-ss', f'{seek:.9f}', '-i', layer.path,
                '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', chain, *encode_args,
                *output_format, '-r', rate_text, '-frames:v', str(segment.frames),
                *color_options, '-map_metadata', '-1', '-video_track_timescale', str(rate.numerator), str(chunk)]
    if prefix >= 120:
        from gpu_phase_seek import shorten_prefix
        command=shorten_prefix(command,getattr(caps,'ffprobe_path',''),segment.frames,
                               rate_text,prefix,settings.gpu_index)
    return command, backend, layer.path, prefix


def _stack_prescale_safe(layer, info, original, transform, width, height):
    """Prescale only a verified opaque, full-canvas downscale without reframing.

    CUDA resamples the original YUV values without a range conversion. The
    downloaded surface keeps the source depth and signal; all original CPU
    grade, alpha, subtitle and frame-clock operations remain downstream.
    """
    return (info is not None and not layer.has_alpha
            and not any(getattr(value, 'has_alpha', False) or pixel_format_has_alpha(
                getattr(value, 'pixel_format', '')) for value in (info, original))
            and info.width >= width and info.height >= height
            and (info.width, info.height) != (width, height)
            and info.width * height == info.height * width
            and info.sample_aspect_ratio in ('1:1', '1/1')
            and not getattr(info, 'has_display_transform', False)
            and getattr(info, 'field_order', 'unknown') in ('progressive', 'unknown')
            and (info.color_primaries, info.color_transfer, info.color_space)
                == ('bt709', 'bt709', 'bt709')
            and info.color_range in ('tv', 'pc')
            and tuple(layer.placement) == (0., 0., 1.) and transform.zoom == 1)


def build_stack_command(segment, project, base, width, height, pixel_format,
                        rate_text, encode_args, chunk, ass_path=None, *,
                        source_frame_rates=None, media_by_path=None, decode_mode='cpu', gpu_index=0,
                        decode_threads=1, filter_threads=1, cuda_prescale=False):
    """CPU alpha composition, followed by the chosen software/NVENC encoder."""
    rates = source_frame_rates or {}
    media = media_by_path or {}
    rate = Fraction(rate_text)
    deep = pixel_format.endswith('10le')
    alpha_format = 'yuva444p10le' if deep else 'rgba'
    overlay_format = 'yuv444p10' if deep else 'rgb'
    colour_format = 'yuv444p10le' if deep else 'rgb24'
    if decode_mode not in {'cpu', 'cuda'}: raise ValueError('图层解码模式无效。')
    if type(cuda_prescale) is not bool: raise ValueError('GPU 预缩放设置必须为布尔值。')
    if any(type(value) is not int or value < 1 for value in (decode_threads, filter_threads)):
        raise ValueError('图层处理线程数无效。')
    command = list(base) + ['-filter_complex_threads', str(filter_threads)]
    graph = [f'color=c=black:s={width}x{height}:r={rate_text},'
             f'format={colour_format},trim=end_frame={segment.frames},settb=AVTB,'
             f'setpts=N/({rate_text}*TB)[stack0]']
    for index, layer in enumerate(reversed(segment.layers)):
        offset = max(0, _frame(segment.start-layer.phase_origin, project.fps))
        source_rate = rates.get(layer.path)
        quantum = (rate / source_rate).numerator if source_rate else None
        prefix = offset % quantum if quantum else offset
        seek = max(0.0, layer.source_start-prefix / project.fps)
        info = media.get(layer.path)
        from clip_transform import ClipTransform
        transform = (transform_at(project, segment.start) if layer.programme or not getattr(project, 'multicam_outer_order', ())
                     else ClipTransform())
        alpha = layer.has_alpha or getattr(info, 'has_alpha', False) or pixel_format_has_alpha(
            getattr(info, 'pixel_format', ''))
        # NVDEC supplies the same decoded YUV samples. Download explicitly so
        # the existing CPU framing/colour/alpha graph stays unchanged.
        cuda = (decode_mode == 'cuda' and not alpha and info is not None
            and info.video_codec in {'h264', 'hevc', 'av1', 'vp9'}
            and info.pixel_format in {'yuv420p', 'yuv420p10le', 'nv12', 'p010le'})
        download = ''
        command += ['-threads', str(decode_threads)]
        if cuda:
            command += ['-hwaccel', 'cuda', '-hwaccel_device', str(gpu_index),
                        '-hwaccel_output_format', 'cuda']
            surface = 'p010le' if getattr(info, 'bit_depth', 8) > 8 else 'nv12'
            download = f'hwdownload,format={surface},'
            if cuda_prescale and _stack_prescale_safe(layer, info,
                    media.get(layer.original_path, info), transform, width, height):
                # Download 4K instead of an 8K surface, and avoid expanding the
                # entire 8K frame to 4:4:4 + alpha in system memory. The CPU
                # framing expression now has identical input/output geometry.
                download = (f'scale_cuda=w={width}:h={height}:interp_algo=bicubic:'
                            f'format={surface},' + download)
        command += ['-ss', f'{seek:.9f}']
        # The native VP8/9 decoders do not expose WebM's separate alpha stream.
        if info is not None and (getattr(info, 'has_alpha', False) or layer.has_alpha):
            decoder = {'vp8': 'libvpx', 'vp9': 'libvpx-vp9'}.get(info.video_codec)
            if decoder:
                command += ['-c:v', decoder]
        command += ['-i', layer.path]
        pad = 'black' if layer.programme else 'black@0'
        framing = (f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                   f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={pad},setsar=1'
                   if transform_is_identity(transform) else
                   framing_filter(transform, width, height, pad_color=pad))
        normalize = (f'[{index}:v:0]{download}setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,'
                     f'tpad=stop_mode=clone:stop_duration={segment.duration+prefix/project.fps+1:.9f},'
                     f'trim=start_frame={prefix}:end_frame={prefix+segment.frames},'
                     f'settb=AVTB,setpts=N/({rate_text}*TB),format={alpha_format},{framing},'
                     + placement_filter(layer.placement, width, height, pad_color=pad) + ',' +
                     f'format={alpha_format}')
        grades = [project.camera_grades.get(layer.camera)]
        if layer.programme:
            grades.append(grade_at(project, segment.start))
            normalize += f',format=rgb24,format={alpha_format}'
        effects = ','.join(filter(None, (grade_filter(grade) for grade in grades if grade is not None)))
        if effects:
            # RGB LUT/curves can discard alpha. Keep the original mask on a
            # separate branch and restore it after all per-layer colour work.
            graph += [normalize + f',split=2[colour{index}][maskin{index}]',
                      f'[maskin{index}]alphaextract[mask{index}]',
                      f'[colour{index}]{effects}[graded{index}]',
                      f'[graded{index}][mask{index}]alphamerge,format={alpha_format}[layer{index}]']
        else:
            graph.append(normalize + f'[layer{index}]')
        graph.append(f'[stack{index}][layer{index}]overlay=x=0:y=0:format={overlay_format}:'
                     f'alpha=straight:eof_action=pass:repeatlast=0[stack{index+1}]')
    # A ClipGrade belongs to a programme time range, not to any camera. Apply
    # it to the finished blend once; nonlinear curves cannot be distributed
    # across translucent layers without changing the picture.
    local = None if getattr(project, 'multicam_outer_order', ()) else grade_at(project, segment.start)
    final = ([grade_filter(local)] if local is not None else []) + [grade_filter(project.global_grade)]
    if ass_path is not None:
        final += [f'setpts=PTS+{segment.start:.12g}/TB', subtitle_filter(ass_path), 'setpts=PTS-STARTPTS']
    final += [f'format={pixel_format}']
    # The synthetic black canvas loses primaries/transfer metadata at overlay.
    # Restore the known signal on frames (encoder flags alone are insufficient
    # for this NVENC runtime), so CPU and GPU chunks concatenate consistently.
    if segment.layers and all((getattr(media.get(layer.path), 'color_primaries', None),
            getattr(media.get(layer.path), 'color_transfer', None),
            getattr(media.get(layer.path), 'color_space', None),
            getattr(media.get(layer.path), 'color_range', None)) == ('bt709', 'bt709', 'bt709', 'tv')
            for layer in segment.layers):
        final += ['setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=limited']
    graph.append(f'[stack{len(segment.layers)}]' + ','.join(filter(None, final)) + '[picture]')
    command += ['-filter_complex', ';'.join(graph), '-map', '[picture]', '-an', '-sn', '-dn']
    command += list(encode_args)
    command += ['-pix_fmt', pixel_format, '-r', rate_text, '-frames:v', str(segment.frames),
                '-map_metadata', '-1', '-video_track_timescale', str(rate.numerator), str(chunk)]
    return command
