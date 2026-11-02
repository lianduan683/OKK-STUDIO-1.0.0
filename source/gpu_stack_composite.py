"""GPU framing, ordered grading and encoded-light alpha composition.

The planner retains the CPU renderer's input clock and effect ordering. It
uses a CUDA-derived Vulkan device so the decode, Vulkan filters and encoder
share the same physical adapter. Software-only alpha codecs are uploaded once.
"""
from __future__ import annotations

from fractions import Fraction
from dataclasses import replace
import hashlib
import math
from pathlib import Path
import tempfile

from cache_paths import cache_dir
from clip_transform import ClipTransform, transform_at, transform_is_identity, validate_transform
from creative_core import escape_filter_path, grade_is_identity
from gpu_export import GpuExportUnsupported, _dimensions, write_grade_shader
from program_edits import grade_at
from video_placement import placement_values
from video_stack import pixel_format_has_alpha


def _frame(seconds, fps):
    return math.floor(float(seconds) * fps + .5)


def _number(value):
    return format(float(value), '.12g')


def framing_geometry(info, transform, width, height):
    """Source crop and destination rectangle, with the CPU's even geometry."""
    validate_transform(transform)
    if info.width <= 0 or info.height <= 0:
        raise GpuExportUnsupported('素材画幅未知，保留原构图路径。')
    if info.sample_aspect_ratio not in ('1:1', '1/1'):
        raise GpuExportUnsupported('非方形像素素材保留原构图路径。')
    ratio = info.width / info.height
    if transform.mode == 'fill':
        sw = math.ceil(max(width, height * ratio) * transform.zoom / 2) * 2
        sh = math.ceil(max(height, width / ratio) * transform.zoom / 2) * 2
    elif transform_is_identity(transform):
        # FFmpeg's identity scale first rounds the ratio-constrained integer
        # size, then enforces divisibility; active framing explicitly truncates.
        sw = max(2, math.floor(min(width, height * ratio) + .5) // 2 * 2)
        sh = max(2, math.floor(min(height, width / ratio) + .5) // 2 * 2)
    else:
        sw = max(2, math.floor(min(width, height * ratio) * transform.zoom / 2) * 2)
        sh = max(2, math.floor(min(height, width / ratio) * transform.zoom / 2) * 2)
    cw, ch = min(width, sw), min(height, sh)
    cx = round((sw - cw) * (transform.x + 1) / 2)
    cy = round((sh - ch) * (transform.y + 1) / 2)
    return dict(crop_x=cx * info.width / sw, crop_y=cy * info.height / sh,
                crop_w=cw * info.width / sw, crop_h=ch * info.height / sh,
                pos_x=(width-cw)//2, pos_y=(height-ch)//2, pos_w=cw, pos_h=ch)


def _rectangle_shader(geometry, fill, directory):
    x, y = geometry['pos_x'], geometry['pos_y']
    right, bottom = x + geometry['pos_w'], y + geometry['pos_h']
    alpha = '1.0' if fill == 'black' else '0.0'
    source = ('//!HOOK PREOUTPUT\n//!BIND HOOKED\n//!DESC Exact canvas rectangle\n'
              'vec4 hook(){vec4 c=HOOKED_tex(HOOKED_pos);vec2 p=HOOKED_pos*HOOKED_size;'
              f'if(p.x<{_number(x)} || p.x>={_number(right)} || p.y<{_number(y)} || p.y>={_number(bottom)})'
              f'c=vec4(0.0,0.0,0.0,{alpha});return c;}}\n')
    folder=Path(directory or cache_dir('gpu_export/shaders'));folder.mkdir(parents=True,exist_ok=True)
    target=folder/(hashlib.sha256(source.encode()).hexdigest()[:32]+'.hook')
    if not target.is_file():
        temporary=None
        try:
            with tempfile.NamedTemporaryFile('w',encoding='utf-8',suffix='.pending',dir=folder,delete=False) as handle:
                temporary=Path(handle.name);handle.write(source)
            temporary.replace(target);temporary=None
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)
    return str(target)


def _placebo(width, height, fmt, *, fill='black@0', geometry=None, grades=(), shader_dir=None,
             inputs=1, output=False, shader_override=None):
    options = [f'inputs={inputs}', f'w={width}', f'h={height}', f'format={fmt}',
               'fit_mode=fill', f'fillcolor={fill}', 'disable_linear=1', 'sigmoid=0',
               'upscaler=bicubic', 'downscaler=bicubic', 'deband=0', 'peak_detect=0',
               'apply_filmgrain=0', 'apply_dolbyvision=0', 'dithering=none',
               'color_primaries=bt709', 'color_trc=bt709',
               'colorspace=bt709' if output else 'colorspace=gbr',
               'range=tv' if output else 'range=pc']
    options.extend(f'{key}={_number(value)}' for key, value in (geometry or {}).items())
    if shader_override:
        options.append('custom_shader_path=' + escape_filter_path(shader_override))
    elif any(grade is not None and not grade_is_identity(grade) for grade in grades):
        shader = write_grade_shader(grades, shader_dir)
        options.append('custom_shader_path=' + escape_filter_path(shader))
    # This runtime emits premultiplied RGB whenever the target retains alpha.
    # Mark it explicitly: libplacebo's alpha_mode output option does not set
    # AVFrame alpha metadata, and a second pass would otherwise multiply twice.
    result = 'libplacebo=' + ':'.join(options)
    if not output:
        result += ',setparams=alpha_mode=premultiplied'
    if geometry and (geometry['pos_x'] > 0 or geometry['pos_y'] > 0
                     or geometry['pos_x'] + geometry['pos_w'] < width
                     or geometry['pos_y'] + geometry['pos_h'] < height):
        # This libplacebo version can rasterize an extra edge pixel for a
        # fractional scale. Enforce the same half-open rectangle as CPU pad.
        # The mask needs a full-canvas pass: the placement pass's hook uses
        # coordinates relative to its scaled content, not the output canvas.
        mask = _rectangle_shader(geometry, fill, shader_dir)
        result += ',' + _placebo(width,height,fmt,fill=fill,shader_override=mask)
    return result


def build_gpu_stack_composite_command(segment, project, base, settings, caps, width, height,
                                      pixel_format, rate_text, encode_args, chunk, ass_path=None, *,
                                      source_frame_rates=None, media_by_path=None, gpu_ffmpeg='',
                                      shader_dir=None, resident=True):
    """Return (command, backend, source_path, maximum_phase_prefix).

    Unsupported signals, effects or malformed metadata never silently lose an
    effect: the caller catches GpuExportUnsupported and retains the CPU graph.
    """
    _dimensions(width, height, pixel_format)
    if not gpu_ffmpeg or not settings.codec.endswith('_nvenc') or settings.decode_mode != 'cuda':
        raise GpuExportUnsupported('GPU 合成要求 CUDA/NVENC 导出设置。')
    if not caps.cuda_decode or not resident:
        raise GpuExportUnsupported('显存互通未通过验证，保留原合成路径。')
    if not segment.layers or len(segment.layers) > 8:
        raise GpuExportUnsupported('空画面或超过八层保留原合成路径。')
    if ass_path is not None:
        raise GpuExportUnsupported('烧录字幕保留原合成路径。')
    if type(segment.frames) is not int or segment.frames < 1:
        raise ValueError('GPU 合成帧数无效。')
    rate = Fraction(rate_text)
    if rate <= 0 or not math.isfinite(float(project.fps)) or project.fps <= 0:
        raise ValueError('GPU 合成帧率无效。')
    if type(settings.gpu_index) is not int or settings.gpu_index < 0:
        raise ValueError('GPU 设备编号无效。')
    from gpu_resident import supported
    if not supported(gpu_ffmpeg, settings.gpu_index):
        raise GpuExportUnsupported('显存互通运行验证失败，保留原合成路径。')
    media, rates = media_by_path or {}, source_frame_rates or {}
    outer = bool(getattr(project, 'multicam_outer_order', ()))
    local = None if outer else grade_at(project, segment.start)
    final_grades = [local, project.global_grade]
    for layer in segment.layers:
        info = media.get(layer.path)
        if info is None or (info.color_primaries, info.color_transfer, info.color_space,
                            info.color_range) != ('bt709', 'bt709', 'bt709', 'tv'):
            raise GpuExportUnsupported('GPU 多层合成当前只接受已标记的 BT.709 有限范围素材。')
        if getattr(info, 'has_display_transform', False) or info.field_order not in ('progressive', 'unknown'):
            raise GpuExportUnsupported('旋转或隔行素材保留原合成路径。')
        if layer.programme:
            if layer.has_alpha or info.has_alpha or pixel_format_has_alpha(info.pixel_format):
                raise GpuExportUnsupported('带透明通道的节目嵌套保留原处理顺序。')
    rgba = 'rgba64le' if pixel_format.endswith('10le') else 'rgba'
    surface = 'p010le' if pixel_format.endswith('10le') else 'nv12'
    command = [gpu_ffmpeg, *base[1:], '-init_hw_device', f'cuda=okk_cu:{settings.gpu_index}',
               '-init_hw_device', 'vulkan=okk_vk@okk_cu', '-filter_hw_device', 'okk_vk',
               '-filter_complex_threads', '1']
    graph = []
    prefixes = []
    all_hardware = True
    for index, layer in enumerate(reversed(segment.layers)):
        info = media[layer.path]
        alpha = layer.has_alpha or info.has_alpha or pixel_format_has_alpha(info.pixel_format)
        hardware = (not alpha and info.video_codec in {'h264', 'hevc', 'av1', 'vp9'}
                    and info.pixel_format in {'yuv420p', 'yuv420p10le', 'nv12', 'p010le'})
        all_hardware = all_hardware and hardware
        offset = max(0, _frame(segment.start-layer.phase_origin, project.fps))
        source_rate = rates.get(layer.path)
        quantum = (rate/source_rate).numerator if source_rate else None
        prefix = offset % quantum if quantum else offset
        prefixes.append(prefix)
        seek = max(0., layer.source_start-prefix/project.fps)
        command += ['-threads', '2']
        if hardware:
            command += ['-hwaccel', 'cuda', '-hwaccel_device', 'okk_cu', '-hwaccel_output_format', 'cuda']
        elif alpha and info.video_codec in ('vp8', 'vp9'):
            command += ['-c:v', {'vp8': 'libvpx', 'vp9': 'libvpx-vp9'}[info.video_codec]]
        command += ['-ss', f'{seek:.9f}', '-i', layer.path]
        normalize = (f'[{index}:v:0]setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,'
                     f'tpad=stop_mode=clone:stop_duration={segment.duration+prefix/project.fps+1:.9f},'
                     f'trim=start_frame={prefix}:end_frame={prefix+segment.frames},'
                     f'settb=AVTB,setpts=N/({rate_text}*TB),')
        transform = transform_at(project, segment.start) if layer.programme or not outer else ClipTransform()
        if hardware:
            from video_stack_render import _stack_prescale_safe
            if _stack_prescale_safe(layer, info, media.get(layer.original_path, info), transform, width, height):
                source_surface='p010le' if info.bit_depth > 8 else 'nv12'
                normalize += f'scale_cuda=w={width}:h={height}:interp_algo=bicubic:format={source_surface},'
                info=replace(info,width=width,height=height)
            normalize += 'hwupload=derive_device=vulkan,'
        else:
            normalize += f'format={rgba},hwupload,'
        pad = 'black' if layer.programme else 'black@0'
        geometry = framing_geometry(info, transform, width, height)
        normalize += _placebo(width, height, rgba, fill=pad, geometry=geometry, shader_dir=shader_dir)
        x, y, scale = layer.placement
        placement_values(dict(x=x, y=y, scale=scale))
        if tuple(layer.placement) != (0., 0., 1.):
            sw, sh = max(2, round(width*scale/2)*2), max(2, round(height*scale/2)*2)
            geometry = dict(pos_x=(width-sw)//2+round(x*width), pos_y=(height-sh)//2+round(y*height),
                            pos_w=sw, pos_h=sh)
            normalize += ',' + _placebo(width, height, rgba, fill=pad, geometry=geometry, shader_dir=shader_dir)
        grades = [project.camera_grades.get(layer.camera)]
        if layer.programme:
            grades.append(grade_at(project, segment.start))
        if any(g is not None and not grade_is_identity(g) for g in grades):
            normalize += ',' + _placebo(width, height, rgba, fill=pad, grades=grades, shader_dir=shader_dir)
        graph.append(normalize + f'[layer{index}]')
    inputs = ''.join(f'[layer{i}]' for i in range(len(segment.layers)))
    graph.append(inputs + _placebo(width, height, rgba, fill='black', inputs=len(segment.layers)) + '[blend]')
    graph.append('[blend]' + _placebo(width, height, surface, fill='black', grades=final_grades,
                                     shader_dir=shader_dir, output=True)
                 + f',setsar=1,settb=AVTB,setpts=N/({rate_text}*TB),hwupload=derive_device=cuda[picture]')
    command += ['-filter_complex', ';'.join(graph), '-map', '[picture]', '-an', '-sn', '-dn',
                *encode_args, '-r', rate_text, '-frames:v', str(segment.frames),
                '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv',
                '-map_metadata', '-1', '-video_track_timescale', str(rate.numerator), str(chunk)]
    backend = 'gpu_resident_composite' if all_hardware else 'gpu_composite_upload'
    return command, backend, segment.layers[0].path, max(prefixes, default=0)
