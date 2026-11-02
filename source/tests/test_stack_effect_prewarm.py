"""Bounded next-segment preparation and cancellation at effect boundaries."""
from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from multicam_core import MulticamProject
from video_stack_preview import PreviewCancelled, StackPreview
from video_stack_player import VideoStackPlayerMixin
from video_stack_render import StackSegment


class StackPrewarmTests(unittest.TestCase):
    def setUp(self):
        self.preview = StackPreview('ffmpeg', 'ffprobe', lambda *args: None, width=2, height=2)
        self.preview.update_clock(100.)
        self.cancel = threading.Event()
        self.events = []
        self.processes = []
        self.max_owned = 0
        self.read_hook = None
        self.prepare_hook = None
        self.publish_hook = None
        self.frame_counts = {}
        self.read_counts = {}
        self.published = []

    def tearDown(self):
        self.preview.close()

    def spawn(self, *args, **kwargs):
        fixture = self
        identity = len(self.processes)
        fixture.events.append(('spawn', identity))

        class Pipe(BytesIO):
            def read(self, count=-1):
                fixture.events.append(('read', identity))
                fixture.read_counts[identity] = fixture.read_counts.get(identity, 0)+1
                fixture.preview.update_clock(identity+(fixture.read_counts[identity]-1)/30.)
                fixture.max_owned = max(fixture.max_owned, len(fixture.preview._processes))
                if fixture.read_hook is not None: fixture.read_hook(identity)
                fixture.max_owned = max(fixture.max_owned, len(fixture.preview._processes))
                data = super().read(count)
                if not data: process.returncode = 0
                return data

        class Process:
            returncode = None
            stderr = None
            stdout = Pipe(bytes([identity % 255])*16*fixture.frame_counts.get(identity, 1))

            def poll(self): return self.returncode

            def terminate(self):
                fixture.events.append(('terminate', identity))
                self.returncode = -15

            def kill(self): self.returncode = -9

            def wait(self, timeout=None):
                fixture.events.append(('wait', identity))
                if self.returncode is None: self.returncode = 0
                return self.returncode

        process = Process()
        self.processes.append(process)
        return process

    def render(self, count=5, playing=True):
        self.preview.set_playing(playing)
        segments = [StackSegment(start=float(index), duration=1., frames=30, layers=())
                    for index in range(count)]
        project = MulticamProject(master_duration=float(count), fps=30.)
        job = (0, project, 0., playing, 1, None, False, None)
        def command(segment, *args, **kwargs):
            if self.prepare_hook is not None: self.prepare_hook(segment)
            self.events.append(('prepared', int(segment.start)))
            return ['ffmpeg', '-filter_complex', 'null', '-f', 'rawvideo', 'pipe:1'], 30.
        original_publish = self.preview._publish
        def publish(generation, payload):
            self.published.append(payload)
            self.events.append(('publish', payload['time']))
            if self.publish_hook is not None: self.publish_hook(payload)
            original_publish(generation, payload)
        with patch('video_stack_preview.preview_segments', return_value=iter(segments)), \
                patch('video_stack_preview.validate_stack_media'), \
                patch('video_stack_preview.preview_command', side_effect=command), \
                patch.object(self.preview, '_publish', side_effect=publish), \
                patch('video_stack_preview.subprocess.Popen', side_effect=self.spawn):
            self.preview._render(job, self.cancel)

    def wait_owned(self, count):
        deadline = time.monotonic()+2.
        while len(self.preview._processes) < count and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertEqual(len(self.preview._processes), count)

    def test_two_successors_are_ready_before_current_ends_with_three_process_bound(self):
        self.frame_counts[0] = 4
        def await_successors(identity):
            if identity == 0 and self.read_counts[0] == 2: self.wait_owned(3)
        self.read_hook = await_successors
        self.render(count=12)
        self.assertLess(self.events.index(('publish', 0.)), self.events.index(('spawn', 1)))
        self.assertLess(self.events.index(('spawn', 2)), self.events.index(('wait', 0)))
        for identity in range(3, 12):
            self.assertLess(self.events.index(('wait', identity-3)), self.events.index(('spawn', identity)))
        self.assertEqual(self.max_owned, 3)
        self.assertEqual(len(self.processes), 12)
        self.assertEqual(self.preview._processes, set())
        self.assertIsNone(self.preview._process)
        self.assertTrue(all(process.stdout.closed for process in self.processes))

    def test_slow_successor_preparation_does_not_block_current_frame_delivery(self):
        entered = threading.Event()
        release = threading.Event()
        self.frame_counts[0] = 5
        def prepare(segment):
            if segment.start == 1.:
                entered.set()
                if not release.wait(2.): raise AssertionError('Current frames blocked by successor preparation')
        def read(identity):
            if identity == 0 and self.read_counts[0] == 2:
                self.assertTrue(entered.wait(2.))
        def publish(payload):
            if payload['time'] == 2/30.:
                self.assertTrue(entered.is_set())
                self.assertNotIn(('prepared', 1), self.events)
                release.set()
        self.prepare_hook, self.read_hook, self.publish_hook = prepare, read, publish
        self.render(count=3)
        self.assertTrue(release.is_set())
        self.assertEqual([f['time'] for f in self.published[:5]], [i/30. for i in range(5)])
        self.assertLess(self.events.index(('publish', 2/30.)), self.events.index(('prepared', 1)))
        self.assertEqual(self.preview._processes, set())
        self.assertTrue(all(process.stdout.closed for process in self.processes))

    def test_paused_exact_frame_starts_no_speculative_process(self):
        self.render(count=1, playing=False)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.max_owned, 1)

    def test_navigation_cancellation_reaps_current_and_prepared_successor(self):
        self.frame_counts[0] = 3
        def cancel_on_read(identity):
            if identity == 0 and self.read_counts[0] == 2:
                self.wait_owned(3)
                self.cancel.set()
                with self.preview._condition: self.preview._terminate_locked()
        self.read_hook = cancel_on_read
        with self.assertRaises(PreviewCancelled): self.render(count=12)
        self.assertEqual(len(self.processes), 3)
        self.assertIn(('terminate', 0), self.events)
        self.assertIn(('terminate', 1), self.events)
        self.assertIn(('terminate', 2), self.events)
        self.assertEqual(self.preview._processes, set())
        self.assertTrue(all(process.stdout.closed for process in self.processes))

    def test_cancel_during_successor_preparation_prevents_late_decoder_start(self):
        entered = threading.Event()
        returned = threading.Event()
        self.frame_counts[0] = 3
        def prepare(segment):
            if segment.start == 1.:
                entered.set()
                if not self.cancel.wait(2.): raise AssertionError('Cancellation did not reach preparation')
                returned.set()
        def read(identity):
            if identity == 0 and self.read_counts[0] == 2:
                self.assertTrue(entered.wait(2.))
                self.cancel.set()
        self.prepare_hook, self.read_hook = prepare, read
        with self.assertRaises(PreviewCancelled): self.render(count=12)
        self.assertTrue(returned.is_set())
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.preview._processes, set())
        self.assertTrue(self.processes[0].stdout.closed)

    def test_failed_successor_startup_also_reaps_the_current_process(self):
        original_spawn = self.spawn
        def fail_second(*args, **kwargs):
            if self.processes: raise OSError('cannot start successor')
            return original_spawn(*args, **kwargs)
        self.spawn = fail_second
        with self.assertRaisesRegex(OSError, 'successor'): self.render(count=3)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.preview._processes, set())
        self.assertTrue(self.processes[0].stdout.closed)

    def test_mailbox_rechecks_decoder_clock_but_preserves_paused_frame(self):
        payload = {'time': 1., 'rgba': b'frame', 'fps': 30., 'playback_frame': True}
        self.preview.set_playing(True)
        self.preview.update_clock(1.)
        self.preview._publish(0, payload)
        self.preview.update_clock(1.2)
        self.assertIsNone(self.preview.take_frame())
        self.preview.set_playing(False)
        self.preview._publish(0, payload)
        frame = self.preview.take_frame()
        self.assertEqual(frame['rgba'], b'frame')
        self.assertEqual(frame['master_time'], 1.2)
        self.assertFalse(frame['playing'])


