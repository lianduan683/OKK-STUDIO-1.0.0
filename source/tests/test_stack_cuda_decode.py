"""Hardware decode must reach the existing composition graph without losing alpha or effects."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from clip_transform import ClipTransform, ClipTransformRange
from creative_core import ColorGrade
from exporter_core import Capabilities, CREATE_NO_WINDOW, ExportSettings, MediaInfo, detect_capabilities
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from program_edits import ClipGrade
from video_stack_render import build_stack_command, plan_stack_segments


class StackCudaPlanTests(unittest.TestCase):
    def make(self, alpha=False, codec='hevc', pixel='yuv420p10le'):
        project = MulticamProject(fps=30., clips=[CameraClip('a', 1, 'a.mov', 0., 2., has_alpha=alpha)],
                                  video_stack_enabled=True)
        segment = plan_stack_segments(project)[0]
        media = {'a.mov': MediaInfo('a.mov', width=7680, height=4320, fps=30.,
                    bit_depth=10, video_codec=codec, pixel_format=pixel, has_alpha=alpha)}
        return project, segment, media

    def command(self, project, segment, media):
        return build_stack_command(segment, project, ['ffmpeg'], 3840, 2160, 'yuv420p10le',
            '30/1', ['-c:v', 'hevc_nvenc'], 'out.mp4', media_by_path=media,
            decode_mode='cuda', gpu_index=1, decode_threads=6, filter_threads=8)

    def test_hevc_uses_selected_gpu_and_explicit_download_into_the_unchanged_graph(self):
        project, segment, media = self.make()
        command = self.command(project, segment, media)
        graph = command[command.index('-filter_complex')+1]
        self.assertEqual(command[command.index('-hwaccel')+1], 'cuda')
        self.assertEqual(command[command.index('-hwaccel_device')+1], '1')
        self.assertEqual(command[command.index('-hwaccel_output_format')+1], 'cuda')
        self.assertEqual(command[command.index('-filter_complex_threads')+1], '8')
        self.assertEqual(command[command.index('-threads')+1], '6')
        self.assertIn('[0:v:0]hwdownload,format=p010le,setpts=', graph)
        reference = build_stack_command(segment, project, ['ffmpeg'], 3840, 2160,
            'yuv420p10le', '30/1', [], 'out.mp4', media_by_path=media)
        self.assertEqual(graph.replace('hwdownload,format=p010le,', ''),
                         reference[reference.index('-filter_complex')+1])

    def test_transparent_and_unsupported_inputs_keep_their_software_decoder(self):
        for options in (dict(alpha=True), dict(codec='prores', pixel='yuv422p10le'),
                        dict(codec='vp9', pixel='yuva420p', alpha=True)):
            with self.subTest(options=options):
                command = self.command(*self.make(**options))
                self.assertNotIn('-hwaccel', command)
                self.assertNotIn('hwdownload', command[command.index('-filter_complex')+1])


@unittest.skipUnless(os.environ.get('OKK_TEST_NVENC') == '1', 'enable local GPU pixel verification')
class StackCudaMediaTests(unittest.TestCase):
    def setUp(self):
        app = Path(__file__).resolve().parents[1]
        self.ffmpeg, self.ffprobe = str(app/'bin/ffmpeg.exe'), str(app/'bin/ffprobe.exe')
        self.caps = detect_capabilities(app)
        if not self.caps.cuda_decode or 'hevc_nvenc' not in self.caps.nvenc_codecs:
            self.skipTest('CUDA/HEVC NVENC required')
        self.temp = tempfile.TemporaryDirectory(prefix='stack-cuda-pixels-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder/'source.mp4'
        self.command([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc2=s=320x180:r=30:d=2', '-an', '-vf', 'format=yuv420p10le',
            '-c:v', 'hevc_nvenc', '-preset', 'p1', '-tune', 'lossless',
            '-rc', 'constqp', '-qp', '0', '-bf', '0', str(self.source)])
        self.alpha = self.folder/'alpha.mov'
        self.command([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=red@0.5:s=320x180:r=30:d=2,format=argb', '-an', '-c:v', 'qtrle', str(self.alpha)])
        self.project = MulticamProject(fps=30., clips=[
            CameraClip('opaque', 1, str(self.source), 0., 2.),
            CameraClip('alpha', 2, str(self.alpha), 0., 2., has_alpha=True)],
            cuts=[Cut(0., 1)], video_stack_enabled=True, video_stack_order=[2, 1],
            clip_grades=[ClipGrade(0., 1., ColorGrade(exposure=.2)),
                         ClipGrade(1., 2., ColorGrade(contrast=1.1))],
            global_grade=ColorGrade(saturation=.8),
            clip_transforms=[ClipTransformRange(0., 2., ClipTransform('fill', 1.1))])
        self.settings = ExportSettings(codec='libx264', preset='ultrafast', quality=0,
            audio='none', decode_mode='cpu', bit_depth='8-bit', parallel_workers=1,
            extra_args=['-threads', '1'])

    def command(self, args):
        result = subprocess.run(args, capture_output=True, timeout=45, creationflags=CREATE_NO_WINDOW)
        if result.returncode: raise AssertionError(result.stderr.decode('utf-8', 'replace')[-2000:])
        return result.stdout

    def runner(self, command, report):
        result = subprocess.run(command, capture_output=True, timeout=45, creationflags=CREATE_NO_WINDOW)
        return result.returncode, result.stderr.decode('utf-8', 'replace')

    def render(self, label, cuda, runner=None):
        output = self.folder/(label+'.mp4')
        render_multicam(self.project, output, self.ffmpeg, self.ffprobe,
            replace(self.settings, decode_mode='cuda' if cuda else 'cpu'), self.caps,
            runner or self.runner, work_directory=self.folder)
        return self.command([self.ffmpeg, '-v', 'error', '-i', str(output),
            '-pix_fmt', 'yuv420p', '-f', 'rawvideo', 'pipe:1'])

    def test_gpu_decode_keeps_cpu_alpha_framing_and_grade_pixels_exact(self):
        reference = self.render('cpu', False)
        seen = []
        def runner(command, report):
            seen.append(command)
            return self.runner(command, report)
        hardware = self.render('cuda', True, runner)
        self.assertEqual(hardware, reference)
        commands = [command for command in seen if '-hwaccel' in command]
        self.assertTrue(commands)
        for command in commands:
            opaque = command.index(str(self.source))
            transparent = command.index(str(self.alpha))
            self.assertLess(command.index('-hwaccel'), opaque)
            # Input options must not attach NVDEC to the transparent QTRLE source.
            self.assertNotIn('-hwaccel', command[opaque+1:transparent])

    def test_hardware_failure_retries_cpu_without_losing_effects_or_repeating_failure(self):
        reference = self.render('reference', False)
        attempted = []
        def runner(command, report):
            attempted.append(command)
            if '-hwaccel' in command: return 1, 'synthetic CUDA decoder failure'
            return self.runner(command, report)
        actual = self.render('fallback', True, runner)
        self.assertEqual(actual, reference)
        self.assertEqual(sum('-hwaccel' in command for command in attempted), 1)
        self.assertGreaterEqual(len(attempted), 3)


if __name__ == '__main__': unittest.main()
