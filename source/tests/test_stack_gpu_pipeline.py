"""Safe opaque-stack GPU routing, with real frame-clock and fallback checks."""
from dataclasses import replace
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from clip_transform import ClipTransform, ClipTransformRange
from creative_core import ColorGrade
from exporter_core import (Capabilities, CREATE_NO_WINDOW, ExportSettings, MediaInfo,
                           detect_capabilities, probe_media)
from gpu_export import GpuExportCapabilities, GpuExportUnsupported, find_gpu_export_ffmpeg
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from program_edits import ClipGrade
from video_stack_render import (StackLayer, StackSegment, build_stack_command,
                                build_stack_gpu_command, plan_stack_segments)


class StackGpuPipelinePlanTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='stack-gpu-plan-')
        self.addCleanup(self.folder.cleanup)
        self.project = MulticamProject(fps=30., clips=[
            CameraClip('source', 1, 'original.mov', 0., 8., proxy_path='preview-proxy.mp4')],
            cuts=[Cut(0., 1)], video_stack_enabled=True)
        self.segment = plan_stack_segments(self.project)[0]
        self.info = MediaInfo('original.mov', width=7680, height=4320, fps=24.,
            video_codec='hevc', pixel_format='yuv420p10le', bit_depth=10,
            color_primaries='bt709', color_transfer='bt709', color_space='bt709', color_range='tv')
        self.settings = ExportSettings(codec='hevc_nvenc', decode_mode='cuda', gpu_index=2)
        self.caps = Capabilities(cuda_decode=True, scale_cuda=True, nvenc_codecs={'hevc_nvenc'})

    def build(self, *, segment=None, project=None, info=None, **kwargs):
        return build_stack_gpu_command(segment or self.segment, project or self.project,
            ['ffmpeg', '-y'], self.settings, self.caps, 3840, 2160, 'yuv420p10le', '30/1',
            ['-c:v', 'hevc_nvenc', '-preset', 'p1', '-bf', '2'], 'output.mp4',
            source_frame_rates={'original.mov': Fraction(24)},
            media_by_path={'original.mov': info or self.info},
            gpu_ffmpeg='gpu-ffmpeg', shader_dir=self.folder.name, **kwargs)

    def test_neutral_uses_original_and_keeps_cuda_surfaces_until_nvenc(self):
        command, backend, source, prefix = self.build()
        chain = command[command.index('-vf')+1]
        self.assertEqual((backend, source, prefix), ('cuda_pipeline', 'original.mov', 0))
        self.assertIn('scale_cuda=3840:2160:format=p010le', chain)
        self.assertNotIn('hwdownload', chain)
        self.assertNotIn('-pix_fmt', command)
        self.assertEqual(command[command.index('-hwaccel_device')+1], '2')
        self.assertIn('original.mov', command)
        self.assertNotIn('preview-proxy.mp4', command)
        self.assertEqual(command[command.index('-frames:v')+1], '240')
        self.assertEqual(command[command.index('-bf')+1], '2')

    def test_transparency_crop_placement_signal_and_unsupported_sources_retain_original_graph(self):
        layer = self.segment.layers[0]
        changes = [dict(segment=replace(self.segment, layers=(replace(layer, has_alpha=True),))),
                   dict(segment=replace(self.segment, layers=(replace(layer, placement=(.1, 0., 1.)),))),
                   dict(segment=replace(self.segment, layers=(layer, layer))),
                   dict(info=replace(self.info, has_alpha=True)),
                   dict(info=replace(self.info, color_range='pc')),
                   dict(info=replace(self.info, color_transfer='smpte2084')),
                   dict(info=replace(self.info, color_space=None)),
                   dict(info=replace(self.info, sample_aspect_ratio='4:3')),
                   dict(info=replace(self.info, has_display_transform=True)),
                   dict(info=replace(self.info, field_order='tt')),
                   dict(info=replace(self.info, width=4096)),
                   dict(info=replace(self.info, video_codec='prores', pixel_format='yuv422p10le'))]
        for changeset in changes:
            with self.subTest(changes=changeset):
                with self.assertRaises(GpuExportUnsupported): self.build(**changeset)
        project = replace(self.project, clip_transforms=[
            ClipTransformRange(0., 8., ClipTransform('fill', 1.2))])
        with self.assertRaises(GpuExportUnsupported): self.build(project=project)
        # Fill at unit zoom equals fit only because the source aspect already matches.
        project.clip_transforms[0].transform.zoom = 1.
        self.assertEqual(self.build(project=project)[1], 'cuda_pipeline')

    def test_treated_opaque_cache_cannot_hide_original_alpha(self):
        layer = replace(self.segment.layers[0], path='treated.mov')
        with self.assertRaises(GpuExportUnsupported):
            build_stack_gpu_command(replace(self.segment, layers=(layer,)), self.project,
                ['ffmpeg'], self.settings, self.caps, 3840, 2160, 'yuv420p10le', '30', [], 'out.mp4',
                media_by_path={'treated.mov': self.info,
                               'original.mov': replace(self.info, has_alpha=True)})

    def test_local_grade_order_matches_programme_outer_and_plain_stack_semantics(self):
        camera, local, global_grade = (ColorGrade(exposure=.2), ColorGrade(gamma=1.1),
                                       ColorGrade(saturation=.8))
        project = replace(self.project, camera_grades={1: camera},
            clip_grades=[ClipGrade(0., 8., local)], global_grade=global_grade)
        plan = SimpleNamespace(global_options=(), input_options=('-hwaccel', 'cuda'),
            filter_chain='gpu-filter', color_options=())
        cases = [(False, False, (camera, local, global_grade)),
                 (True, True, (camera, local, global_grade)),
                 (True, False, (camera, global_grade))]
        for outer, programme, expected in cases:
            with self.subTest(outer=outer, programme=programme):
                current = replace(project, multicam_outer_order=[0] if outer else [])
                # An outer-only camera is unaffected by the programme crop.
                if outer and not programme:
                    current.clip_transforms = [ClipTransformRange(0., 8., ClipTransform('fill', 2.))]
                segment = replace(self.segment, layers=(replace(self.segment.layers[0], programme=programme),))
                with patch('gpu_export.build_gpu_video_plan', return_value=plan) as planner:
                    command, backend, _, _ = self.build(project=current, segment=segment)
                self.assertEqual(tuple(planner.call_args.args[0]), expected)
                self.assertEqual(backend, 'gpu_stack_grade')
                self.assertEqual(command[command.index('-vf')+1], 'gpu-filter')

    def test_lut_is_rejected_instead_of_dropped(self):
        project = replace(self.project, camera_grades={1: ColorGrade(lut_path='look.cube')})
        caps = GpuExportCapabilities('gpu-ffmpeg', True, True, False, scale_cuda=True)
        with patch('gpu_export.detect_gpu_export', return_value=caps):
            with self.assertRaisesRegex(GpuExportUnsupported, 'LUT'):
                self.build(project=project)

    def test_frame_phase_and_seek_match_original_stack_for_nonmatching_cfr(self):
        segment = replace(self.segment, start=.7, duration=.6, frames=18,
            layers=(replace(self.segment.layers[0], source_start=4.7),))
        command, _, _, prefix = self.build(segment=segment)
        self.assertEqual(prefix, 1)  # 21 output frames modulo 30/24 period (5).
        self.assertEqual(command[command.index('-ss')+1], '4.666666667')
        self.assertIn('trim=start_frame=1:end_frame=19', command[command.index('-vf')+1])
        cpu = build_stack_command(segment, self.project, ['ffmpeg'], 3840, 2160,
            'yuv420p10le', '30/1', [], 'out.mp4', source_frame_rates={'original.mov': Fraction(24)})
        self.assertEqual(command[command.index('-ss')+1], cpu[cpu.index('-ss')+1])

    def test_subtitles_force_download_after_gpu_grade_and_preserve_timeline_time(self):
        segment = replace(self.segment, start=1., duration=1., frames=30)
        plan = SimpleNamespace(global_options=(), input_options=('-hwaccel', 'cuda'),
            filter_chain='libplacebo=fixture,hwdownload,format=p010le', color_options=())
        with patch('gpu_export.build_gpu_video_plan', return_value=plan):
            command, backend, _, _ = self.build(segment=segment, ass_path='captions.ass')
        chain = command[command.index('-vf')+1]
        self.assertEqual(backend, 'gpu_stack_grade')
        self.assertGreater(chain.index('subtitles='), chain.index('hwdownload'))
        self.assertIn('setpts=PTS+1/TB', chain)
        self.assertTrue(chain.endswith('setpts=PTS-STARTPTS'))


