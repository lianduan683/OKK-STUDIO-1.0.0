"""Programme framing, first-frame staging and independent GPU transition sides."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from clip_transform import ClipTransform, ClipTransformRange, framing_filter
from creative_core import Transition
from exporter_core import CREATE_NO_WINDOW, find_binary
from multicam_core import CameraClip, Cut, MulticamProject
import multicam_player as module
from multicam_player import MulticamPlayer, _Slot
from realtime_transform import RealtimeTransform, preview_dimensions
try:
    from .test_multicam_player import _Trace, _controller_type
    from .test_gpu_live_player import MovieBackend
except ImportError:
    from test_multicam_player import _Trace, _controller_type
    from test_gpu_live_player import MovieBackend


class RecordingController:
    def __init__(self):
        self.calls = []

    def command(self, command):
        self.calls.append(command)


class PreviewDimensionsTests(unittest.TestCase):
    def test_default_and_small_canvases_never_upscale_or_mutate_settings(self):
        self.assertEqual(preview_dimensions(SimpleNamespace()), (1280, 720))
        self.assertEqual(preview_dimensions(SimpleNamespace(project_settings={})), (1280, 720))
        for width, height in ((160, 90), (64, 96), (1088, 612), (1280, 720)):
            settings = dict(width=width, height=height)
            project = SimpleNamespace(project_settings=settings)
            self.assertEqual(preview_dimensions(project), (width, height))
            self.assertEqual(settings, dict(width=width, height=height))

    def test_8k_portrait_and_dci_preserve_exact_aspect_and_even_dimensions(self):
        for original, expected in (((7680, 4320), (1280, 720)),
                                   ((1920, 1080), (1280, 720)),
                                   ((1080, 1920), (396, 704)),
                                   ((2160, 3840), (396, 704)),
                                   ((4096, 2160), (1024, 540))):
            with self.subTest(original=original):
                project = SimpleNamespace(project_settings=dict(zip(('width', 'height'), original)))
                width, height = preview_dimensions(project)
                self.assertEqual((width, height), expected)
                self.assertEqual(width * original[1], height * original[0])
                self.assertEqual((width % 2, height % 2), (0, 0))
                self.assertLessEqual(width, 1280)
                self.assertLessEqual(height, 720)

    def test_unusual_ratios_retain_original_when_exact_even_cap_is_impossible(self):
        for original in ((7680, 4322), (2048, 1150)):
            with self.subTest(original=original):
                project = SimpleNamespace(project_settings=dict(zip(('width', 'height'), original)))
                self.assertEqual(preview_dimensions(project), original)


class RealtimeTransformTests(unittest.TestCase):
    def test_untouched_identity_and_bypass_never_issue_optional_commands(self):
        controller = SimpleNamespace(command=lambda command: self.fail(str(command)))
        engine = RealtimeTransform()
        self.assertFalse(engine.apply(controller, ClipTransform(), 1920, 1080))
        self.assertFalse(engine.apply(controller, ClipTransform(zoom=2), 1920, 1080, bypass=True))

    def test_updates_replace_one_label_and_cache_values_dimensions_and_controller(self):
        a, b, engine = RecordingController(), RecordingController(), RealtimeTransform()
        transform = ClipTransform('fill', 2, -.5, .25)
        self.assertTrue(engine.apply(a, transform, 1080, 1920))
        expected = f'{engine.INPUT_FILTER},@okk_framing:lavfi=[{framing_filter(transform, 1080, 1920)}]'
        self.assertEqual(a.calls, [['vf', 'add', expected]])
        self.assertFalse(engine.apply(a, transform, 1080, 1920))
        engine.apply(b, transform, 1080, 1920)
        self.assertEqual(a.calls, b.calls)
        transform.x = 1
        self.assertTrue(engine.apply(a, transform, 1080, 1920))
        self.assertTrue(engine.apply(a, transform, 1920, 1080))
        self.assertEqual(len(a.calls), 3)
        self.assertTrue(all(call[:2] == ['vf', 'add'] for call in a.calls))

    def test_identity_and_baked_preview_remove_only_installed_filter_once(self):
        controller, engine = RecordingController(), RealtimeTransform()
        transform = ClipTransform(zoom=2)
        engine.apply(controller, transform, 1920, 1080)
        self.assertTrue(engine.apply(controller, ClipTransform(), 1920, 1080))
        self.assertFalse(engine.apply(controller, ClipTransform(), 1920, 1080))
        engine.apply(controller, transform, 1920, 1080)
        engine.apply(controller, transform, 1920, 1080, bypass=True)
        self.assertFalse(engine.apply(controller, transform, 1920, 1080, bypass=True))
        self.assertEqual(controller.calls[1], ['vf', 'remove', engine.REMOVE])
        self.assertEqual(controller.calls[3], controller.calls[1])
        self.assertTrue(engine.apply(controller, transform, 1920, 1080))

    def test_failed_command_remains_retryable(self):
        controller, engine = RecordingController(), RealtimeTransform()
        transform = ClipTransform(zoom=2)
        with patch.object(controller, 'command', side_effect=RuntimeError('vf failed')):
            with self.assertRaises(RuntimeError):
                engine.apply(controller, transform, 1920, 1080)
        self.assertTrue(engine.apply(controller, transform, 1920, 1080))
        with patch.object(controller, 'command', side_effect=RuntimeError('remove failed')):
            with self.assertRaises(RuntimeError):
                engine.apply(controller, ClipTransform(), 1920, 1080)
        self.assertTrue(engine.apply(controller, ClipTransform(), 1920, 1080))

    def test_baked_keys_and_stack_do_not_receive_second_framing(self):
        player = MulticamPlayer.__new__(MulticamPlayer)
        player._project = SimpleNamespace(clip_transforms=[object()])
        player._transform_engine = RealtimeTransform()
        controller, slot = RecordingController(), _Slot(1)
        slot.controller = controller
        for key in ('render-preview', 'transition-preview', 'prerender'):
            player._transform_engine.apply(controller, ClipTransform(zoom=2), 1920, 1080)
            self.assertTrue(player._sync_program_transform(slot, 0, key=(key,)))
            self.assertEqual(controller.calls[-1], ['vf', 'remove', RealtimeTransform.REMOVE])
        player._stack_enabled = True
        before = len(controller.calls)
        self.assertFalse(player._sync_program_transform(slot, 0, key=('raw',)))
        self.assertEqual(len(controller.calls), before)


class PreviewFixture(unittest.TestCase):
    def setUp(self):
        self.trace = _Trace()
        trace, base = self.trace, _controller_type(self.trace)

        class Controller(base):
            framing = None

            def command(self, command, **kwargs):
                if command[:2] == ['vf', 'add']:
                    self.framing = command[2]
                elif command[:2] == ['vf', 'remove']:
                    self.framing = None
                return super().command(command, **kwargs)

            def load(self, path):
                trace.record('loaded_framing', self.window, self.framing)
                return super().load(path)

            def wait_for_frame(self, target, **kwargs):
                trace.record('frame_framing', self.window, self.framing)
                return self._position()

        self.controller_type = Controller
        self.patches = [patch.object(module, 'MpvController', Controller),
                        patch.object(module, '_AudioController', Controller),
                        patch.object(MulticamPlayer, 'TICK', .01)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.events, self.received = queue.Queue(), []
        self.project = MulticamProject(
            clips=[CameraClip('a', 1, 'a.mp4', 0, 8), CameraClip('b', 2, 'b.mp4', 0, 8)],
            cuts=[Cut(0, 1), Cut(4, 2)], fps=30)
        self.project.project_settings = dict(width=1080, height=1920, sample_rate=48000, bit_depth='24bit')
        self.player = None
        self.backends = []

    def tearDown(self):
        if self.player is not None:
            self.player.close()
            self.assertTrue(self.player.wait_closed(3))

    def start(self, *, double=False, gpu=False):
        def factory(host, runtime=None):
            backend = MovieBackend(self.trace, self.controller_type, host, runtime)
            self.backends.append(backend)
            return backend
        self.player = MulticamPlayer('mpv', {1: 101, 2: 102}, 200, 300, self.events,
            program_standby_window=201 if double or gpu else None,
            gpu_live=gpu, gpu_session_factory=factory)
        self.player.submit('configure', project=self.project)
        self.wait(lambda: self.player._program.active and all(slot.active for slot in self.player._slots.values()))

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            while not self.events.empty():
                kind, value = self.events.get_nowait()
                self.received.append((kind, value))
                if kind == 'program_swap':
                    self.player.submit('program_swap_ack', window=value)
            if predicate():
                return
            time.sleep(.005)
        self.fail('preview timed out: ' + str(self.received[-4:]))

    def graph(self, transform):
        width, height = preview_dimensions(self.project)
        return f'{RealtimeTransform.INPUT_FILTER},@okk_framing:lavfi=[{framing_filter(transform, width, height)}]'

    def vf_calls(self):
        return [(window, payload) for kind, window, payload, _ in self.trace.snapshot()
                if kind == 'command' and payload[0] == 'vf']


class MpvTransformPreviewTests(PreviewFixture):
    def test_8k_zoom_four_is_capped_before_first_source_load(self):
        crop = ClipTransform('fill', 4)
        self.project.project_settings.update(width=7680, height=4320)
        self.project.clip_transforms = [ClipTransformRange(0, 8, crop)]
        self.start()
        expected = f'{RealtimeTransform.INPUT_FILTER},@okk_framing:lavfi=[{framing_filter(crop, 1280, 720)}]'
        self.assertEqual(self.player._program.controller.framing, expected)
        self.assertIn(('loaded_framing', 200, expected),
                      [(kind, window, value) for kind, window, value, _ in self.trace.snapshot()])
        self.assertEqual(self.player._project.project_settings, self.project.project_settings)

    def test_empty_settings_fall_back_and_atomic_canvas_fill_change_updates_without_reload(self):
        crop = ClipTransform('fill')
        self.project.project_settings = {}
        self.project.clip_transforms = [ClipTransformRange(0, 8, crop)]
        self.start()
        expected = self.graph(crop)
        self.assertEqual(self.player._program.controller.framing, expected)
        loads = [call for call in self.trace.snapshot() if call[0] == 'load']
        self.project.project_settings = dict(width=1088, height=612, sample_rate=48000, bit_depth='24bit')
        self.player.submit('configure', project=self.project)
        expected = self.graph(crop)
        self.wait(lambda: self.player._program.controller.framing == expected)
        self.assertEqual(loads, [call for call in self.trace.snapshot() if call[0] == 'load'])

    def test_first_source_frame_is_framed_and_grid_is_independent(self):
        crop = ClipTransform('fill', 1.5, 1, -.5)
        self.project.clip_transforms = [ClipTransformRange(0, 8, crop)]
        self.start()
        self.assertEqual(self.vf_calls(), [(200, ('vf', 'add', self.graph(crop)))])
        loads = [(window, value) for kind, window, value, _ in self.trace.snapshot() if kind == 'loaded_framing']
        self.assertIn((200, self.graph(crop)), loads)
        self.assertTrue(all(value is None for window, value in loads if window in (101, 102)))

    def test_same_source_range_exit_and_reconfigure_reset_without_reload(self):
        self.project.cuts = [Cut(0, 1)]
        crop = ClipTransform(zoom=2)
        self.project.clip_transforms = [ClipTransformRange(0, 1, crop)]
        self.start()
        before_loads = [call for call in self.trace.snapshot() if call[0] == 'load']
        self.player.submit('seek', time=1)
        self.wait(lambda: (200, ('vf', 'remove', RealtimeTransform.REMOVE)) in self.vf_calls())
        self.player.submit('seek', time=.5)
        self.wait(lambda: len(self.vf_calls()) == 3)
        self.project.clip_transforms = []
        self.player.submit('configure', project=self.project)
        self.wait(lambda: len(self.vf_calls()) == 4)
        self.assertEqual(before_loads, [call for call in self.trace.snapshot() if call[0] == 'load'])
        before = len(self.received)
        self.wait(lambda: len(self.received) >= before + 4)
        self.assertEqual(len(self.vf_calls()), 4, 'filter commands repeated on idle ticks')

    def test_hidden_staging_uses_future_shot_framing_before_load_and_handoff(self):
        left, right = ClipTransform('fill', 2, -1), ClipTransform('fill', 2, 1)
        self.project.cuts = [Cut(0, 1), Cut(2, 2)]
        self.project.clip_transforms = [ClipTransformRange(0, 2, left), ClipTransformRange(2, 8, right)]
        self.start(double=True)
        self.wait(lambda: self.player._program_standby.prewarm_target is not None)
        self.assertEqual(self.player._program_standby.controller.framing, self.graph(right))
        self.player.submit('seek', time=2)
        self.wait(lambda: self.player._program.window == 201)
        self.assertEqual(self.player._program.controller.framing, self.graph(right))
        frames = [value for kind, window, value, _ in self.trace.snapshot()
                  if kind in ('loaded_framing', 'frame_framing') and window == 201]
        self.assertTrue(frames)
        self.assertTrue(all(value == self.graph(right) for value in frames))

    def test_baked_excerpt_resets_before_load_and_raw_restores(self):
        crop = ClipTransform(zoom=2)
        self.project.clip_transforms = [ClipTransformRange(0, 8, crop)]
        self.start()
        self.player.submit('render_preview', path='baked.mp4', start=0, end=1, play=False)
        self.wait(lambda: self.player._program.key[0] == 'render-preview')
        self.assertIsNone(self.player._program.controller.framing)
        loads = [value for kind, window, value, _ in self.trace.snapshot() if kind == 'loaded_framing' and window == 200]
        self.assertEqual(loads, [self.graph(crop), None])
        self.player.submit('seek', time=1)
        self.wait(lambda: self.player._program.key[0] != 'render-preview')
        self.assertEqual(self.player._program.controller.framing, self.graph(crop))


class GpuTransformPreviewTests(PreviewFixture):
    def test_8k_zoom_four_texture_uses_capped_graph(self):
        crop = ClipTransform('fill', 4)
        self.project.project_settings.update(width=7680, height=4320)
        self.project.clip_transforms = [ClipTransformRange(0, 8, crop)]
        self.start(gpu=True)
        expected = f'{RealtimeTransform.INPUT_FILTER},@okk_framing:lavfi=[{framing_filter(crop, 1280, 720)}]'
        self.assertEqual(self.backends[0].controllers['a'].framing, expected)
        self.assertEqual(self.player._project.project_settings, self.project.project_settings)

    def test_same_camera_transition_keeps_each_side_crop_through_cut(self):
        left, right = ClipTransform('fill', 2, -1), ClipTransform('fill', 2, 1)
        self.project.cuts = [Cut(0, 1)]
        self.project.edit_points = [4]
        self.project.transitions = [Transition(4, 'fade', 1)]
        self.project.clip_transforms = [ClipTransformRange(0, 4, left), ClipTransformRange(4, 8, right)]
        self.start(gpu=True)
        self.player.submit('seek', time=4.1)
        self.wait(lambda: self.player._gpu_context is not None and self.player._gpu_context['b'].active)
        backend = self.backends[0]
        self.assertEqual(backend.controllers['a'].framing, self.graph(left))
        self.assertEqual(backend.controllers['b'].framing, self.graph(right))
        # The new decoder must already have its side-specific filter at load.
        self.assertIn(('loaded_framing', 201, self.graph(right)),
                      [(kind, window, value) for kind, window, value, _ in self.trace.snapshot()])
        before = len(self.vf_calls())
        self.player.submit('seek', time=4.2)
        self.wait(lambda: self.player._position == 4.2 and any(
            call['op'] == 'compose' and call.get('active') and abs(call.get('progress', 0) - .7) < 1e-6
            for call in backend.calls))
        self.assertEqual(len(self.vf_calls()), before)
        self.player.submit('seek', time=4.6)
        self.wait(lambda: self.player._gpu_context is None and self.player._position == 4.6 and
                  backend.controllers[self.player._program.controller.slot].framing == self.graph(right))
        self.assertTrue(all(window in (200, 201) for window, _ in self.vf_calls()))

    def test_armed_outgoing_follows_current_range_until_transition_window(self):
        early, outgoing, incoming = ClipTransform(zoom=1.5), ClipTransform(zoom=2), ClipTransform(zoom=3)
        self.project.transitions = [Transition(4, 'fade', 1)]
        self.project.clip_transforms = [ClipTransformRange(0, 3, early),
            ClipTransformRange(3, 4, outgoing), ClipTransformRange(4, 8, incoming)]
        self.start(gpu=True)
        backend = self.backends[0]
        self.player.submit('seek', time=2)
        self.wait(lambda: self.player._gpu_context is not None and self.player._gpu_context['b'].active)
        self.assertEqual(backend.controllers['a'].framing, self.graph(early))
        self.assertEqual(backend.controllers['b'].framing, self.graph(incoming))
        self.player.submit('seek', time=3.8)
        self.wait(lambda: backend.controllers['a'].framing == self.graph(outgoing))
        self.assertEqual(backend.controllers['b'].framing, self.graph(incoming))


class FramingOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get('OKK_TEST_FFMPEG') or find_binary('ffmpeg', Path(__file__).resolve().parents[1])
        if not cls.ffmpeg:
            raise unittest.SkipTest('FFmpeg unavailable; set OKK_TEST_FFMPEG for pixel checks')

    def test_8k_zoom_four_preview_graph_bounds_expanded_frame_and_final_canvas(self):
        project = SimpleNamespace(project_settings=dict(width=7680, height=4320))
        controller = RecordingController()
        RealtimeTransform().apply(controller, ClipTransform('fill', 4), *preview_dimensions(project))
        graph = controller.calls[-1][2].split(':lavfi=[', 1)[1][:-1]
        # Inspect the actual intermediate after scale, then the final canvas.
        # A small input with the same 16:9 aspect avoids allocating an 8K
        # source while executing the exact preview scale/crop expressions.
        graph = graph.replace(',setsar=1,', ',showinfo,setsar=1,', 1) + ',showinfo'
        result = subprocess.run([self.ffmpeg, '-v', 'info', '-f', 'lavfi', '-i', 'color=red:s=160x90',
                                 '-vf', graph, '-frames:v', '1', '-f', 'null', '-'],
                                capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW)
        log = result.stderr.decode(errors='replace')
        self.assertEqual(result.returncode, 0, log)
        self.assertRegex(log, r's:5120x2880\b')
        self.assertRegex(log, r's:1280x720\b')

    def test_real_gpu_textures_are_framed_before_transition_composition(self):
        runtime = os.environ.get('OKK_TEST_GPU_RUNTIME')
        if not runtime:
            self.skipTest('set OKK_TEST_GPU_RUNTIME for offscreen D3D11 pixel checks')
        from gpu_live_controller import GpuLiveSession, TextureController
        from gpu_live_session import GpuLiveSession as NativeSession
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'source.mp4')
            subprocess.run([self.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                            'color=red:s=160x90:d=2,drawbox=x=80:y=0:w=80:h=90:color=blue:t=fill',
                            '-pix_fmt', 'yuv420p', '-y', path], capture_output=True, check=True,
                           timeout=20, creationflags=CREATE_NO_WINDOW)
            session = GpuLiveSession(0, runtime=runtime, session_factory=lambda host, runtime=None:
                NativeSession(host, runtime=runtime, width=64, height=96, diagnostics=True))
            a, b = TextureController(session, 'a', 0), TextureController(session, 'b', 0)
            try:
                a.start()
                b.start()
                engine = RealtimeTransform()
                for controller, x in ((a, -1), (b, 1)):
                    engine.apply(controller, ClipTransform('fill', 2, x), 64, 96)
                    controller.load(path)
                    controller.command(['seek', .2, 'absolute+exact'])
                    controller.wait_for_frame(.2)
                    self.assertEqual(session.frame_state(controller.slot)['hwdec'], 'd3d11va')

                def capture(name):
                    file = Path(folder) / (name + '.bmp')
                    session.screenshot(str(file))
                    # The diagnostic BMP has a 54-byte header and top-down BGRA.
                    center = 54 + ((96 // 2) * 64 + 64 // 2) * 4
                    blue, green, red, _ = file.read_bytes()[center:center + 4]
                    return red, green, blue

                session.compose(front_slot='a')
                left = capture('left')
                session.compose(front_slot='b')
                right = capture('right')
                session.compose(front_slot='a', other_slot='b', kind='fade', progress=.5, active=True)
                mixed = capture('mixed')
                self.assertGreater(left[0], 200, str((left, right, mixed, session.frame_state('a').get('video-out-params'))))
                self.assertLess(left[2], 20)
                self.assertGreater(right[2], 200)
                self.assertLess(right[0], 20)
                self.assertGreater(mixed[0], 80)
                self.assertGreater(mixed[2], 80)
                # Removing and reinstalling on an already loaded hardware
                # decoder must update the texture before another handoff.
                engine.apply(a, ClipTransform(), 64, 96)
                a.command(['seek', .4, 'absolute+exact'])
                a.wait_for_frame(.4)
                self.assertEqual(a.command(['get_property', 'vf']), [])
                engine.apply(a, ClipTransform('fill', 2, -1), 64, 96)
                a.command(['seek', .5, 'absolute+exact'])
                a.wait_for_frame(.5)
                session.compose(front_slot='a')
                restored = capture('restored')
                self.assertGreater(restored[0], 200)
                self.assertLess(restored[2], 20)
            finally:
                try:
                    a.stop()
                finally:
                    try:
                        b.stop()
                    finally:
                        session.close()

    def test_real_mpv_accepts_idle_filter_replaces_label_and_resets_before_next_file(self):
        mpv = os.environ.get('OKK_TEST_MPV') or find_binary('mpv', Path(__file__).resolve().parents[1])
        if not mpv:
            self.skipTest('MPV unavailable; set OKK_TEST_MPV for IPC checks')
        from mpv_controller import MpvController
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'source.mp4')
            subprocess.run([self.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=160x90:d=2',
                            '-pix_fmt', 'yuv420p', '-y', path], capture_output=True, check=True,
                           timeout=20, creationflags=CREATE_NO_WINDOW)
            controller = MpvController(mpv, 0)
            controller.process = subprocess.Popen(
                [mpv, '--no-config', '--vo=null', '--ao=null', '--hwdec=no', '--pause', '--idle=yes',
                 '--terminal=no', '--input-ipc-server=' + controller.pipe_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
            process = controller.process
            try:
                controller.command(['get_property', 'mpv-version'])
                controller.command(['vf', 'add', '@keep:lavfi=[hflip]'])
                engine = RealtimeTransform()
                engine.apply(controller, ClipTransform('fill', 2, -1), 64, 96)
                controller.command(['loadfile', path, 'replace'])

                def await_dimensions(width, height):
                    deadline = time.monotonic() + 5
                    actual = None
                    while time.monotonic() < deadline:
                        try:
                            actual = controller.command(['get_property', 'video-out-params'])
                        except RuntimeError:
                            pass  # The source has not loaded yet.
                        if actual and (actual.get('w'), actual.get('h')) == (width, height):
                            return
                        time.sleep(.01)
                    self.fail(f'MPV output dimensions: {actual}')

                await_dimensions(64, 96)
                engine.apply(controller, ClipTransform('fill', 2, 1), 64, 96)
                filters = controller.command(['get_property', 'vf'])
                self.assertEqual([item['label'] for item in filters], ['keep', 'okk_framing_input', 'okk_framing'])
                engine.apply(controller, ClipTransform(), 64, 96)
                controller.command(['loadfile', path, 'replace'])
                await_dimensions(160, 90)
                filters = controller.command(['get_property', 'vf'])
                self.assertEqual([item['label'] for item in filters], ['keep'])
            finally:
                controller.stop()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def test_live_filter_produces_timeline_dimensions_and_distinct_crop_pixels(self):
        # Red/blue side-by-side input: opposite crop positions must select the
        # corresponding half. Test the exact graph sent through MPV lavfi.
        pixels = []
        for x in (-1, 1):
            controller = RecordingController()
            RealtimeTransform().apply(controller, ClipTransform('fill', 2, x), 64, 96)
            graph = controller.calls[-1][2].split(':lavfi=[', 1)[1][:-1]
            command = [self.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                       'color=red:s=160x90,drawbox=x=80:y=0:w=80:h=90:color=blue:t=fill',
                       '-vf', graph, '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-']
            result = subprocess.run(command, capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertEqual(len(result.stdout), 64 * 96 * 3)
            center = ((96 // 2) * 64 + 64 // 2) * 3
            pixels.append(tuple(result.stdout[center:center + 3]))
        self.assertGreater(pixels[0][0], 200)
        self.assertLess(pixels[0][2], 20)
        self.assertGreater(pixels[1][2], 200)
        self.assertLess(pixels[1][0], 20)


if __name__ == '__main__':
    unittest.main()
