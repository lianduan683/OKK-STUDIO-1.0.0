"""Validate/compile OKK effect packages or wrap already-exported assets.

Example: python effect_package_cli.py capabilities
This utility does not parse .prfpset/.setting or host third-party binaries.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from fractions import Fraction
import json
from pathlib import Path
import shutil
import tempfile

from effect_contract import (SCHEMA, ContractError, EffectPackage, capabilities,
                             compile_effect, file_sha256, load_package, read_json,
                             validate_manifest)


def create_asset_package(source, destination, *, effect_id, name, kind,
                         application, source_format, fps=None, blend='alpha') -> EffectPackage:
    """Publish a new directory atomically; existing packages are never overwritten.

    Callers must probe media beforehand. fps is the exported overlay's CFR, not
    its originating Resolve/Premiere project's clock. No transcoding is done.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ContractError('E_ASSET_MISSING', '输入素材不存在')
    if destination.exists():
        raise ContractError('E_DESTINATION', '目标目录已存在，请使用新目录或新版本')
    if kind not in ('overlay', 'mask', 'lut'):
        raise ContractError('E_ASSET_FORMAT', 'kind 必须为 overlay/mask/lut')
    if kind == 'overlay':
        try:
            rate = Fraction(fps)
        except (ValueError, TypeError, ZeroDivisionError) as exc:
            raise ContractError('E_TIMING', '叠加素材必须提供导出帧率，例如 30000/1001') from exc
    else:
        rate = None
        if fps is not None:
            raise ContractError('E_TIMING', '静态资源不接受 fps')
    engines = {'overlay': 'okk.overlay.v1', 'mask': 'okk.mask.v1', 'lut': 'okk.grade.v1'}
    asset = {'path': 'assets/media' + source.suffix.lower(),
             'sha256': file_sha256(source), 'role': kind}
    if rate is not None:
        asset['fps'] = {'num': rate.numerator, 'den': rate.denominator}
    notes = {
        'overlay': ['由源软件导出的素材封装；只保留素材内动画和不透明度，不能编辑源节点。',
                    '叠加素材按声明帧率从头播放并循环，铺满主画面；不包含音频。'],
        'mask': ['静态灰度排序遮罩采用线性阈值擦除，不能还原原转场的变形、运动模糊或时间动画。'],
        'lut': ['只映射 LUT 色彩变换，不包含空间效果、遮罩、跟踪和时间效果。'],
    }
    manifest = {
        'schema': SCHEMA, 'id': effect_id, 'version': '1.0.0', 'name': name,
        'kind': 'transition' if kind == 'mask' else 'effect',
        'category': {'overlay': '叠加', 'mask': '遮罩转场', 'lut': '调色风格'}[kind],
        'description': f'{application} 导出素材',
        'source': {'application': application, 'format': source_format, 'original_id': '',
                   'fidelity': {'overlay': 'baked', 'mask': 'approximate', 'lut': 'mapped'}[kind],
                   'notes': notes[kind]},
        'render': {'inputs': 2 if kind == 'mask' else 1, 'color_space': 'rec709-display',
                   'alpha': 'straight', 'temporal_radius': 0},
        'implementation': {'engine': engines[kind], 'asset': 'media'},
        'parameters': {}, 'assets': {'media': asset},
    }
    if kind == 'overlay':
        manifest['implementation']['blend'] = blend
        manifest['parameters']['opacity'] = {
            'type': 'number', 'label': '不透明度', 'unit': 'ratio',
            'default': 1.0, 'min': 0.0, 'max': 1.0, 'animatable': False}
    validate_manifest(manifest)
    if rate is not None and not 1 <= rate <= 240:
        raise ContractError('E_CAPABILITY', '叠加渲染器只支持 1–240 FPS 的素材')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.okk-effect-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'package'
        (staged / 'assets').mkdir(parents=True)
        shutil.copyfile(source, staged / asset['path'])
        (staged / 'effect.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                                          encoding='utf-8')
        load_package(staged)
        staged.rename(destination)
    return load_package(destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('capabilities')
    check = commands.add_parser('validate')
    check.add_argument('package', type=Path)
    compile_parser = commands.add_parser('compile')
    compile_parser.add_argument('package', type=Path)
    compile_parser.add_argument('instance', type=Path)
    pack = commands.add_parser('pack')
    pack.add_argument('source', type=Path)
    pack.add_argument('destination', type=Path)
    pack.add_argument('--id', dest='effect_id', required=True)
    pack.add_argument('--name', required=True)
    pack.add_argument('--kind', choices=('overlay', 'mask', 'lut'), required=True)
    pack.add_argument('--application', required=True)
    pack.add_argument('--source-format', required=True)
    pack.add_argument('--fps', help='已导出的素材帧率，例如 25 或 30000/1001')
    pack.add_argument('--blend', choices=('alpha', 'screen'), default='alpha')
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    try:
        if command == 'capabilities':
            result = capabilities()
        elif command == 'pack':
            package = create_asset_package(**args)
            result = {'package': str(package.root), 'id': package.manifest['id'],
                      'source': package.manifest['source']}
        else:
            package = load_package(args['package'])
            if command == 'validate':
                result = {'valid': True, 'id': package.manifest['id'],
                          'version': package.manifest['version']}
            else:
                compiled = compile_effect(package, read_json(args['instance']))
                result = asdict(compiled)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False))
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({'error': getattr(exc, 'code', 'E_IO'), 'message': str(exc)},
                         ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
