"""Bounded read-ahead overlaps real output, and cancellation drains both stages."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from creative_core import Transition
from exporter_core import Capabilities, CREATE_NO_WINDOW, ExportCancelled, ExportSettings, detect_capabilities
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from multicam_outer import GROUP_URI, resolve_compound
from video_stack_render import StackLayer, StackSegment, plan_stack_segments


class RealtimePipelineTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.ffmpeg, self.ffprobe = str(root/'bin/ffmpeg.exe'), str(root/'bin/ffprobe.exe')
        if not Path(self.ffmpeg).is_file(): self.skipTest('bundled FFmpeg required')
        self.temp = tempfile.TemporaryDirectory(prefix='okk-realtime-cache-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder/'source.mp4'
        self.command([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc2=s=320x180:r=30:d=3', '-an', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-threads', '1', str(self.source)])
        self.project = MulticamProject(fps=30.,
            clips=[CameraClip('inside', 1, str(self.source), 0., 3.)],
            cuts=[Cut(0., 1), Cut(1., 1), Cut(2., 1)], edit_points=[1., 2.],
            video_stack_enabled=True, multicam_outer_order=[0])
        self.settings = ExportSettings(codec='libx264', preset='ultrafast', quality=0,
            audio='none', decode_mode='cpu', bit_depth='8-bit', parallel_workers=1,
            extra_args=['-threads', '1'])

    def command(self, args):
        result = subprocess.run(args, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        if result.returncode: raise AssertionError(result.stderr.decode('utf-8', 'replace')[-2500:])
        return result.stdout

    def runner(self, args, report):
        result = subprocess.run(args, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        return result.returncode, result.stderr.decode('utf-8', 'replace')

    def render(self, output, **kwargs):
        return render_multicam(self.project, output, self.ffmpeg, self.ffprobe,
            self.settings, Capabilities(), kwargs.pop('runner', self.runner),
            work_directory=self.folder, **kwargs)

    def test_input_read_ahead_and_output_run_concurrently_with_a_bounded_window(self):
        input_blocked, output_started = threading.Event(), threading.Event()
        original = deepcopy(self.project)
        snapshots = []

        def prepare(segment, *args, **kwargs):
            if segment.start == 1.:
                input_blocked.set()
                self.assertTrue(output_started.wait(5), 'output waited for all input caches')
            return resolve_compound(segment, *args, **kwargs)

        def output(args, report):
            if any(str(x).endswith('camera_000000.mp4') for x in args):
                self.assertTrue(input_blocked.wait(5), 'the next input was not read ahead')
                output_started.set()
            return self.runner(args, report)

        path = self.folder/'pipeline.mp4'
        with patch('multicam_outer.resolve_compound', side_effect=prepare):
            self.render(path, runner=output, metrics=snapshots.append)
        self.assertTrue(output_started.is_set())
        self.assertEqual(self.project, original)
        self.assertTrue(all(s.get('input_prepared_segments', 0)-s.get('completed_segments', 0) <= 2
                            for s in snapshots))
        self.assertEqual(snapshots[-1]['progress_percent'], 100.)
        raw = self.command([self.ffmpeg, '-v', 'error', '-i', str(path), '-pix_fmt', 'yuv420p',
                            '-f', 'rawvideo', 'pipe:1'])
        self.project.cuts = [Cut(0., 1)]
        self.project.edit_points = []
        continuous = self.folder/'continuous.mp4'
        self.render(continuous)
        reference = self.command([self.ffmpeg, '-v', 'error', '-i', str(continuous), '-pix_fmt',
                                  'yuv420p', '-f', 'rawvideo', 'pipe:1'])
        self.assertEqual(raw, reference)

    def test_cancel_stops_input_producer_and_preserves_existing_destination(self):
        entered = threading.Event()
        cancel = threading.Event()
        path = self.folder/'existing.mp4'; path.write_bytes(b'keep existing movie')

        def prepare(segment, *args, **kwargs):
            entered.set()
            while not kwargs['cancel'].is_set(): time.sleep(.01)
            raise ExportCancelled('cancelled input cache')

        with patch('multicam_outer.resolve_compound', side_effect=prepare), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.render, path, cancel_event=cancel)
            self.assertTrue(entered.wait(5))
            cancel.set()
            with self.assertRaises(ExportCancelled): future.result(timeout=5)
        self.assertEqual(path.read_bytes(), b'keep existing movie')

    def test_cache_failure_preserves_destination_and_does_not_prepare_entire_movie(self):
        calls = []
        path = self.folder/'existing.mp4'; path.write_bytes(b'keep existing movie')

        def prepare(segment, *args, **kwargs):
            calls.append(segment.start)
            if segment.start >= 1: raise ValueError('synthetic cache failure')
            return resolve_compound(segment, *args, **kwargs)

        with patch('multicam_outer.resolve_compound', side_effect=prepare):
            with self.assertRaisesRegex(ValueError, 'synthetic cache failure'):
                self.render(path)
        self.assertNotIn(2., calls)
        self.assertEqual(path.read_bytes(), b'keep existing movie')

    def test_export_avoids_preview_only_four_second_splits(self):
        self.project.clips[0].duration = 20.
        self.project.edit_points = []
        self.project.cuts = [Cut(0., 1)]
        self.assertGreater(len(plan_stack_segments(self.project)), 1)
        jobs = plan_stack_segments(self.project, for_export=True)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].frames, 600)

    def test_frame_rounded_transition_boundary_does_not_render_preceding_shot_as_a_cache(self):
        self.project.fps = 10.
        self.project.transitions = [Transition(2.06, 'fade', .8)]
        segment = StackSegment(0., 1.7, 17, (StackLayer(0, GROUP_URI, GROUP_URI, 0., 0.),))
        with patch('multicam_core.render_multicam', side_effect=AssertionError('unnecessary input cache')):
            actual = resolve_compound(segment, self.project, self.ffmpeg, self.ffprobe, 320, 180)
        self.assertEqual(actual.layers[0].path, str(self.source))

    def test_covered_camera_retains_the_legacy_compound_when_it_is_also_an_outer_track(self):
        self.project.multicam_outer_order = [0, 1]
        segment = StackSegment(0., .4, 12, (StackLayer(0, GROUP_URI, GROUP_URI, 0., 0.),))
        with patch('cache_paths.cache_dir', return_value=self.folder), \
                patch('multicam_core.render_multicam', side_effect=RuntimeError('covered compound preserved')):
            with self.assertRaisesRegex(RuntimeError, 'covered compound preserved'):
                resolve_compound(segment, self.project, self.ffmpeg, self.ffprobe, 320, 180)


@unittest.skipUnless(os.environ.get('OKK_TEST_NVENC') == '1', 'enable short local NVENC verification')
class RealtimeGpuCacheTests(RealtimePipelineTests):
    def test_ten_bit_gpu_input_cache_is_lossless_and_starts_before_whole_timeline_preparation(self):
        caps = detect_capabilities(Path(self.ffmpeg).parent.parent)
        if 'hevc_nvenc' not in caps.nvenc_codecs: self.skipTest('HEVC NVENC not available')
        self.project.cuts = [Cut(0., 1), Cut(1.5, 1)]
        self.project.transitions = [Transition(1.5, 'fade', .4)]
        self.project.edit_points = []
        segment = next(s for s in plan_stack_segments(self.project) if s.start <= 1.5 < s.end)
        seen = []
        import quick_proxy
        original = quick_proxy._run_encode

        def capture(command, *args, **kwargs):
            seen.append(command)
            return original(command, *args, **kwargs)

        cpu_folder, gpu_folder = self.folder/'cpu-cache', self.folder/'gpu-cache'
        cpu_folder.mkdir(); gpu_folder.mkdir()
        with patch('cache_paths.cache_dir', return_value=cpu_folder):
            software = resolve_compound(segment, self.project, self.ffmpeg, self.ffprobe, 320, 180, deep=True)
        with patch('cache_paths.cache_dir', return_value=gpu_folder), \
                patch('quick_proxy._run_encode', side_effect=capture):
            hardware = resolve_compound(segment, self.project, self.ffmpeg, self.ffprobe, 320, 180,
                                        deep=True, caps=replace(caps, cuda_decode=False))
        self.assertTrue(any('hevc_nvenc' in command for command in seen))
        self.assertTrue(any('lossless' in command and 'constqp' in command for command in seen))

        def pixels(value):
            path = next(layer.path for layer in value.layers if layer.camera == 0)
            return self.command([self.ffmpeg, '-v', 'error', '-i', path, '-pix_fmt', 'yuv420p10le',
                                 '-f', 'rawvideo', 'pipe:1'])

        self.assertEqual(pixels(hardware), pixels(software))


if __name__ == '__main__': unittest.main()
