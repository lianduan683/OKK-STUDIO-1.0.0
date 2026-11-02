"""Bounded CUDA prescaling keeps CPU colour/alpha composition authoritative."""
from array import array
from dataclasses import replace
from fractions import Fraction
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from clip_transform import ClipTransform, ClipTransformRange
from creative_core import ColorGrade
from exporter_core import CREATE_NO_WINDOW, MediaInfo
from multicam_core import CameraClip, Cut, MulticamProject
from video_stack_render import build_stack_command, plan_stack_segments


class StackPrescalePlanTests(unittest.TestCase):
    def setUp(self):
        self.project = MulticamProject(fps=30., clips=[CameraClip('a', 1, 'a.mov', 0., 2.)],
            cuts=[Cut(0., 1)], video_stack_enabled=True,
            camera_grades={1: ColorGrade(exposure=.2, gamma=1.1)},
            clip_transforms=[ClipTransformRange(0., 2., ClipTransform('fill', 1., .5, -.7))])
        self.segment = plan_stack_segments(self.project)[0]
        self.info = MediaInfo('a.mov', width=7680, height=4320, fps=30., bit_depth=10,
            video_codec='hevc', pixel_format='yuv420p10le', color_primaries='bt709',
            color_transfer='bt709', color_space='bt709', color_range='pc')

    def graph(self, *, info=None, segment=None, project=None, **options):
        command = build_stack_command(segment or self.segment, project or self.project,
            ['ffmpeg'], 3840, 2160, 'yuv420p10le', '30/1', [], 'out.mp4',
            media_by_path={'a.mov': info or self.info}, decode_mode='cuda', **options)
        return command[command.index('-filter_complex')+1]

    def test_full_and_limited_range_keep_all_cpu_effects_after_downloading_smaller_surface(self):
        prefix = 'scale_cuda=w=3840:h=2160:interp_algo=bicubic:format=p010le,'
        for color_range in ('pc', 'tv'):
            info = replace(self.info, color_range=color_range)
            original = self.graph(info=info)
            accelerated = self.graph(info=info, cuda_prescale=True)
            self.assertIn(prefix + 'hwdownload,format=p010le,', accelerated)
            self.assertEqual(accelerated.replace(prefix, ''), original)
            self.assertIn('lutrgb=', accelerated)
            self.assertIn('alphaextract', accelerated)
            self.assertIn('overlay=', accelerated)

    def test_geometry_alpha_signal_and_unavailable_prescaler_remain_unchanged(self):
        changes = [dict(info=replace(self.info, width=8192)),
                   dict(info=replace(self.info, width=1920, height=1080)),
                   dict(info=replace(self.info, sample_aspect_ratio='4:3')),
                   dict(info=replace(self.info, color_range=None)),
                   dict(info=replace(self.info, color_transfer='smpte2084')),
                   dict(info=replace(self.info, has_display_transform=True)),
                   dict(info=replace(self.info, field_order='tt')),
                   dict(info=replace(self.info, has_alpha=True)),
                   dict(segment=replace(self.segment, layers=(replace(self.segment.layers[0],
                        placement=(.1, 0., 1.)),))),
                   dict(project=replace(self.project, clip_transforms=[
                        ClipTransformRange(0., 2., ClipTransform('fill', 1.2))]))]
        for change in changes:
            with self.subTest(change=change):
                self.assertNotIn('scale_cuda=', self.graph(cuda_prescale=True, **change))
        self.assertNotIn('scale_cuda=', self.graph())

    def test_source_frame_phase_and_original_alpha_are_not_bypassed(self):
        layer = replace(self.segment.layers[0], path='treated.mov', source_start=.7)
        segment = replace(self.segment, start=.7, duration=.6, frames=18, layers=(layer,))
        command = build_stack_command(segment, self.project, ['ffmpeg'], 3840, 2160,
            'yuv420p10le', '30/1', [], 'out.mp4', decode_mode='cuda', cuda_prescale=True,
            source_frame_rates={'treated.mov': Fraction(24)},
            media_by_path={'treated.mov': self.info, 'a.mov': replace(self.info, has_alpha=True)})
        graph = command[command.index('-filter_complex')+1]
        self.assertNotIn('scale_cuda=', graph)
        self.assertIn('trim=start_frame=1:end_frame=19', graph)
        self.assertEqual(command[command.index('-ss')+1], '0.666666667')


