"""OKK Effect Contract 1.0: declarative packages, instances and native adapters.

Packages never contain executable entry points. Loading/compiling does not
modify the project, start a renderer, or silently approximate unsupported data.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
import re
from typing import Protocol

from creative_core import (ColorGrade, GRADE_RANGES, SKIN_RANGES, WB_RANGES,
                           WHEEL_RANGES, Transition, TRANSITION_KINDS, validate_grade)
from effects_core import (EffectSettings, EFFECT_RANGES, ALLOWED_ROTATIONS,
                          validate_effects)
from transitions_core import TransitionSpec, XFADE_MODES, default_transition_duration

SCHEMA = 'okk.effect/1.0'
INSTANCE_SCHEMA = 'okk.effect-instance/1.0'
ENGINES = ('okk.grade.v1', 'okk.effects.v1', 'okk.overlay.v1',
           'okk.xfade.v1', 'okk.mask.v1')
FIDELITIES = ('native', 'mapped', 'approximate', 'baked')
MAX_JSON_BYTES = 1024 * 1024
_ID = r'[a-z][a-z0-9_]*(?:[.\-][a-z0-9_]+)+'
_VERSION = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
_PARAM_ID = r'[a-z][a-z0-9_]*'


class ContractError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f'{code}: {message}')


def _fail(code, message):
    raise ContractError(code, message)


def _object(value, required, optional=(), label='对象'):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        _fail('E_SCHEMA', f'{label}必须是 JSON 对象')
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        _fail('E_SCHEMA', f'{label}缺少字段 {sorted(missing)}；未知字段 {sorted(unknown)}')


def _text(value, label, pattern=None, allow_empty=False):
    if (not isinstance(value, str) or (not value.strip() and not allow_empty)
            or any(c in value for c in '\x00\r\n')
            or (pattern is not None and not re.fullmatch(pattern, value))):
        _fail('E_SCHEMA', f'{label}无效')


def _number(value, label, minimum=-1e12, maximum=1e12, integer=False):
    if (type(value) not in (int, float) or not minimum <= value <= maximum
            or not math.isfinite(value) or (integer and type(value) is not int)):
        _fail('E_PARAMETER', f'{label}必须是 {minimum:g}–{maximum:g} 范围内的有限'
              + ('整数' if integer else '数字'))
    return value


def _rate(value):
    _object(value, ('num', 'den'), label='帧率')
    num = _number(value['num'], '帧率分子', 1, 1_000_000, integer=True)
    den = _number(value['den'], '帧率分母', 1, 1_000_000, integer=True)
    rate = Fraction(num, den)
    if not Fraction(1, 1000) <= rate <= 1000:
        _fail('E_TIMING', '帧率必须在 0.001–1000 FPS 之间')
    return rate


def read_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        _fail('E_SCHEMA', 'JSON 超过 1 MiB')

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail('E_SCHEMA', f'JSON 字段重复：{key}')
            result[key] = value
        return result

    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_pairs,
                          parse_constant=lambda value: _fail('E_SCHEMA', f'非法常量：{value}'))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError('E_SCHEMA', '无法读取 JSON') from exc


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _relative_path(value):
    _text(value, '资源相对路径')
    if ('\\' in value or ':' in value or PureWindowsPath(value).is_absolute()
            or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/'))):
        _fail('E_ASSET_PATH', f'资源路径必须是包内 POSIX 相对路径：{value}')


def resolve_asset(root, asset):
    _relative_path(asset['path'])
    root = Path(root).resolve()
    target = (root / asset['path']).resolve()
    if not target.is_relative_to(root):
        _fail('E_ASSET_PATH', f'资源路径越界：{asset["path"]}')
    if not target.is_file():
        _fail('E_ASSET_MISSING', f'找不到资源：{asset["path"]}')
    if file_sha256(target) != asset['sha256']:
        _fail('E_ASSET_HASH', f'资源内容已改变：{asset["path"]}')
    return target


def _parameter_value(spec, value, name):
    kind = spec['type']
    if kind in ('number', 'integer'):
        return _number(value, name, spec['min'], spec['max'], integer=kind == 'integer')
    if kind == 'boolean':
        if type(value) is not bool:
            _fail('E_PARAMETER', f'{name}必须是布尔值')
    elif kind == 'enum':
        if not any(type(value) is type(choice) and value == choice for choice in spec['choices']):
            _fail('E_PARAMETER', f'{name}不在枚举选项中')
    return value


def _parameter_definition(name, spec):
    _text(name, '参数 ID', _PARAM_ID)
    _object(spec, ('type', 'label', 'unit', 'default', 'animatable'),
            ('min', 'max', 'choices'), f'参数 {name}')
    for key in ('label', 'unit'):
        _text(spec[key], f'{name}.{key}')
    if spec['animatable'] is not False:
        _fail('E_UNSUPPORTED_ANIMATION', 'v1 原生适配器只接受静态参数')
    kind = spec['type']
    if kind in ('number', 'integer'):
        if not {'min', 'max'} <= spec.keys() or 'choices' in spec:
            _fail('E_SCHEMA', f'{name}数值参数需要 min/max，不能设置 choices')
        _number(spec['min'], name + '.min')
        _number(spec['max'], name + '.max', minimum=spec['min'])
    elif kind == 'enum':
        choices = spec.get('choices')
        if (not isinstance(choices, list) or not choices or 'min' in spec or 'max' in spec
                or any(type(c) not in (str, int) for c in choices)
                or len({(type(c), c) for c in choices}) != len(choices)):
            _fail('E_SCHEMA', f'{name}枚举选项无效')
        for choice in choices:
            if isinstance(choice, str):
                _text(choice, name + '.choices')
    elif kind == 'boolean':
        if {'min', 'max', 'choices'} & spec.keys():
            _fail('E_SCHEMA', f'{name}布尔参数不能设置数值范围或枚举')
    else:
        _fail('E_SCHEMA', f'不支持的参数类型：{kind}')
    _parameter_value(spec, spec['default'], name)


def _bindings(manifest):
    """Validate parameter identity, type, unit and *whole* declared range."""
    engine = manifest['implementation']['engine']
    grade_ranges = GRADE_RANGES | SKIN_RANGES | WB_RANGES | WHEEL_RANGES
    effects_ranges = {k: v[:2] for k, v in EFFECT_RANGES.items()
                      if k != 'overlay_opacity'}
    for name, spec in manifest['parameters'].items():
        numeric = (grade_ranges if engine == 'okk.grade.v1' else
                   effects_ranges if engine == 'okk.effects.v1' else
                   {'opacity': (0, 1)} if engine == 'okk.overlay.v1' else {})
        if name in numeric:
            unit = ('ev' if name == 'exposure' else 'ratio') if engine == 'okk.grade.v1' else (
                'seconds' if name in ('fade_in', 'fade_out') else
                'ratio' if name == 'opacity' else 'percent')
            low, high = numeric[name]
            if (spec['type'] not in ('number', 'integer') or spec['unit'] != unit
                    or spec['min'] < low or spec['max'] > high):
                _fail('E_PARAMETER_BINDING', f'{name}的类型、单位或范围与 {engine} 不符')
        elif engine == 'okk.effects.v1' and name in ('hflip', 'vflip'):
            if spec['type'] != 'boolean' or spec['unit'] != 'boolean':
                _fail('E_PARAMETER_BINDING', f'{name}必须是 boolean 参数')
        elif engine == 'okk.effects.v1' and name == 'rotation':
            if (spec['type'] != 'enum' or spec['unit'] != 'degrees'
                    or any(type(c) is not int or c not in ALLOWED_ROTATIONS for c in spec['choices'])):
                _fail('E_PARAMETER_BINDING', 'rotation 只支持 0/90/180/270 度')
        else:
            _fail('E_PARAMETER_BINDING', f'{engine}没有参数 {name}')


def validate_manifest(value):
    _object(value, ('schema', 'id', 'version', 'name', 'kind', 'category', 'description',
                    'source', 'render', 'implementation', 'parameters', 'assets'))
    if value['schema'] != SCHEMA:
        _fail('E_VERSION', f'只支持 {SCHEMA}')
    _text(value['id'], '效果 ID', _ID)
    _text(value['version'], '效果版本', _VERSION)
    for key in ('name', 'category', 'description'):
        _text(value[key], key, allow_empty=key == 'description')
    source = value['source']
    _object(source, ('application', 'format', 'original_id', 'fidelity', 'notes'), label='来源')
    for key in ('application', 'format', 'original_id'):
        _text(source[key], 'source.' + key, allow_empty=key == 'original_id')
    if source['fidelity'] not in FIDELITIES or not isinstance(source['notes'], list):
        _fail('E_SCHEMA', '来源必须声明转换程度和说明')
    for note in source['notes']:
        _text(note, '转换说明')
    impl = value['implementation']
    _object(impl, ('engine',), ('asset', 'mode', 'blend'), '渲染实现')
    engine = impl['engine']
    if engine not in ENGINES:
        _fail('E_BACKEND', f'没有注册渲染适配器：{engine}')
    transition = engine in ('okk.xfade.v1', 'okk.mask.v1')
    if value['kind'] != ('transition' if transition else 'effect'):
        _fail('E_SCHEMA', '效果类型与渲染器不匹配')
    render = value['render']
    _object(render, ('inputs', 'color_space', 'alpha', 'temporal_radius'), label='渲染约定')
    if (type(render['inputs']) is not int or render['inputs'] != (2 if transition else 1)
            or render['color_space'] != 'rec709-display' or render['alpha'] != 'straight'
            or type(render['temporal_radius']) is not int or render['temporal_radius'] != 0):
        _fail('E_CAPABILITY', 'v1 需要 Rec.709 显示域、直通 Alpha，且不支持时间邻域采样')
    assets = value['assets']
    _object(assets, (), assets.keys() if isinstance(assets, dict) else (), '资源表')
    seen = set()
    for name, asset in assets.items():
        _text(name, '资源 ID', _PARAM_ID)
        _object(asset, ('path', 'sha256', 'role'), ('fps',), f'资源 {name}')
        _relative_path(asset['path'])
        _text(asset['sha256'], '资源 SHA256', r'[0-9a-f]{64}')
        role = asset['role']
        _text(role, '资源类型')
        suffixes = {'overlay': {'.mov', '.mp4', '.mkv', '.webm'}, 'mask': {'.png'},
                    'lut': {'.cube', '.3dl'}}
        if role not in suffixes or Path(asset['path']).suffix.lower() not in suffixes[role]:
            _fail('E_ASSET_FORMAT', f'资源 {name}的文件格式不受支持')
        if asset['path'].casefold() in seen:
            _fail('E_SCHEMA', '资源路径重复')
        seen.add(asset['path'].casefold())
        if role == 'overlay':
            if 'fps' not in asset:
                _fail('E_SCHEMA', '叠加视频必须声明帧率')
            _rate(asset['fps'])
        elif 'fps' in asset:
            _fail('E_SCHEMA', '静态资源不能声明帧率')
    allowed = {'engine'}
    role = None
    if engine == 'okk.grade.v1':
        allowed.add('asset')
        role = 'lut' if 'asset' in impl else None
    elif engine in ('okk.overlay.v1', 'okk.mask.v1'):
        allowed.add('asset')
        role = 'overlay' if engine == 'okk.overlay.v1' else 'mask'
    if role and (not isinstance(impl.get('asset'), str)
                 or impl['asset'] not in assets or assets[impl['asset']]['role'] != role):
        _fail('E_ASSET_MISSING', f'{engine}需要一个 {role} 资源引用')
    if engine == 'okk.overlay.v1':
        allowed.add('blend')
        if impl.get('blend') not in ('alpha', 'screen'):
            _fail('E_CAPABILITY', '叠加混合模式只支持 alpha/screen')
    if engine == 'okk.xfade.v1':
        allowed.add('mode')
        _text(impl.get('mode'), 'xfade 模式')
        if impl.get('mode') not in XFADE_MODES:
            _fail('E_CAPABILITY', '当前 FFmpeg 转场适配器不支持该模式')
    if impl.keys() - allowed:
        _fail('E_SCHEMA', f'{engine}包含无效配置字段')
    params = value['parameters']
    _object(params, (), params.keys() if isinstance(params, dict) else (), '参数表')
    for name, spec in params.items():
        _parameter_definition(name, spec)
    _bindings(value)
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class EffectPackage:
    root: Path
    manifest: dict


def load_package(directory) -> EffectPackage:
    root = Path(directory).resolve()
    manifest = validate_manifest(read_json(root / 'effect.json'))
    for asset in manifest['assets'].values():
        resolve_asset(root, asset)
    return EffectPackage(root, manifest)


def validate_instance(package, value):
    _object(value, ('schema', 'id', 'effect_id', 'effect_version', 'enabled',
                    'fps', 'timing', 'parameters', 'keyframes'), label='效果实例')
    if value['schema'] != INSTANCE_SCHEMA:
        _fail('E_VERSION', f'只支持 {INSTANCE_SCHEMA}')
    _text(value['id'], '实例 ID')
    manifest = package.manifest
    if (value['effect_id'], value['effect_version']) != (manifest['id'], manifest['version']):
        _fail('E_VERSION', '实例的效果 ID/版本必须与效果包完全一致')
    if type(value['enabled']) is not bool:
        _fail('E_SCHEMA', 'enabled 必须是布尔值')
    fps = _rate(value['fps'])
    if manifest['kind'] == 'transition' and not 1 <= fps <= 120:
        _fail('E_CAPABILITY', '当前独立转场渲染器只支持 1–120 FPS')
    anchor = 'cut_frame' if manifest['kind'] == 'transition' else 'start_frame'
    timing = value['timing']
    _object(timing, (anchor, 'duration_frames'), label='实例时间')
    _number(timing[anchor], anchor, 0, 10**12, integer=True)
    frames = _number(timing['duration_frames'], 'duration_frames', 1, 10**12, integer=True)
    if anchor == 'cut_frame' and timing[anchor] * 2 < frames:
        _fail('E_TIMING', '转场窗口不能超出时间线开头')
    overrides = value['parameters']
    _object(overrides, (), manifest['parameters'].keys(), '实例参数')
    if not isinstance(value['keyframes'], dict):
        _fail('E_SCHEMA', 'keyframes 必须是对象')
    if value['keyframes']:
        _fail('E_UNSUPPORTED_ANIMATION', '当前适配器不能执行参数关键帧，请烘焙或扩展适配器')
    params = {name: _parameter_value(spec, overrides.get(name, spec['default']), name)
              for name, spec in manifest['parameters'].items()}
    return params, fps, float(Fraction(timing[anchor], 1) / fps), float(Fraction(frames, 1) / fps)


@dataclass(frozen=True, slots=True)
class CompiledEffect:
    engine: str
    native: ColorGrade | EffectSettings | TransitionSpec | None
    anchor_seconds: float
    duration_seconds: float
    asset_dir: Path
    fingerprint: str
    warnings: tuple[str, ...]

    def program_transition(self) -> Transition:
        """Adapt to the programme's centred clock; validate handles in the host."""
        spec = self.native
        if not isinstance(spec, TransitionSpec):
            _fail('E_HOST_CAPABILITY', '该实例不是启用的转场')
        mode = spec.xfade_mode if spec.kind == 'xfade' else 'fade'
        if mode not in TRANSITION_KINDS:
            _fail('E_HOST_CAPABILITY', '节目时间线暂不支持此 xfade 模式；请使用独立转场渲染器')
        return Transition(self.anchor_seconds, mode, spec.duration,
                          str(self.asset_dir / spec.mask_file) if spec.kind == 'mask' else '')