class StackPauseIntegrationTests(unittest.TestCase):
    def test_space_pause_resume_reuses_stream_but_navigation_and_revision_replace_it(self):
        class Preview:
            live = False
            def __init__(self): self.requests = []; self.cancelled = 0
            def update_clock(self, position): pass
            def set_playing(self, playing): return self.live
            def request(self, *args, **kwargs): self.requests.append((args, kwargs)); self.live = args[2]
            def cancel(self): self.cancelled += 1; self.live = False
        class Player(VideoStackPlayerMixin):
            def _stack_project(self): return 'project snapshot'
            def _emit(self, *args): pass
        player = Player()
        player._init_stack_preview()
        player._stack_enabled = True
        preview = player._stack_preview = Preview()
        player._position = 0.
        player._active_navigation_serial = player._request_id = 0
        player._subtitle_ass_path = None
        player._use_proxies = True
        player._playing = False
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 1)  # Initial exact paused frame.
        player._playing = True
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 2)  # Start continuous stream once.
        player._playing = False
        player._sync_stack_preview()
        player._playing = True
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 2)
        self.assertEqual(preview.cancelled, 0)
        player._active_navigation_serial += 1
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 3)
        player._stack_revision += 1
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 4)
        self.assertEqual(preview.cancelled, 1)


