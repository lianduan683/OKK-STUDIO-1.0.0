from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest

from creative_core import ColorGrade
from gpu_lut_shader import attach_grade_luts, read_cube_texture, LutShaderUnsupported
from realtime_grade import shader_source


IDENTITY = ('LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n'
            '0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n')


class GpuLutShaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gpu_lut_')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "颜色' LUT.cube"
        self.path.write_text(IDENTITY, 'utf-8')

    def test_cube_axis_order_and_float_precision(self):
        cube = read_cube_texture(self.path)
        self.assertEqual(cube.size, 2)
        pixels = list(struct.iter_unpack('<4f', cube.rgba))
        self.assertEqual(pixels[1], (1., 0., 0., 1.))
        self.assertEqual(pixels[2], (0., 1., 0., 1.))
        self.assertEqual(pixels[4], (0., 0., 1., 1.))
        self.path.write_text('\ufeff# comment\nTITLE "example"\n' + IDENTITY, 'utf-8')
        self.assertEqual(read_cube_texture(self.path), cube)

    def test_disabled_or_absent_lut_never_accesses_missing_file(self):
        source = shader_source()
        grades = (None, ColorGrade(enabled=False, lut_path='missing.cube'), ColorGrade())
        self.assertEqual(attach_grade_luts(source, grades), source)

    def test_each_lut_follows_its_own_primary_and_precedes_next_grade(self):
        grades = (ColorGrade(lut_path=str(self.path)), ColorGrade(), ColorGrade(lut_path=str(self.path)))
        source = attach_grade_luts(shader_source(), grades)
        self.assertIn('//!BIND OKK_LUT_0\n//!BIND OKK_LUT_2\n', source)
        self.assertNotIn('OKK_LUT_1', source)
        for index in (0, 2):
            self.assertIn(f'g{index}_skin_deyellow);\n    return rg_lut{index}(c);', source)
        self.assertIn('rg_grade2(rg_grade1(rg_grade0(c.rgb)))', source)
        self.assertIn('//!FILTER NEAREST', source)
        self.assertIn('texelFetch(OKK_LUT_0,', source)

    def test_embedded_bytes_change_when_lut_file_is_replaced(self):
        grade = ColorGrade(lut_path=str(self.path))
        before = attach_grade_luts(shader_source(), (grade,))
        self.path.write_text(IDENTITY.rsplit('1 1 1\n', 1)[0] + '.5 .6 .7\n', 'utf-8')
        after = attach_grade_luts(shader_source(), (grade,))
        self.assertNotEqual(before, after)

    def test_unsupported_domains_shapers_and_unknown_headers_retain_cpu(self):
        cases = (
            IDENTITY.replace('DOMAIN_MIN 0 0 0', 'DOMAIN_MIN -1 -1 -1'),
            IDENTITY.replace('DOMAIN_MAX 1 1 1', 'DOMAIN_MAX 2 2 2'),
            IDENTITY.replace('DOMAIN_MAX 1 1 1', 'DOMAIN_MAX nan 1 1'),
            'LUT_1D_SIZE 2\n' + IDENTITY,
            'LUT_3D_INPUT_RANGE 0 1\n' + IDENTITY,
            IDENTITY + '0 0 0\n',
            IDENTITY + 'LUT_3D_SIZE 2\n',
            IDENTITY.replace('LUT_3D_SIZE 2', 'LUT_3D_SIZE 129'),
            IDENTITY.replace('LUT_3D_SIZE 2', 'LUT_3D_SIZE 1'),
            IDENTITY.replace('LUT_3D_SIZE 2', 'LUT_3D_SIZE 2.0'),
            IDENTITY.replace('DOMAIN_MIN 0 0 0', 'DOMAIN_MIN 0 0 0\nDOMAIN_MIN 0 0 0'),
        )
        for text in cases:
            with self.subTest(text=text[:80]):
                self.path.write_text(text, 'utf-8')
                with self.assertRaises(LutShaderUnsupported):
                    read_cube_texture(self.path)

    def test_nonfinite_truncated_missing_and_other_formats_retain_cpu(self):
        for last in ('nan 0 0', 'inf 0 0', '1e30 0 0', 'bad 0 0', ''):
            with self.subTest(last=last):
                self.path.write_text(IDENTITY.rsplit('1 1 1\n', 1)[0] + last, 'utf-8')
                with self.assertRaises(LutShaderUnsupported):
                    read_cube_texture(self.path)
        for path in (self.path.with_suffix('.3dl'), self.path.with_name('missing.cube')):
            with self.assertRaises(LutShaderUnsupported):
                read_cube_texture(path)

    def test_shader_contract_changes_fail_instead_of_omitting_lut(self):
        grade = ColorGrade(lut_path=str(self.path))
        for source in (shader_source().replace('//!BIND HOOKED\n', ''),
                       shader_source().replace('g0_skin_deyellow);', 'g0_skin_deyellow + 0.0);')):
            with self.assertRaises(LutShaderUnsupported):
                attach_grade_luts(source, (grade,))
        with self.assertRaises(LutShaderUnsupported):
            attach_grade_luts(shader_source(), (grade,) * 4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