@unittest.skipUnless(os.environ.get('OKK_TEST_GPU_PRESCALE') == '1',
                     'enable only during scheduled small GPU pixel verification')
class StackPrescalePixelTests(unittest.TestCase):
    width, height, frames = 320, 180, 3

    def run_command(self, command, **kwargs):
        result = subprocess.run(command, capture_output=True, timeout=30,
                                creationflags=CREATE_NO_WINDOW, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace')[-4000:])
        return result.stdout

    def test_full_and_limited_range_gradients_keep_black_white_and_grade_values(self):
        app = Path(__file__).resolve().parents[1]
        ffmpeg = str(app/'bin'/'ffmpeg.exe')
        with tempfile.TemporaryDirectory(prefix='stack-prescale-pixels-') as temporary:
            folder = Path(temporary)
            for color_range in ('pc', 'tv'):
                with self.subTest(color_range=color_range):
                    low, high = (0, 1023) if color_range == 'pc' else (64, 940)
                    # Flat black/white margins expose accidental full/limited
                    # conversion; the centre exposes scaling/grade differences.
                    luminance = array('H', (low if x < 64 else high if x >= 576 else
                        round(low+(high-low)*(x-64)/511) for y in range(360) for x in range(640)))
                    chroma = array('H', [512]) * (640*360//2)
                    source = folder/(color_range+'.mp4')
                    self.run_command([ffmpeg, '-v', 'error', '-y', '-threads', '1',
                        '-f', 'rawvideo', '-pixel_format', 'yuv420p10le', '-video_size', '640x360',
                        '-framerate', '30', '-i', 'pipe:0', '-an', '-c:v', 'hevc_nvenc',
                        '-preset', 'p1', '-tune', 'lossless', '-rc', 'constqp', '-qp', '0', '-bf', '0',
                        '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
                        '-color_range', color_range, str(source)],
                        input=(luminance.tobytes()+chroma.tobytes())*self.frames)
                    info = MediaInfo(str(source), width=640, height=360, fps=30., bit_depth=10,
                        video_codec='hevc', pixel_format='yuv420p10le', color_primaries='bt709',
                        color_transfer='bt709', color_space='bt709', color_range=color_range)
                    project = MulticamProject(fps=30., clips=[
                        CameraClip('a', 1, str(source), 0., self.frames/30)], cuts=[Cut(0., 1)],
                        video_stack_enabled=True, camera_grades={1: ColorGrade(exposure=.04,
                        contrast=.97, gamma=1.12, skin_lighten=.1)},
                        clip_transforms=[ClipTransformRange(0., self.frames/30,
                                                           ClipTransform('fill', 1., 0., -.7))])
                    segment = plan_stack_segments(project)[0]
                    outputs = []
                    for accelerated in (False, True):
                        output = folder/f'{color_range}-{accelerated}.raw'
                        command = build_stack_command(segment, project, [ffmpeg, '-v', 'error', '-y'],
                            self.width, self.height, 'yuv420p10le', '30/1',
                            ['-c:v', 'rawvideo', '-threads', '1', '-f', 'rawvideo'], str(output),
                            media_by_path={str(source): info}, decode_mode='cuda',
                            cuda_prescale=accelerated, decode_threads=1, filter_threads=1)
                        self.run_command(command)
                        values = array('H'); values.frombytes(output.read_bytes()); outputs.append(values)
                    count = self.width*self.height*3//2*self.frames
                    self.assertEqual([len(values) for values in outputs], [count, count])
                    differences = [abs(a-b) for a,b in zip(*outputs)]
                    mse = sum(value*value for value in differences)/count
                    psnr = 10*math.log10(1023**2/mse) if mse else float('inf')
                    self.assertGreater(psnr, 40., f'{color_range}: PSNR {psnr}')
                    self.assertLess(sum(differences)/count, 4.)
                    for x in (8, self.width-9):
                        a = sum(outputs[0][y*self.width+x] for y in range(self.height))/self.height
                        b = sum(outputs[1][y*self.width+x] for y in range(self.height))/self.height
                        self.assertLessEqual(abs(a-b), 4., (color_range, x, a, b))


if __name__ == '__main__': unittest.main()