def compile_effect(package: EffectPackage, instance: dict, *, base_grade=None) -> CompiledEffect:
    """The shared preview/export boundary. No parameter clamping or execution."""
    manifest = validate_manifest(package.manifest)
    package = EffectPackage(package.root, manifest)
    paths = {name: resolve_asset(package.root, asset) for name, asset in manifest['assets'].items()}
    params, _fps, anchor, duration = validate_instance(package, instance)
    impl = manifest['implementation']
    engine = impl['engine']
    try:
        if engine == 'okk.grade.v1':
            native = copy.deepcopy(base_grade) if base_grade is not None else ColorGrade()
            for name, value in params.items():
                setattr(native, name, value)
            if 'asset' in impl:
                native.lut_path = str(paths[impl['asset']])
            native.enabled = True
            validate_grade(native)
        elif engine == 'okk.effects.v1':
            native = validate_effects(EffectSettings(**params), duration=duration)
        elif engine == 'okk.overlay.v1':
            asset = manifest['assets'][impl['asset']]
            native = validate_effects(EffectSettings(
                overlay=manifest['name'], overlay_path=str(paths[impl['asset']]),
                overlay_kind=impl['blend'], overlay_fps=float(_rate(asset['fps'])),
                overlay_opacity=params.get('opacity', 1.0) * 100), duration=duration)
            # The legacy effect renderer supports 1–240 FPS; do not let its
            # normalizer silently clamp a package's declared source clock.
            if native.overlay_fps != float(_rate(asset['fps'])):
                _fail('E_CAPABILITY', '叠加渲染器只支持 1–240 FPS 的素材')
        else:
            duration = default_transition_duration(duration)
            native = TransitionSpec(
                kind='mask' if engine == 'okk.mask.v1' else 'xfade',
                label=manifest['name'], duration=duration,
                xfade_mode=impl.get('mode', 'fade'),
                mask_file=manifest['assets'][impl['asset']]['path'] if 'asset' in impl else '')
    except ContractError:
        raise
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError('E_NATIVE_VALIDATION', str(exc)) from exc
    canonical = json.dumps({'adapter': 'okk-contract-1.0', 'package': manifest,
                            'instance': instance, 'native': asdict(native)}, sort_keys=True, ensure_ascii=True,
                           separators=(',', ':'), allow_nan=False)
    warnings = tuple(manifest['source']['notes'])
    if manifest['source']['fidelity'] in ('approximate', 'baked'):
        warnings = (f'转换程度：{manifest["source"]["fidelity"]}',) + warnings
    return CompiledEffect(engine, native if instance['enabled'] else None, anchor, duration,
                          Path(package.root).resolve(), hashlib.sha256(canonical.encode()).hexdigest(), warnings)


@dataclass(frozen=True, slots=True)
class ConversionReport:
    """Converters must report unsupported source features before publishing."""
    fidelity: str
    mapped: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    package_path: Path | None = None


class SourceAdapter(Protocol):
    """Extension seam for version-specific PR/Fusion parsers, not a claim of support."""
    def inspect(self, source: Path) -> ConversionReport: ...
    def convert(self, source: Path, destination: Path) -> ConversionReport: ...


def capabilities():
    return {'schema': SCHEMA, 'instance_schema': INSTANCE_SCHEMA, 'engines': list(ENGINES),
            'parameter_animation': False, 'color_spaces': ['rec709-display'],
            'overlay_blends': ['alpha', 'screen'], 'overlay_fps': [1, 240],
            'transition_fps': [1, 120], 'transition_seconds': [0.1, 4.0],
            'program_xfade_modes': list(TRANSITION_KINDS),
            'xfade_modes': sorted(XFADE_MODES), 'raw_source_importers': []}
