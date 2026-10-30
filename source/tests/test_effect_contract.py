"""Contract boundaries and real renders using the existing FFmpeg adapters."""
import copy
from dataclasses import asdict
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from creative_core import ColorGrade, grade_filter
from effect_contract import (ContractError, compile_effect, load_package,
                             read_json, validate_manifest, capabilities)
from effect_package_cli import create_asset_package
from effects_core import build_effect_filter_graph, build_effect_filters
from transitions_core import (TransitionClip, build_transition_video_graph,
                              validate_transitions)

SOURCE = Path(__file__).resolve().parents[1]
EXAMPLES = SOURCE / 'effect_contract_examples'


def fixture(name='blur'):
    return load_package(EXAMPLES / name), read_json(EXAMPLES / (name + '-instance.json'))


def instance_for(package):
    _, value = fixture('dissolve' if package.manifest['kind'] == 'transition' else 'blur')
    value['effect_id'] = package.manifest['id']
    value['effect_version'] = package.manifest['version']
    value['parameters'] = {}
    return value


def numeric(default, low=0, high=1, unit='ratio'):
    return {'type': 'number', 'label': '参数', 'unit': unit, 'default': default,
            'min': low, 'max': high, 'animatable': False}


class ContractTests(unittest.TestCase):
    def assert_code(self, code, callback):
        with self.assertRaises(ContractError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_examples_compile_to_existing_models_and_keep_rational_clock(self):
        package, instance = fixture()
        before = copy.deepcopy(instance)
        compiled = compile_effect(package, instance)
        self.assertEqual(compiled.native.blur, 45)
        self.assertAlmostEqual(compiled.duration_seconds, 3.003)
        self.assertEqual(build_effect_filters(compiled.native), ['gblur=sigma=2.2500'])
        self.assertEqual(instance, before)
        self.assertEqual(compiled.fingerprint, compile_effect(package, instance).fingerprint)
        instance['parameters']['blur'] = 46
        self.assertNotEqual(compiled.fingerprint, compile_effect(package, instance).fingerprint)

    def test_transition_preserves_centred_program_clock(self):
        package, instance = fixture('dissolve')
        result = compile_effect(package, instance)
        transition = result.program_transition()
        self.assertEqual(asdict(transition), {'time': 4, 'kind': 'fade', 'duration': .8, 'mask_path': ''})
        self.assertEqual(package.manifest['name'], '标准叠化')

    def test_unknown_versions_ids_fields_and_overrides_fail(self):
        package, instance = fixture()
        for key, value, code in [('schema', 'okk.effect-instance/2.0', 'E_VERSION'),
                                  ('effect_version', '1.0.1', 'E_VERSION'),
                                  ('effect_id', 'another.id', 'E_VERSION'),
                                  ('enabled', 1, 'E_SCHEMA'),
                                  ('unused', True, 'E_SCHEMA')]:
            changed = copy.deepcopy(instance)
            changed[key] = value
            with self.subTest(key=key):
                self.assert_code(code, lambda: compile_effect(package, changed))
        instance['parameters']['typo'] = 5
        self.assert_code('E_SCHEMA', lambda: compile_effect(package, instance))

    def test_invalid_numbers_are_not_coerced_or_clamped(self):
        package, instance = fixture()
        for value in (-1, 101, float('nan'), float('inf'), True, '30', 10**1000):
            instance['parameters']['blur'] = value
            with self.subTest(value=str(value)[:30]):
                self.assert_code('E_PARAMETER', lambda: compile_effect(package, instance))

    def test_animation_is_rejected_instead_of_discarded(self):
        package, instance = fixture()
        instance['keyframes'] = {'blur': [{'frame': 0, 'value': 0}, {'frame': 20, 'value': 100}]}
        self.assert_code('E_UNSUPPORTED_ANIMATION', lambda: compile_effect(package, instance))
        package.manifest['parameters']['blur']['animatable'] = True
        self.assert_code('E_UNSUPPORTED_ANIMATION', lambda: validate_manifest(package.manifest))

    def test_parameter_binding_rejects_wrong_units_types_and_declared_ranges(self):
        package, _ = fixture()
        for key, value in [('unit', 'pixels'), ('min', -1), ('max', 101), ('default', True)]:
            manifest = copy.deepcopy(package.manifest)
            manifest['parameters']['blur'][key] = value
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate_manifest(manifest)
        manifest = copy.deepcopy(package.manifest)
        manifest['parameters']['saturation'] = numeric(1, 0, 3)
        self.assert_code('E_PARAMETER_BINDING', lambda: validate_manifest(manifest))

    def test_boolean_rotation_and_fade_bindings_use_native_constraints(self):
        package, instance = fixture()
        package.manifest['parameters'] = {
            'hflip': {'type': 'boolean', 'label': '水平翻转', 'unit': 'boolean',
                      'default': True, 'animatable': False},
            'rotation': {'type': 'enum', 'label': '旋转', 'unit': 'degrees',
                         'default': 90, 'choices': [0, 90, 180, 270], 'animatable': False},
            'fade_out': numeric(1, 0, 10, 'seconds')}
        instance['parameters'] = {}
        native = compile_effect(package, instance).native
        self.assertTrue(native.hflip)
        self.assertEqual(native.rotation, 90)
        instance['parameters']['rotation'] = 45
        self.assert_code('E_PARAMETER', lambda: compile_effect(package, instance))
        instance['parameters'] = {'fade_out': 4}
        self.assert_code('E_NATIVE_VALIDATION', lambda: compile_effect(package, instance))

    def test_fps_frame_counts_and_transition_window_validation(self):
        package, instance = fixture('dissolve')
        for fps in ({'num': True, 'den': 1}, {'num': 25, 'den': 0}, {'num': 1001, 'den': 1}):
            instance['fps'] = fps
            with self.subTest(fps=fps), self.assertRaises(ContractError):
                compile_effect(package, instance)
        instance['fps'] = {'num': 25, 'den': 1}
        for frames in (0, 1.5, True, 200):
            instance['timing']['duration_frames'] = frames
            with self.subTest(frames=frames), self.assertRaises(ContractError):
                compile_effect(package, instance)
        instance['timing'] = {'cut_frame': 0, 'duration_frames': 10}
        self.assert_code('E_TIMING', lambda: compile_effect(package, instance))

    def test_disabled_instance_bypasses_native_output(self):
        package, instance = fixture()
        instance['enabled'] = False
        self.assertIsNone(compile_effect(package, instance).native)

    def test_grade_preserves_independent_fields_and_fingerprint_tracks_base(self):
        package, instance = fixture()
        package.manifest['implementation'] = {'engine': 'okk.grade.v1'}
        package.manifest['parameters'] = {'saturation': numeric(.5, 0, 3)}
        instance['parameters'] = {}
        base = ColorGrade(exposure=1, curve_b=((0., 0.), (.5, .6), (1., 1.)))
        original = copy.deepcopy(base)
        result = compile_effect(package, instance, base_grade=base)
        self.assertEqual(result.native.saturation, .5)
        self.assertEqual(result.native.exposure, 1)
        self.assertEqual(result.native.curve_b, base.curve_b)
        self.assertTrue(grade_filter(result.native))
        self.assertEqual(base, original)
        base.exposure = 2
        self.assertNotEqual(result.fingerprint, compile_effect(package, instance, base_grade=base).fingerprint)

    def test_backend_and_color_capabilities_are_explicit(self):
        package, _ = fixture()
        for field, value in [('color_space', 'acescg'), ('temporal_radius', 1), ('inputs', True)]:
            manifest = copy.deepcopy(package.manifest)
            manifest['render'][field] = value
            self.assert_code('E_CAPABILITY', lambda: validate_manifest(manifest))
        package.manifest['implementation']['engine'] = 'ofx.anything'
        self.assert_code('E_BACKEND', lambda: validate_manifest(package.manifest))
        self.assertEqual(capabilities()['raw_source_importers'], [])

    def test_malformed_transition_mode_reports_contract_error(self):
        package, _ = fixture('dissolve')
        for value in (None, True, 0, [], {}):
            package.manifest['implementation']['mode'] = value
            with self.subTest(value=value):
                self.assert_code('E_SCHEMA', lambda: validate_manifest(package.manifest))

    def test_transition_fps_cannot_be_silently_clamped_by_native_renderer(self):
        package, instance = fixture('dissolve')
        instance['fps'] = {'num': 240, 'den': 1}
        self.assert_code('E_CAPABILITY', lambda: compile_effect(package, instance))

    def test_program_adapter_rejects_modes_not_supported_by_program(self):
        package, instance = fixture('dissolve')
        package.manifest['implementation']['mode'] = 'hblur'
        result = compile_effect(package, instance)
        self.assertEqual(result.native.xfade_mode, 'hblur')
        self.assert_code('E_HOST_CAPABILITY', result.program_transition)

    def test_json_rejects_duplicate_keys_nonfinite_and_oversize(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'data.json'
            for content in ('{"schema":1,"schema":2}', '{"value":NaN}', ' ' * (1024 * 1024 + 1)):
                path.write_text(content, encoding='utf-8')
                self.assert_code('E_SCHEMA', lambda: read_json(path))

    def test_checked_in_schema_is_valid_json_and_defines_both_documents(self):
        schema = read_json(SOURCE / 'effect_contract_schema.json')
        self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        self.assertEqual(len(schema['oneOf']), 2)
        self.assertIn('package', schema['$defs'])
        self.assertIn('instance', schema['$defs'])


class PackTests(unittest.TestCase):
    assert_code = ContractTests.assert_code

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def pack(self, kind='overlay', fps='30000/1001', blend='alpha'):
        source = self.root / {'overlay': 'source.mov', 'mask': 'mask.png', 'lut': 'look.cube'}[kind]
        source.write_bytes(b'test fixture; decoding is a separate renderer check')
        return create_asset_package(source, self.root / ('package-' + kind),
            effect_id='test.effects.asset', name='测试资源', kind=kind,
            application='davinci-resolve', source_format='exported-asset',
            fps=fps if kind == 'overlay' else None, blend=blend)

    def test_overlay_alpha_and_screen_map_normalized_opacity_to_legacy_percent(self):
        package = self.pack(blend='screen')
        instance = instance_for(package)
        instance['parameters']['opacity'] = .42
        result = compile_effect(package, instance)
        self.assertEqual(result.native.overlay_opacity, 42)
        self.assertAlmostEqual(result.native.overlay_fps, 30000 / 1001)
        self.assertEqual(result.native.overlay_kind, 'screen')
        self.assertIn('baked', result.warnings[0])
        self.assertIn('screen', build_effect_filter_graph(result.native))

    def test_asset_hash_is_rechecked_at_compile_time(self):
        package = self.pack()
        (package.root / package.manifest['assets']['media']['path']).write_bytes(b'changed')
        self.assert_code('E_ASSET_HASH', lambda: compile_effect(package, instance_for(package)))

    def test_missing_asset_and_path_traversal_are_rejected(self):
        package = self.pack()
        asset = package.manifest['assets']['media']
        for value in ('../source.mov', 'C:/source.mov', '/source.mov', 'assets\\media.mov',
                      'assets/../source.mov', 'assets//media.mov'):
            asset['path'] = value
            with self.subTest(path=value):
                self.assert_code('E_ASSET_PATH', lambda: compile_effect(package, instance_for(package)))
        asset['path'] = 'assets/missing.mov'
        self.assert_code('E_ASSET_MISSING', lambda: compile_effect(package, instance_for(package)))

    def test_mask_and_lut_compile_using_package_resources(self):
        mask = self.pack('mask')
        result = compile_effect(mask, instance_for(mask))
        self.assertTrue(Path(result.program_transition().mask_path).is_file())
        self.assertEqual(result.native.kind, 'mask')
        clips = [TransitionClip('a', 2), TransitionClip('b', 2)]
        validate_transitions(clips, [result.native], mask_dir=result.asset_dir)
        lut = self.pack('lut')
        self.assertTrue(Path(compile_effect(lut, instance_for(lut)).native.lut_path).is_file())

    def test_package_can_be_relocated_without_embedded_absolute_paths(self):
        package = self.pack()
        moved = self.root / 'moved'
        shutil.move(str(package.root), moved)
        package = load_package(moved)
        native = compile_effect(package, instance_for(package)).native
        self.assertTrue(Path(native.overlay_path).is_relative_to(moved))

    def test_pack_never_overwrites_and_does_not_publish_invalid_input(self):
        package = self.pack()
        source = self.root / 'source.mov'
        kwargs = dict(effect_id='test.effects.new', name='name', kind='overlay',
                      application='premiere-pro', source_format='render', fps='25')
        self.assert_code('E_DESTINATION', lambda: create_asset_package(source, package.root, **kwargs))
        kwargs['fps'] = '500'
        target = self.root / 'invalid'
        self.assert_code('E_CAPABILITY', lambda: create_asset_package(source, target, **kwargs))
        self.assertFalse(target.exists())


class NativeRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = SOURCE / 'bin' / 'ffmpeg.exe'
        if not cls.ffmpeg.is_file():
            raise unittest.SkipTest('FFmpeg not available')

    def run_ffmpeg(self, arguments):
        result = subprocess.run([str(self.ffmpeg), '-hide_banner', '-loglevel', 'error',
                                 '-filter_complex_threads', '1', *arguments],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')[-2000:])
        return result.stdout

    def test_real_blur_render_changes_pixels_and_remains_deterministic(self):
        package, instance = fixture()
        compiled = compile_effect(package, instance)
        prefix = ['-f', 'lavfi', '-i', 'testsrc2=s=32x32:r=25:d=0.12']
        output = ['-frames:v', '3', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1']
        plain = self.run_ffmpeg(prefix + output)
        args = prefix + ['-vf', ','.join(build_effect_filters(compiled.native))] + output
        effected = self.run_ffmpeg(args)
        self.assertEqual(len(plain), 3 * 32 * 32)
        self.assertEqual(len(effected), len(plain))
        self.assertNotEqual(plain, effected)
        self.assertEqual(effected, self.run_ffmpeg(args))

    def test_real_xfade_and_mask_start_on_a_and_finish_on_b(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mask = root / 'gradient.png'
            self.run_ffmpeg(['-f', 'lavfi', '-i',
                'nullsrc=s=16x16,geq=lum=X/W*255:cb=128:cr=128',
                '-frames:v', '1', '-threads', '1', str(mask)])
            mask_package = create_asset_package(mask, root / 'mask-package',
                effect_id='test.transition.mask', name='灰度擦除', kind='mask',
                application='test', source_format='png')
            for package in (fixture('dissolve')[0], mask_package):
                instance = instance_for(package)
                instance['timing'] = {'cut_frame': 25, 'duration_frames': 10}
                compiled = compile_effect(package, instance)
                clips = [TransitionClip('a', 1, 16, 16, 25), TransitionClip('b', 1, 16, 16, 25)]
                validate_transitions(clips, [compiled.native], mask_dir=compiled.asset_dir)
                graph = build_transition_video_graph(clips, [compiled.native], mask_dir=compiled.asset_dir)
                pixels = self.run_ffmpeg([
                    '-f', 'lavfi', '-i', 'color=red:s=16x16:r=25:d=1',
                    '-f', 'lavfi', '-i', 'color=blue:s=16x16:r=25:d=1',
                    '-filter_complex', graph, '-map', '[vout]',
                    '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
                with self.subTest(engine=compiled.engine):
                    self.assertGreaterEqual(len(pixels), 39 * 16 * 16 * 3)
                    self.assertGreater(pixels[0], 200)
                    self.assertLess(pixels[2], 20)
                    self.assertLess(pixels[-3], 20)
                    self.assertGreater(pixels[-1], 200)

    def test_real_alpha_overlay_and_identity_lut_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / 'alpha.mov'
            self.run_ffmpeg(['-f', 'lavfi', '-i',
                'color=blue@0.5:s=16x16:r=25:d=0.2,format=argb',
                '-c:v', 'qtrle', str(video)])
            package = create_asset_package(video, root / 'overlay-package',
                effect_id='test.effects.alpha', name='透明叠加', kind='overlay',
                application='test', source_format='exported-video', fps='25')
            compiled = compile_effect(package, instance_for(package))
            graph = build_effect_filter_graph(compiled.native, in_label='[0:v]', out_label='[out]')
            pixels = self.run_ffmpeg(['-f', 'lavfi', '-i', 'color=red:s=16x16:r=25:d=0.2',
                '-filter_complex', graph, '-map', '[out]', '-frames:v', '5',
                '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
            self.assertEqual(len(pixels), 5 * 16 * 16 * 3)
            self.assertGreater(pixels[0], 70)
            self.assertLess(pixels[0], 190)
            self.assertGreater(pixels[2], 70)
            self.assertLess(pixels[2], 190)

            lut = root / 'identity.cube'
            lut.write_text('LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n'
                           '0 0 1\n1 0 1\n0 1 1\n1 1 1\n', encoding='ascii')
            package = create_asset_package(lut, root / 'lut-package',
                effect_id='test.effects.lut', name='恒等 LUT', kind='lut',
                application='test', source_format='cube')
            compiled = compile_effect(package, instance_for(package))
            prefix = ['-f', 'lavfi', '-i', 'color=red:s=16x16:r=25:d=0.2']
            output = ['-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1']
            rendered = self.run_ffmpeg(prefix + ['-vf', grade_filter(compiled.native)] + output)
            self.assertGreater(rendered[0], 200)
            self.assertLess(rendered[1], 20)
            self.assertLess(rendered[2], 20)


if __name__ == '__main__':
    unittest.main()