@unittest.skipUnless(os.environ.get('OKK_TEST_NVENC') == '1', 'enable actual NVIDIA pipeline verification')
class StackGpuPipelineMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Path(__file__).resolve().parents[1]
        cls.ffmpeg, cls.ffprobe = str(cls.app/'bin/ffmpeg.exe'), str(cls.app/'bin/ffprobe.exe')
        cls.caps = detect_capabilities(cls.app)
        cls.gpu_ffmpeg = find_gpu_export_ffmpeg(cls.app)
        if not cls.caps.cuda_decode or 'hevc_nvenc' not in cls.caps.nvenc_codecs or not cls.gpu_ffmpeg:
            raise unittest.SkipTest('NVDEC, NVENC and GPU grade runtime required')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='stack-gpu-media-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder/'original.mp4'
        self.lossless = ['-c:v', 'hevc_nvenc', '-preset', 'p1', '-tune', 'lossless',
                         '-rc', 'constqp', '-qp', '0', '-bf', '0', '-fps_mode', 'cfr']
        self.run_command([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc2=s=320x180:r=24:d=3', '-vf',
            'format=yuv420p10le,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=limited',
            '-an', *self.lossless,
            '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
            '-color_range', 'tv', str(self.source)])
        self.project = MulticamProject(fps=30., clips=[CameraClip('source', 1, str(self.source), 0., 3.)],
            cuts=[Cut(0., 1)], video_stack_enabled=True, range_end=2.,
            clip_grades=[ClipGrade(1., 2., ColorGrade(exposure=.2))])
        self.settings = ExportSettings(codec='hevc_nvenc', preset='p1', quality=0, bframes=0,
            bit_depth='10-bit', audio='none', decode_mode='cuda', parallel_workers=1,
            auto_parallel=False, extra_args=['-tune', 'lossless', '-rc', 'constqp', '-qp', '0'])
        self.commands = []

    def run_command(self, command):
        result = subprocess.run(command, capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace')[-2500:])
        return result.stdout

    def runner(self, command, report):
        self.commands.append(command)
        result = subprocess.run(command, capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        return result.returncode, result.stderr.decode('utf-8', 'replace')

    def render(self, name, *, project=None, settings=None, runner=None):
        output = self.folder/(name+'.mp4')
        render_multicam(project or self.project, output, self.ffmpeg, self.ffprobe,
            settings or self.settings, self.caps, runner or self.runner, work_directory=self.folder)
        return output

    def pixels(self, path):
        return self.run_command([self.ffmpeg, '-v', 'error', '-i', str(path),
                                 '-pix_fmt', 'yuv420p10le', '-f', 'rawvideo', 'pipe:1'])

    def test_real_render_selects_neutral_and_graded_gpu_chunks_and_keeps_timing(self):
        output = self.render('gpu-mixed')
        commands = [command for command in self.commands if '-vf' in command]
        self.assertEqual(len(commands), 2)
        chains = [command[command.index('-vf')+1] for command in commands]
        self.assertNotIn('hwdownload', chains[0])
        self.assertIn('libplacebo=', chains[1])
        self.assertNotIn('-/filter_complex', commands[0])
        probe = json.loads(self.run_command([self.ffprobe, '-v', 'error', '-select_streams', 'v:0',
            '-count_frames', '-show_streams', '-of', 'json', str(output)]))['streams'][0]
        self.assertEqual(probe['nb_read_frames'], '60')
        self.assertEqual(probe['avg_frame_rate'], '30/1')
        self.assertEqual(probe['pix_fmt'], 'yuv420p10le')
        self.assertEqual((probe['width'], probe['height']), (320, 180))
        self.assertAlmostEqual(float(probe['duration']), 2., places=5)
        self.assertEqual((probe['color_primaries'], probe['color_transfer'], probe['color_space'],
                          probe['color_range']), ('bt709', 'bt709', 'bt709', 'tv'))

    def test_gpu_failure_retries_full_cpu_graph_with_identical_fallback_pixels(self):
        project = replace(self.project, clip_grades=[ClipGrade(0., 2., ColorGrade(exposure=.2))])
        expected = self.pixels(self.render('cpu-reference', project=project,
                               settings=replace(self.settings, decode_mode='cpu')))
        attempts = []
        def fail_fast_path(command, report):
            attempts.append(command)
            if '-vf' in command and 'libplacebo=' in command[command.index('-vf')+1]:
                return 1, 'synthetic GPU grade failure'
            return self.runner(command, report)
        actual = self.pixels(self.render('gpu-fallback', project=project, runner=fail_fast_path))
        self.assertEqual(actual, expected)
        self.assertEqual(sum('-vf' in command for command in attempts), 1)
        self.assertTrue(any('-/filter_complex' in command for command in attempts))

    def test_gpu_and_cpu_framing_chunks_join_with_consistent_per_frame_signal(self):
        project = replace(self.project, clip_transforms=[
            ClipTransformRange(1., 2., ClipTransform('fill', 1.2))])
        chunk_signals = []
        def inspect_chunks(command, report):
            code, details = self.runner(command, report)
            if code == 0 and '-frames:v' in command:
                frames = json.loads(self.run_command([self.ffprobe, '-v', 'error',
                    '-select_streams', 'v:0', '-show_frames', '-show_entries',
                    'frame=best_effort_timestamp_time,color_primaries,color_transfer,color_space,color_range',
                    '-of', 'json', str(command[-1])]))['frames']
                chunk_signals.append(frames)
            return code, details
        output = self.render('gpu-cpu-framing', project=project, runner=inspect_chunks)
        picture_commands = [command for command in self.commands if '-frames:v' in command]
        self.assertEqual(len(picture_commands), 2)
        self.assertIn('-vf', picture_commands[0])
        self.assertIn('-/filter_complex', picture_commands[1])
        result = json.loads(self.run_command([self.ffprobe, '-v', 'error', '-select_streams', 'v:0',
            '-count_frames', '-show_frames', '-show_streams', '-of', 'json', str(output)]))
        stream, frames = result['streams'][0], result['frames']
        self.assertEqual(stream['nb_read_frames'], '60')
        self.assertEqual(stream['avg_frame_rate'], '30/1')
        self.assertAlmostEqual(float(stream['duration']), 2., places=5)
        self.assertEqual(len(frames), 60)
        for index, frame in enumerate(frames):
            self.assertAlmostEqual(float(frame['best_effort_timestamp_time']), index/30, places=5)
        # Check individual encoded chunks as well as concat's decoded frames;
        # a container tag inherited from the first chunk must not mask a change.
        for section, pictures in enumerate([*chunk_signals, frames]):
            for index, frame in enumerate(pictures):
                self.assertEqual(tuple(frame.get(key) for key in
                    ('color_primaries', 'color_transfer', 'color_space', 'color_range')),
                    ('bt709', 'bt709', 'bt709', 'tv'), (section, index))

    def test_nonmatching_source_clock_partition_matches_continuous_gpu_frames(self):
        project = replace(self.project, range_end=3., clip_grades=[])
        segment = plan_stack_segments(project)[0]
        info = probe_media(self.ffprobe, str(self.source))
        def make(job, name):
            output = self.folder/(name+'.mp4')
            command, _, _, _ = build_stack_gpu_command(job, project,
                [self.ffmpeg, '-v', 'error', '-y'], self.settings, self.caps, 320, 180,
                'yuv420p10le', '30/1', self.lossless, output,
                source_frame_rates={str(self.source): Fraction(24)},
                media_by_path={str(self.source): info}, gpu_ffmpeg=self.gpu_ffmpeg,
                shader_dir=self.folder/'shaders')
            self.run_command(command)
            return self.pixels(output)
        full = make(segment, 'full')
        # 21 is not a multiple of the 30/24 conversion period (5).
        partial = replace(segment, start=.7, duration=.6, frames=18,
                          layers=(replace(segment.layers[0], source_start=.7),))
        part = make(partial, 'part')
        frame_bytes = 320*180*3
        self.assertEqual(part, full[21*frame_bytes:39*frame_bytes])


if __name__ == '__main__': unittest.main(verbosity=2)
