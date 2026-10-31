"""Synthetic FFmpeg pixels: Open Gate, SAR, range isolation and transitions."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from clip_transform import ClipTransform, framing_filter, set_transform_on_range
from creative_core import Transition
from exporter_core import CREATE_NO_WINDOW, Capabilities, ExportSettings
from multicam_core import CameraClip, MulticamProject, render_multicam


class ClipTransformRenderTests(unittest.TestCase):
    width, height = 160, 90

    @classmethod
    def command(cls, command, *, input=None):
        result = subprocess.run([str(arg) for arg in command], input=input,
                                capture_output=True, timeout=45, creationflags=CREATE_NO_WINDOW)
        if result.returncode:
            raise AssertionError(result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(__file__).resolve().parents[1] / 'bin' / 'ffmpeg.exe'
        cls.ffprobe = cls.ffmpeg.with_name('ffprobe.exe')
        if not cls.ffmpeg.is_file() or not cls.ffprobe.is_file():
            raise unittest.SkipTest('bundled FFmpeg/ffprobe unavailable')
        cls.temp = tempfile.TemporaryDirectory(prefix='clip_transform_pixels_')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / 'open_gate_4x3.mkv'
        cls.mixed_source = cls.root / 'open_gate_25fps.mkv'
        cls.anamorphic = cls.root / 'anamorphic.mkv'
        for path, fps in ((cls.source, 10), (cls.mixed_source, 25)):
            # Each luma plane gives independent x/y coordinates and a small
            # frame identifier. FFV1 preserves every generated sample.
            raw = bytearray()
            for frame in range(fps * 4):
                raw.extend(bytes(32 + x // 2 + y + frame % 4
                                 for y in range(120) for x in range(160)))
                raw.extend(bytes([94]) * (160 * 120))
                raw.extend(bytes([166]) * (160 * 120))
            cls.command([cls.ffmpeg, '-v', 'error', '-y', '-f', 'rawvideo', '-pixel_format', 'yuv444p',
                         '-video_size', '160x120', '-framerate', fps, '-i', 'pipe:0', '-an',
                         '-c:v', 'ffv1', '-threads', '1', path], input=bytes(raw))
        cls.command([cls.ffmpeg, '-v', 'error', '-y', '-i', cls.source, '-vf', 'setsar=4/3',
                     '-c:v', 'ffv1', '-threads', '1', cls.anamorphic])

    def make(self, *, mixed=False):
        return MulticamProject(clips=[CameraClip('a', 1,
            str(self.mixed_source if mixed else self.source), -.017 if mixed else 0, 4)],
            fps=30 if mixed else 10,
            project_settings=dict(width=160, height=90, sample_rate=48000, bit_depth='24bit'))

    @staticmethod
    def runner(command, report):
        result = subprocess.run(command, capture_output=True, timeout=45, creationflags=CREATE_NO_WINDOW)
        if report is not None:
            report(dict(frame='1', out_time_us='100000'))
        return result.returncode, result.stderr.decode('utf-8', 'replace')

    def render(self, project, name, *, workers=1, depth='8-bit'):
        output = self.root / (name + '.mov')
        settings = ExportSettings(codec='libx264', preset='ultrafast', quality=0, audio='none',
                                  resolution='timeline', decode_mode='cpu', parallel_workers=workers,
                                  bframes=0, bit_depth=depth, extra_args=['-threads', '1'])
        render_multicam(project, output, str(self.ffmpeg), str(self.ffprobe), settings,
                        Capabilities(), self.runner)
        raw = self.command([self.ffmpeg, '-v', 'error', '-i', output, '-map', '0:v:0',
                            '-an', '-pix_fmt', 'yuv420p', '-f', 'rawvideo', 'pipe:1'])
        size = self.width * self.height * 3 // 2
        self.assertEqual(len(raw) % size, 0)
        frames = [raw[i:i + size] for i in range(0, len(raw), size)]
        stream = json.loads(self.command([self.ffprobe, '-v', 'error', '-select_streams', 'v:0',
            '-count_frames', '-show_entries', 'stream=width,height,sample_aspect_ratio,pix_fmt,nb_read_frames',
            '-of', 'json', output]))['streams'][0]
        self.assertEqual((stream['width'], stream['height'], stream['sample_aspect_ratio']), (160, 90, '1:1'))
        return frames, stream

    def filter_frame(self, transform, *, source=None, width=160, height=90):
        raw = self.command([self.ffmpeg, '-v', 'error', '-i', source or self.source,
            '-vf', framing_filter(transform, width, height), '-frames:v', '1',
            '-pix_fmt', 'yuv444p', '-f', 'rawvideo', 'pipe:1'])
        self.assertEqual(len(raw), width * height * 3)
        return raw

    def luma(self, frame, x=80, y=45):
        return frame[y * self.width + x]

    def test_open_gate_fill_16x9_crops_without_stretching_and_selects_vertical_origin(self):
        for offset, source_y in ((-1, 0), (0, 15), (1, 30)):
            with self.subTest(offset=offset):
                frame = self.filter_frame(ClipTransform('fill', 1, 0, offset))
                for x, y in ((0, 0), (80, 45), (159, 89)):
                    self.assertEqual(self.luma(frame, x, y), 32 + x // 2 + y + source_y)
                # Pixel-coordinate slopes stay unchanged: the source is
                # cropped, rather than squeezed from 4:3 into 16:9.
                self.assertEqual(self.luma(frame, 100, 45) - self.luma(frame, 80, 45), 10)
                self.assertEqual(self.luma(frame, 80, 65) - self.luma(frame, 80, 45), 20)

    def test_fit_keeps_whole_frame_and_zoom_crop_moves_in_both_axes(self):
        fit = self.filter_frame(ClipTransform('fit', 1, .5, .5))
        identity = self.filter_frame(ClipTransform())
        self.assertEqual(fit, identity)
        for x in (0, 19, 140, 159):
            self.assertEqual(self.luma(fit, x, 45), 16)
        self.assertGreater(self.luma(fit, 80, 89) - self.luma(fit, 80, 0), 115)
        left = self.filter_frame(ClipTransform('fill', 2, -1, 0))
        right = self.filter_frame(ClipTransform('fill', 2, 1, 0))
        top = self.filter_frame(ClipTransform('fill', 2, 0, -1))
        bottom = self.filter_frame(ClipTransform('fill', 2, 0, 1))
        self.assertAlmostEqual(self.luma(right) - self.luma(left), 40, delta=1)
        self.assertAlmostEqual(self.luma(bottom) - self.luma(top), 75, delta=1)
        zoomed = self.filter_frame(ClipTransform('fit', 2))
        self.assertGreater(self.luma(zoomed, 0, 45), 20)

    def test_transparent_stack_padding_preserves_alpha_for_identity_and_active_fit(self):
        for transform in (ClipTransform(), ClipTransform('fit', 1.2)):
            with self.subTest(transform=transform):
                chain = ('format=rgba,' + framing_filter(transform, 160, 90, pad_color='black@0'))
                raw = self.command([self.ffmpeg, '-v', 'error', '-i', self.source, '-vf', chain,
                    '-frames:v', '1', '-pix_fmt', 'rgba', '-f', 'rawvideo', 'pipe:1'])
                self.assertEqual(len(raw), 160 * 90 * 4)
                self.assertEqual(raw[(45 * 160) * 4 + 3], 0)
                self.assertEqual(raw[(45 * 160 + 80) * 4 + 3], 255)

    def test_fill_and_fit_honor_anamorphic_display_aspect(self):
        # 160x120 with SAR=4:3 is already a 16:9 displayed picture. Correct
        # framing retains its complete vertical extent at a 160x90 target.
        for transform in (ClipTransform('fill'), ClipTransform('fit', 1, .5, .5)):
            with self.subTest(transform=transform):
                frame = self.filter_frame(transform, source=self.anamorphic)
                self.assertGreater(self.luma(frame, 80, 89) - self.luma(frame, 80, 0), 115)
                self.assertAlmostEqual(self.luma(frame, 0, 0), 32, delta=1)
                self.assertAlmostEqual(self.luma(frame, 159, 89), 230, delta=1)

    def test_program_export_only_changes_selected_frames_and_partial_range_matches(self):
        value = self.make()
        before, _ = self.render(value, 'fit_baseline')
        set_transform_on_range(value, 1, 2, ClipTransform('fill', 1, 0, -1))
        after, _ = self.render(value, 'only_middle_crop', workers=2)
        self.assertEqual(len(after), 40)
        self.assertEqual(after[:10], before[:10])
        self.assertEqual(after[20:], before[20:])
        self.assertNotEqual(after[10:20], before[10:20])
        self.assertGreater(self.luma(after[10], 0, 45), 16)
        value.range_start, value.range_end = .8, 2.2
        partial, _ = self.render(value, 'selected_export')
        self.assertEqual(partial, after[8:22])

    def test_mixed_fps_transform_edges_preserve_unaffected_frame_sampling(self):
        value = self.make(mixed=True)
        value.range_end = 3.5
        before, _ = self.render(value, 'mixed_before', workers=2)
        set_transform_on_range(value, .23333, 1.0333, ClipTransform('fill', 1.5, -.5, .5))
        after, _ = self.render(value, 'mixed_after', workers=2)
        self.assertEqual(after[:7], before[:7])
        self.assertEqual(after[31:], before[31:])
        self.assertNotEqual(after[7:31], before[7:31])

    def test_same_camera_transition_has_each_sides_framing_and_partial_phase(self):
        value = self.make()
        set_transform_on_range(value, 0, 2, ClipTransform('fill', 1, 0, -1))
        set_transform_on_range(value, 2, 4, ClipTransform('fill', 1, 0, 1))
        value.transitions = [Transition(2, 'fade', 1)]
        complete, stream = self.render(value, 'framed_transition')
        self.assertEqual(stream['nb_read_frames'], '40')
        a, middle, b = [self.luma(complete[index]) for index in (15, 20, 24)]
        self.assertLess(a, middle)
        self.assertLess(middle, b)
        self.assertGreater(b - a, 20)
        partial = deepcopy(value)
        partial.range_start, partial.range_end = 1.8, 2.2
        frames, _ = self.render(partial, 'partial_framed_transition')
        self.assertEqual(frames, complete[18:22])

    def test_framing_and_transition_retain_ten_bit_dimensions(self):
        value = self.make()
        set_transform_on_range(value, 0, 2, ClipTransform('fill', 1.5, -1, -1))
        set_transform_on_range(value, 2, 4, ClipTransform('fill', 2, 1, 1))
        value.transitions = [Transition(2, 'fade', 1)]
        value.range_start, value.range_end = 1.5, 2.5
        frames, stream = self.render(value, 'framing_ten_bit', depth='10-bit')
        self.assertEqual(len(frames), 10)
        self.assertEqual(stream['pix_fmt'], 'yuv420p10le')

    def test_stack_crop_recovers_open_gate_source_and_only_changes_selected_frames(self):
        value = self.make()
        value.video_stack_enabled = True
        before, _ = self.render(value, 'stack_fit_before')
        set_transform_on_range(value, 1, 2, ClipTransform('fill', 1, 0, 1))
        after, stream = self.render(value, 'stack_selected_fill')
        self.assertEqual(stream['nb_read_frames'], '40')
        self.assertEqual(after[:10], before[:10])
        self.assertEqual(after[20:], before[20:])
        self.assertGreater(self.luma(after[10], 0, 45), 16)
        self.assertNotEqual(after[10:20], before[10:20])


if __name__ == '__main__':
    unittest.main()
