"""Embed verified 3D .cube LUTs into the export primary-grading hook.

Each LUT is sampled tetrahedrally after its own grade, matching grade_filter.
Only ordinary unit-domain 3D cubes are accepted. Other LUT formats/shapers
remain on the existing CPU path instead of silently changing their meaning.
Texture blocks follow https://libplacebo.org/custom-shaders/#textures.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import struct


class LutShaderUnsupported(ValueError):
    """The original CPU LUT route must be retained."""


MAX_CUBE_SIZE = 65
MAX_CUBE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CubeTexture:
    size: int
    rgba: bytes


def read_cube_texture(path) -> CubeTexture:
    path = Path(path)
    if path.suffix.lower() != '.cube':
        raise LutShaderUnsupported('GPU LUT 仅支持经过验证的 3D .cube；此 LUT 保留 CPU 路径。')
    try:
        if path.stat().st_size > MAX_CUBE_BYTES:
            raise LutShaderUnsupported('LUT 文件过大，保留 CPU 路径。')
        with path.open('rb') as stream:
            raw = stream.read(MAX_CUBE_BYTES + 1)
        if len(raw) > MAX_CUBE_BYTES:
            raise LutShaderUnsupported('LUT 文件过大，保留 CPU 路径。')
        text = raw.decode('utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise LutShaderUnsupported('无法读取 GPU LUT：' + str(path)) from exc
    size = None
    domains = {}
    samples = bytearray()
    count = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        fields = line.split('#', 1)[0].strip().split()
        if not fields:
            continue
        tag = fields[0]
        if tag == 'TITLE':
            continue
        if tag == 'LUT_3D_SIZE':
            if size is not None or count or len(fields) != 2:
                raise LutShaderUnsupported('LUT 网格声明无效。')
            try:
                size = int(fields[1])
            except ValueError as exc:
                raise LutShaderUnsupported('LUT 网格声明无效。') from exc
            if not 2 <= size <= MAX_CUBE_SIZE:
                raise LutShaderUnsupported(f'GPU LUT 支持 2–{MAX_CUBE_SIZE} 阶网格；此 LUT 保留 CPU 路径。')
            continue
        if tag in ('DOMAIN_MIN', 'DOMAIN_MAX'):
            if count or tag in domains or len(fields) != 4:
                raise LutShaderUnsupported('LUT 输入范围声明无效。')
            try:
                values = tuple(float(x) for x in fields[1:])
            except ValueError as exc:
                raise LutShaderUnsupported('LUT 输入范围声明无效。') from exc
            expected = (0., 0., 0.) if tag == 'DOMAIN_MIN' else (1., 1., 1.)
            if values != expected:
                raise LutShaderUnsupported('非标准输入范围 LUT 尚未验证 GPU 等价性，保留 CPU 路径。')
            domains[tag] = values
            continue
        if size is None or len(fields) != 3 or count >= size ** 3:
            raise LutShaderUnsupported(f'LUT 第 {line_number} 行不是受支持的 3D 网格数据。')
        try:
            values = tuple(float(x) for x in fields)
            if not all(math.isfinite(x) and abs(x) <= 65504 for x in values):
                raise ValueError('non-finite or extreme LUT sample')
            samples.extend(struct.pack('<4f', *values, 1.))
        except (ValueError, OverflowError, struct.error) as exc:
            raise LutShaderUnsupported(f'LUT 第 {line_number} 行数值无效。') from exc
        count += 1
    if size is None or count != size ** 3:
        raise LutShaderUnsupported('LUT 网格数据不完整。')
    # .cube stores R fastest, then G, then B; this is a 3D texture's XYZ order.
    return CubeTexture(size, bytes(samples))


def _tetrahedral_function(index, size):
    name = f'OKK_LUT_{index}'
    # The six tetrahedra are selected by sorting the fractional RGB offsets.
    # Integer texel fetches deliberately avoid hardware trilinear filtering.
    return f'''
vec3 rg_lut{index}_fetch(ivec3 p) {{
    return texelFetch({name}, clamp(p, ivec3(0), ivec3({size - 1})), 0).rgb;
}}
vec3 rg_lut{index}(vec3 c) {{
    vec3 p = clamp(c, 0.0, 1.0) * {float(size - 1):.1f};
    ivec3 lo = ivec3(floor(p));
    vec3 f = p - vec3(lo);
    ivec3 first, second;
    float a, b, d;
    if (f.r >= f.g) {{
        if (f.g >= f.b) {{ first=ivec3(1,0,0); second=ivec3(1,1,0); a=f.r; b=f.g; d=f.b; }}
        else if (f.r >= f.b) {{ first=ivec3(1,0,0); second=ivec3(1,0,1); a=f.r; b=f.b; d=f.g; }}
        else {{ first=ivec3(0,0,1); second=ivec3(1,0,1); a=f.b; b=f.r; d=f.g; }}
    }} else {{
        if (f.r >= f.b) {{ first=ivec3(0,1,0); second=ivec3(1,1,0); a=f.g; b=f.r; d=f.b; }}
        else if (f.g >= f.b) {{ first=ivec3(0,1,0); second=ivec3(0,1,1); a=f.g; b=f.b; d=f.r; }}
        else {{ first=ivec3(0,0,1); second=ivec3(0,1,1); a=f.b; b=f.g; d=f.r; }}
    }}
    return clamp((1.0-a)*rg_lut{index}_fetch(lo) + (a-b)*rg_lut{index}_fetch(lo+first)
                 + (b-d)*rg_lut{index}_fetch(lo+second) + d*rg_lut{index}_fetch(lo+ivec3(1)), 0.0, 1.0);
}}
'''


def attach_grade_luts(source: str, grades) -> str:
    """Return a hook containing each enabled LUT at the end of its own grade.

    This is export-only; it does not alter the live preview's dynamic uniforms.
    LUT bytes are embedded, so the caller's content hash also invalidates a
    shader when the file changes at the same path.
    """
    grades = tuple(grades)
    if len(grades) > 3:
        raise LutShaderUnsupported('GPU 调色最多支持机位、片段和全局三层。')
    textures, functions, bindings = [], [], []
    for index, grade in enumerate(grades):
        if grade is None or not grade.enabled or not grade.lut_path:
            continue
        cube = read_cube_texture(grade.lut_path)
        name = f'OKK_LUT_{index}'
        textures.append(f'//!TEXTURE {name}\n//!SIZE {cube.size} {cube.size} {cube.size}\n'
                        '//!FORMAT rgba32f\n//!FILTER NEAREST\n//!BORDER CLAMP\n'
                        + cube.rgba.hex() + '\n')
        bindings.append(f'//!BIND {name}\n')
        functions.append(_tetrahedral_function(index, cube.size))
        marker = (f'    c = rg_portrait(c,g{index}_skin_lighten,g{index}_skin_saturation,'
                  f'g{index}_skin_deyellow);\n    return c;')
        if source.count(marker) != 1:
            raise LutShaderUnsupported('GPU LUT 调色顺序与 shader 版本不匹配。')
        source = source.replace(marker, marker.replace('    return c;', f'    return rg_lut{index}(c);'))
    if not textures:
        return source
    marker = '//!BIND HOOKED\n'
    if source.count(marker) != 1 or source.count('vec3 rg_grade0(') != 1:
        raise LutShaderUnsupported('GPU LUT shader 绑定版本不匹配。')
    source = source.replace(marker, marker + ''.join(bindings))
    source = source.replace('vec3 rg_grade0(', '\n'.join(functions) + '\nvec3 rg_grade0(')
    return '\n'.join(textures) + '\n' + source
