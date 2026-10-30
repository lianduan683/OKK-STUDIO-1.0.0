"""Real synthetic media, bounded sampling and read-only review workflows."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exposure_check import ExposureService, ScanOptions, analyze_pixels, encode_png, extract_frame, plan_samples
from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject
from exporter_core import CREATE_NO_WINDOW

REVIEW = dict(classification='normal', confidence=.84, reason='主体细节可辨，黑色背景属于舞台照明。', intentional_lighting=True, regions=[])


def wait(service, scan_id, *, review=False):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        report = service.get(scan_id)
        if report['review_status' if review else 'status'] != 'running': return report
        time.sleep(.02)
    raise AssertionError('exposure worker did not complete')


class ExposureModelTests(unittest.TestCase):
    def test_bright_dark_normal_and_mixed_are_only_local_hints(self):
        self.assertEqual(analyze_pixels(bytes((0, 0, 0)) * 100)['classification'], 'underexposed')
        self.assertEqual(analyze_pixels(bytes((255, 255, 255)) * 100)['classification'], 'overexposed')
        self.assertEqual(analyze_pixels(bytes((128, 128, 128)) * 100)['classification'], 'normal')
        result = analyze_pixels(bytes((0, 0, 0)) * 80 + bytes((255, 255, 255)) * 20)
        self.assertEqual(result['classification'], 'mixed')
        self.assertIn('拍摄意图', result['reason'])

    def test_sampling_caps_cover_the_whole_requested_timeline(self):
        project = MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', 0, 3600), CameraClip('b', 2, 'b.mp4', 0, 3600)], fps=30)
        options = ScanOptions.parse(dict(scope='cameras', interval=.5, max_frames=30), project)
        samples, requested = plan_samples(project, options)
        self.assertGreater(requested, 1000)
        self.assertEqual(len(samples), 30)
        self.assertEqual({c for c, _ in samples}, {1, 2})
        self.assertGreater(max(t for _, t in samples), 3500)
        self.assertLess(min(t for _, t in samples), 100)
        with self.assertRaises(ValueError): ScanOptions.parse(dict(max_frames=301), project)
        with self.assertRaises(ValueError): ScanOptions.parse(dict(interval=float('nan')), project)

    def test_program_scan_does_not_include_unselected_cameras(self):
        project = MulticamProject(clips=[CameraClip('a', 1, 'a', 0, 5), CameraClip('b', 2, 'b', 0, 5)],
                                   cuts=[Cut(0, 1), Cut(2, 2)], fps=30)
        samples, _ = plan_samples(project, ScanOptions.parse(dict(interval=.5), project))
        self.assertTrue(all(c == (1 if t < 2 else 2) for c, t in samples))

    def test_disabled_clips_are_not_sampled(self):
        project = MulticamProject(clips=[CameraClip('a', 1, 'a', 0, 5),
            CameraClip('b', 2, 'b', 0, 5, enabled=False)], fps=30)
        samples, _ = plan_samples(project, ScanOptions.parse(dict(scope='cameras'), project))
        self.assertEqual({camera for camera, _ in samples}, {1})


class ExposureMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='exposure-check-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ffmpeg = Path(__file__).resolve().parents[1] / 'bin' / 'ffmpeg.exe'
        if not self.ffmpeg.is_file(): self.skipTest('Bundled FFmpeg unavailable')
        self.project = MulticamProject(fps=10)
        for camera, color in ((1, 'black'), (2, 'white'), (3, 'gray')):
            path = self.root / f'{camera}.mp4'
            result = subprocess.run([str(self.ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                f'color=c={color}:s=96x54:r=10:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)],
                capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0)
            self.project.clips.append(CameraClip(str(camera), camera, str(path), 0, 1))
        self.project.cuts = [Cut(0, 3)]
        self.service = ExposureService(str(self.ffmpeg), self.root / 'cache')
        self.addCleanup(self.service.close)

    def scan(self, **options):
        initial = self.service.start(self.project, dict(scope='cameras', interval=.5, **options))
        report = wait(self.service, initial['scan_id'])
        self.assertEqual(report['status'], 'complete', report['message'])
        self.assertEqual(report['error_count'], 0, report['frames'])
        return report

    def test_real_camera_scan_saves_png_and_keeps_the_project_unchanged(self):
        before = copy.deepcopy(self.project)
        report = self.scan()
        kinds = {item['camera']: item['local']['classification'] for item in report['frames']}
        self.assertEqual(kinds, {1: 'underexposed', 2: 'overexposed', 3: 'normal'})
        self.assertEqual(self.project, before)
        self.assertNotIn('image_path', json.dumps(report))
        frame = self.service.frame(report['scan_id'], report['frames'][0]['frame_id'])
        self.assertTrue(Path(frame['image_path']).read_bytes().startswith(b'\x89PNG'))

    def test_grade_changes_are_present_in_graded_scans(self):
        self.project.camera_grades[3] = ColorGrade(exposure=2)
        raw = self.scan(view='source', cameras=[3])
        graded = self.scan(view='graded', cameras=[3])
        self.assertEqual(raw['frames'][0]['local']['classification'], 'normal')
        self.assertEqual(graded['frames'][0]['local']['classification'], 'overexposed')

    def test_trimmed_source_uses_original_source_in(self):
        path = self.root / 'black-white.mp4'
        result = subprocess.run([str(self.ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=c=black:s=96x54:r=10:d=1', '-f', 'lavfi', '-i',
            'color=c=white:s=96x54:r=10:d=1', '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
            '-map', '[v]', '-c:v', 'libx264', str(path)], capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        self.project.clips = [CameraClip('trim', 1, str(path), 0, .8, source_in=1.1)]
        self.project.cuts = [Cut(0, 1)]
        report = self.scan(view='source')
        self.assertTrue(all(f['local']['classification'] == 'overexposed' for f in report['frames']))
        self.assertGreaterEqual(report['frames'][0]['source_time'], 1.1)

    def test_hdr_is_flagged_for_color_managed_review(self):
        from types import SimpleNamespace
        with patch('exposure_check.probe_media', return_value=SimpleNamespace(
                color_transfer='smpte2084', color_primaries='bt2020')):
            report = self.scan(cameras=[2])
        self.assertEqual(report['frames'][0]['local']['classification'], 'uncertain')
        self.assertIn('HDR', report['frames'][0]['local']['reason'])

    def test_harness_can_explain_intentional_darkness_without_editing_video(self):
        report = self.scan(cameras=[1])
        original = copy.deepcopy(self.project)
        item = report['frames'][0]
        submitted = self.service.submit(report['scan_id'], [{'frame_id': item['frame_id'], 'result': REVIEW}],
                                        report['scan_revision'], self.project, model='harness-test-model')
        self.assertEqual(submitted['frames'][0]['local']['classification'], 'underexposed')
        self.assertEqual(submitted['frames'][0]['review']['classification'], 'normal')
        self.assertEqual(self.project, original)
        self.project.global_grade.exposure = .5
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.service.submit(report['scan_id'], [{'frame_id': item['frame_id'], 'result': REVIEW}],
                                report['scan_revision'], self.project)

    def test_invalid_batch_is_atomic_and_no_synthetic_ai_result_is_created(self):
        report = self.scan(cameras=[1])
        entries = [{'frame_id': report['frames'][0]['frame_id'], 'result': REVIEW}, {'frame_id': 'missing', 'result': REVIEW}]
        with self.assertRaises(ValueError): self.service.submit(report['scan_id'], entries, report['scan_revision'], self.project)
        self.assertTrue(all(item['review'] is None for item in self.service.get(report['scan_id'])['frames']))

    def test_api_error_preserves_local_findings_and_hides_key(self):
        from vision_review import VisionConfig
        report = self.scan(cameras=[1])
        with patch('exposure_check.request_review', side_effect=ValueError('test-key connection error')):
            self.service.review_api(report['scan_id'], [report['frames'][0]['frame_id']], report['scan_revision'],
                                    VisionConfig(base_url='http://127.0.0.1/v1', model='test'), 'test-key', self.project)
            finished = wait(self.service, report['scan_id'], review=True)
        self.assertEqual(finished['review_status'], 'failed')
        self.assertNotIn('test-key', json.dumps(finished))
        self.assertIsNone(finished['frames'][0]['review'])
        self.assertEqual(finished['frames'][0]['local']['classification'], 'underexposed')

    def test_cancellation_keeps_completed_observations(self):
        entered, release = threading.Event(), threading.Event()
        def capture(project, camera, t, view, ffmpeg, event):
            entered.set(); release.wait(2)
            if event.is_set():
                from exporter_core import ExportCancelled
                raise ExportCancelled('cancelled')
            return extract_frame(project, camera, t, view, ffmpeg, event)
        with patch('exposure_check.extract_frame', side_effect=capture):
            report = self.service.start(self.project, {'interval': .5})
            self.assertTrue(entered.wait(2))
            self.service.cancel(report['scan_id']); release.set()
            finished = wait(self.service, report['scan_id'])
        self.assertEqual(finished['status'], 'cancelled')


if __name__ == '__main__': unittest.main()
