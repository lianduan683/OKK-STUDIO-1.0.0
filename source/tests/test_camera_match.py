"""Numerical matching and actual FFmpeg verification of graded references."""
from dataclasses import replace
from pathlib import Path
import math
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_color import analyze_rgb_samples
from camera_match import (paired_sample_times, propose_grade, camera_filter, match_camera,
                          _read_frame, CameraMatchResult, SAMPLE_WIDTH, SAMPLE_HEIGHT)
from creative_core import ColorGrade
from exporter_core import CREATE_NO_WINDOW, ExportCancelled
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import set_grade_on_range


class CameraMatchModelTests(unittest.TestCase):
    def test_sample_times_use_only_real_simultaneous_recordings_across_gaps(self):
        project = MulticamProject(clips=[CameraClip('r1', 1, 'r1', 0, 3),
            CameraClip('r2', 1, 'r2', 7, 3), CameraClip('t1', 2, 't1', 1, 1),
            CameraClip('t2', 2, 't2', 8, 2)], fps=30)
        times = paired_sample_times(project, 1, 2, 0, 10)
        self.assertEqual(len(times), 5)
        self.assertTrue(all(1 <= time < 2 or 8 <= time < 10 for time in times))
        self.assertEqual(paired_sample_times(project, 1, 2, 3, 7), ())

    def test_matching_preserves_manual_wheels_curves_and_lut(self):
        target = analyze_rgb_samples([bytes((120, 110, 100)) * 1000])
        reference = analyze_rgb_samples([bytes((145, 145, 145)) * 1000])
        base = ColorGrade(lift_r=.03, gamma_y=.05, curve_master=((0., 0.), (.5, .57), (1., 1.)), lut_path='manual.cube')
        grade = propose_grade(base, target, reference)
        self.assertEqual((grade.lift_r, grade.gamma_y, grade.curve_master, grade.lut_path),
                         (base.lift_r, base.gamma_y, base.curve_master, base.lut_path))
        self.assertGreater(grade.exposure, base.exposure)
        self.assertLess(grade.wb_red, grade.wb_blue)
        self.assertEqual(propose_grade(base, target, reference, strength=0), base)
        for amount in (float('nan'), -1, 1.01, True):
            with self.assertRaises(ValueError):
                propose_grade(base, target, reference, strength=amount)

    def test_unrelated_saturated_background_is_not_used_as_a_neutral_reference(self):
        target = analyze_rgb_samples([bytes((50, 20, 100)) * 1000])
        reference = analyze_rgb_samples([bytes((20, 80, 30)) * 1000])
        with self.assertRaisesRegex(ValueError, '共同'):
            propose_grade(ColorGrade(), target, reference)

    def test_reference_uses_its_chosen_local_grade_without_borrowing_target_clip_grades(self):
        project = MulticamProject(clips=[CameraClip('r', 1, 'r', 0, 4), CameraClip('t', 2, 't', 0, 4)],
            cuts=[Cut(0, 1), Cut(2, 2)], fps=30, global_grade=ColorGrade(exposure=.1),
            camera_grades={1: ColorGrade(exposure=.2), 2: ColorGrade(exposure=-.1)})
        set_grade_on_range(project, 0, 2, ColorGrade(exposure=.3))
        set_grade_on_range(project, 2, 4, ColorGrade(exposure=-.4))
        from creative_core import grade_filter
        reference = camera_filter(project, 1, 3, reference_time=1)
        self.assertEqual(reference, ','.join(grade_filter(ColorGrade(exposure=e)) for e in (.2, .3, .1)))
        target = camera_filter(project, 2, 1)
        self.assertEqual(target, ','.join(grade_filter(ColorGrade(exposure=e)) for e in (-.1, .1)))


class CameraMatchMediaTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1]
        self.ffmpeg, self.ffprobe = folder / 'bin' / 'ffmpeg.exe', folder / 'bin' / 'ffprobe.exe'
        if not self.ffmpeg.is_file():
            self.skipTest('Bundled FFmpeg unavailable')
        self.temp = tempfile.TemporaryDirectory(prefix='standard-camera-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def source(self, name, rgb):
        path = self.folder / (name + '.mkv')
        raw = bytes(rgb) * 64 * 36 * 20
        process = subprocess.run([str(self.ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', '64x36', '-framerate', '10',
            '-i', 'pipe:0', '-c:v', 'ffv1', '-pix_fmt', 'bgr0', str(path)], input=raw,
            capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(process.returncode, 0, process.stderr.decode('utf-8', 'replace'))
        return str(path)

    def project(self):
        project = MulticamProject(clips=[CameraClip('r', 1, self.source('reference', (96, 96, 96)), 0, 2),
            CameraClip('t', 2, self.source('target', (108, 100, 90)), 0, 2)],
            cuts=[Cut(0, 1), Cut(1, 2)], fps=10, camera_grades={1: ColorGrade(exposure=.2)},
            global_grade=ColorGrade(exposure=.1))
        set_grade_on_range(project, 0, 1, ColorGrade(exposure=.2))
        return project

    def test_real_frames_match_graded_reference_and_saved_grade_uses_same_render_transform(self):
        project = self.project()
        original_reference = replace(project.camera_grades[1])
        result = match_camera(project, 1, 2, 0, 1, str(self.ffmpeg), str(self.ffprobe), reference_time=.5)
        self.assertIsNotNone(result.grade, result.message)
        self.assertLess(result.after, result.before * .4)
        self.assertGreater(result.improvement, 60)
        self.assertEqual(len(result.matched_pixels), SAMPLE_WIDTH * SAMPLE_HEIGHT * 3)
        self.assertNotIn(2, project.camera_grades)
        project.camera_grades[2] = result.grade
        destination = self.folder / 'matched.multicam.json'
        project.save(destination)
        restored = MulticamProject.load(destination)
        self.assertEqual(restored.camera_grades[1], original_reference)
        pixels = _read_frame(restored, 2, 1.5, str(self.ffmpeg), None)
        self.assertEqual(pixels, result.matched_pixels)

    def test_near_match_does_not_accumulate_exposure_when_repeated(self):
        project = self.project()
        first = match_camera(project, 1, 2, 0, 1, str(self.ffmpeg), reference_time=.5)
        self.assertIsNotNone(first.grade)
        project.camera_grades[2] = first.grade
        second = match_camera(project, 1, 2, 0, 1, str(self.ffmpeg), reference_time=.5)
        if second.grade is not None:
            self.assertLess(abs(second.grade.exposure - first.grade.exposure), .03)
        else:
            self.assertIn('接近', second.message)

    def test_reference_lut_is_measured_and_reference_camera_is_unchanged(self):
        project = self.project()
        lut = self.folder / 'cool-reference.cube'
        rows = ['TITLE "test reference"', 'LUT_3D_SIZE 2', 'DOMAIN_MIN 0 0 0', 'DOMAIN_MAX 1 1 1']
        for blue in (0., 1.):
            for green in (0., 1.):
                for red in (0., 1.):
                    rows.append(f'{red * .90} {green} {blue}')
        lut.write_text('\n'.join(rows) + '\n', encoding='ascii')
        project.camera_grades[1] = replace(project.camera_grades[1], lut_path=str(lut))
        original = replace(project.camera_grades[1])
        result = match_camera(project, 1, 2, 0, 1, str(self.ffmpeg), reference_time=.5)
        self.assertIsNotNone(result.grade, result.message)
        self.assertLess(result.after, result.before * .5)
        self.assertEqual(project.camera_grades[1], original)
        channels = [sum(result.reference_pixels[channel::3]) for channel in range(3)]
        self.assertLess(channels[0], channels[2] * .94)

    def test_no_overlap_and_cancel_do_not_modify_camera_grades(self):
        project = self.project()
        project.clips[1].start = 3
        result = match_camera(project, 1, 2, 0, 2, str(self.ffmpeg))
        self.assertIsNone(result.grade)
        self.assertIn('同时录制', result.message)
        project.clips[1].start = 0
        event = threading.Event(); event.set()
        with self.assertRaises(ExportCancelled):
            match_camera(project, 1, 2, 0, 1, str(self.ffmpeg), cancel_event=event)
        self.assertNotIn(2, project.camera_grades)

    def test_worse_render_candidates_are_rejected_instead_of_overwriting_manual_grade(self):
        project = self.project()
        def read(*args, **kwargs):
            if kwargs.get('override') is not None:
                return bytes((20, 20, 20)) * SAMPLE_WIDTH * SAMPLE_HEIGHT
            return _read_frame(*args, **kwargs)
        with patch('camera_match._read_frame', side_effect=read):
            result = match_camera(project, 1, 2, 0, 1, str(self.ffmpeg))
        self.assertIsNone(result.grade)
        self.assertIn('未找到可靠改善', result.message)


if __name__ == '__main__':
    unittest.main()