class StackPrewarmMediaTests(unittest.TestCase):
    def test_real_effect_boundaries_keep_export_pixels_and_bounded_processes(self):
        try:
            from .test_video_stack_render import StackMediaTests
        except ImportError:
            from test_video_stack_render import StackMediaTests
        from creative_core import ColorGrade
        from exporter_core import probe_media
        from program_edits import ClipGrade
        from video_stack_preview import preview_command, preview_segments

        StackMediaTests.setUpClass()
        preview = None
        try:
            fixture = StackMediaTests()
            project = fixture.project()
            project.fps = 30.
            project.clips[0].path = str(fixture.dynamic25)
            project.clip_grades = [ClipGrade(.4, .8, ColorGrade(exposure=.3)),
                                  ClipGrade(.8, 1.2, ColorGrade(exposure=-.2)),
                                  ClipGrade(1.2, 1.6, ColorGrade(exposure=.5))]
            media = {clip.path: probe_media(fixture.ffprobe, clip.path) for clip in project.clips}
            expected = b''.join(fixture.command(preview_command(segment, project, fixture.ffmpeg,
                media, 128, 72)[0]) for segment in preview_segments(project, 0.))
            events = []
            preview = StackPreview(fixture.ffmpeg, fixture.ffprobe,
                lambda *event: events.append(event), width=128, height=72)
            preview.request(project, 0., True, 1, use_proxies=False)
            frames = []
            peak = 0
            deadline = time.monotonic()+12.
            while len(frames) < 60 and time.monotonic() < deadline:
                with preview._condition: peak = max(peak, len(preview._processes))
                frame = preview.take_frame()
                if frame is not None:
                    frames.append(frame)
                    if len(frames) == 6:
                        self.assertTrue(preview.set_playing(False))
                        with preview._condition: retained = set(preview._processes)
                        time.sleep(1.)
                        with preview._condition: self.assertEqual(preview._processes, retained)
                        self.assertIsNone(preview.take_frame())
                        self.assertTrue(preview.set_playing(True))
                    # Drive a deterministic master clock, permitting exactly
                    # one output frame at a time; machine speed cannot skip it.
                    preview.update_clock(len(frames)/30.)
                time.sleep(.001)
            self.assertEqual(len(frames), 60, events)
            self.assertGreaterEqual(peak, 2)
            self.assertLessEqual(peak, 3)
            self.assertEqual(b''.join(frame['rgba'] for frame in frames), expected)
            for index, frame in enumerate(frames): self.assertAlmostEqual(frame['time'], index/30.)
            self.assertFalse([event for event in events if event[0] == 'error'], events)
            preview.close()
            self.assertEqual(preview._processes, set())
            self.assertFalse(preview._thread.is_alive())
        finally:
            if preview is not None: preview.close()
            StackMediaTests.tearDownClass()


if __name__ == '__main__':
    unittest.main(verbosity=2)
