"""Only grading/reference cameras bypass proxies, scoped to the colour page."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from . import test_delivery_ui as fixture
from . import test_realtime_grade_ui as embedded
from . import test_multicam_player as playback
from . import test_gpu_live_player as gpu
from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_proxy import _stamp


class ColorOriginalPageTests(unittest.TestCase):
    setUp = fixture.DeliveryTkTests.setUp
    tearDown = fixture.DeliveryTkTests.tearDown
    _create_app = fixture.DeliveryTkTests._create_app
    _destroy_app = fixture.DeliveryTkTests._destroy_app

    def _settle(self):
        self.app.attributes('-alpha', 0)
        self.app.geometry('1280x820+10000+10000')
        fixture.DeliveryTkTests._settle(self)

    def test_colour_and_reference_targets_do_not_change_editing_proxy_preference(self):
        ws = self.workspace
        ws.project = MulticamProject([CameraClip(str(i), i, f'original-{i}.mp4', 0, 6,
            proxy_path=f'proxy-{i}.mp4') for i in (1, 2, 3)], [Cut(0, 1), Cut(3, 2)])
        ws.player = Mock()
        for preference in (True, False):
            for destination in ('multicam', 'audio', 'delivery'):
                with self.subTest(preference=preference, destination=destination):
                    self.app._select_page('multicam')
                    ws.proxy_var.set(preference)
                    ws.project.color_reference_camera = 3
                    ws.selected_program_range = (0., 3.)
                    self.app._select_page('color')
                    self._settle()
                    self.assertEqual(ws._color_original_cameras(), (1, 3))
                    self.assertEqual(ws.proxy_var.get(), preference)
                    self.assertIn('原片', ws.finishing_workspace.summary_var.get())
                    ws._configure_player()
                    configurations = [c for c in ws.player.submit.call_args_list if c.args[0] == 'configure']
                    self.assertEqual(configurations[-1].kwargs['use_proxies'], preference)
                    self.assertEqual(configurations[-1].kwargs['original_cameras'], (1, 3))
                    self.assertTrue(configurations[-1].kwargs['color_monitoring'])
                    ws.set_grade_target(2)
                    self.assertEqual(ws._color_original_cameras(), (2, 3))
                    ws.set_color_reference_camera(1)
                    self.assertEqual(ws.player.submit.call_args.kwargs['cameras'], (1, 2))
                    self.app._select_page(destination)
                    self._settle()
                    policies = [c for c in ws.player.submit.call_args_list if c.args[0] == 'original_cameras']
                    self.assertEqual(policies[-1].kwargs['cameras'], ())
                    monitoring = [c for c in ws.player.submit.call_args_list if c.args[0] == 'color_monitoring']
                    self.assertFalse(monitoring[-1].kwargs['enabled'])
                    self.assertEqual(ws.proxy_var.get(), preference)
                    self.assertEqual(ws.project.clips[0].proxy_path, 'proxy-1.mp4')
                    ws.player.reset_mock()


class EmbeddedOriginalTests(unittest.TestCase):
    setUp = embedded.RealtimeGradeWorkspaceTests.setUp
    tearDown = embedded.RealtimeGradeWorkspaceTests.tearDown

    def test_selection_reference_and_full_project_targets_update_without_global_proxy_toggle(self):
        ws = self.workspace
        self.assertTrue(ws._color_mode)
        self.assertTrue(ws.proxy_var.get())
        self.assertEqual(ws._color_original_cameras(), (2,))
        ws.set_color_reference_camera(1)
        self.assertEqual(ws._color_original_cameras(), (1, 2))
        ws.select_program_range(0, 3)
        self.assertEqual(ws._color_original_cameras(), (1,))
        ws.select_program_range(0, 8)
        self.assertEqual(ws._color_original_cameras(), (1, 2))
        ws.set_color_reference_camera(None)
        ws.set_grade_target(None)
        self.assertEqual(ws._color_original_cameras(), (1, 2))
        ws.exit_color_workspace()
        self.assertTrue(ws.proxy_var.get())
        self.assertEqual(ws._color_original_cameras(), ())
        ws.enter_color_workspace()
        self.assertTrue(ws.proxy_var.get())

    def test_background_proxy_completion_preserves_camera_policy(self):
        ws = self.workspace
        ws.set_color_reference_camera(1)
        clip = ws.project.clips[0]
        proxy = self.folder / 'proxy.mp4'
        proxy.write_bytes(b'proxy')
        request = dict(source=clip.path, stamp=_stamp(clip.path), clips=[(clip.id, '')])
        ws._quick_proxy_token += 1
        ws._handle_quick_proxy_job('quick_proxy_ready', (ws._quick_proxy_token, request, str(proxy)))
        self.assertEqual(clip.proxy_path, str(proxy))
        self.assertTrue(self.player.submit.call_args.kwargs['use_proxies'])
        self.assertEqual(ws._color_original_cameras(), (1, 2))
        with patch('external_proxy_link.validate_existing_proxy', return_value=None):
            self.assertTrue(ws._start_proxy_link(pairs=[]))
            ws._proxy_link_thread.join(3)
        self.assertTrue(ws.proxy_var.get())
        self.assertEqual(ws._color_original_cameras(), (1, 2))


class OriginalDecoderTests(unittest.TestCase):
    setUp = playback.MulticamPlayerTests.setUp
    tearDown = playback.MulticamPlayerTests.tearDown
    _wait = playback.MulticamPlayerTests._wait
    _state = playback.MulticamPlayerTests._state
    _configure = playback.MulticamPlayerTests._configure

    def test_three_cameras_grade_reference_original_other_proxy_and_editing_all_proxies(self):
        with tempfile.TemporaryDirectory() as directory:
            proxies = []
            for i in (1, 2, 3):
                proxy = Path(directory) / f'proxy-{i}.mp4'
                proxy.write_bytes(b'proxy')
                proxies.append(str(proxy))
            project = MulticamProject([CameraClip(str(i), i, f'original-{i}.mp4', 0, 10,
                proxy_path=proxies[i-1]) for i in (1, 2, 3)], [Cut(0, 1)])
            self.player.submit('camera_windows', windows={1: 101, 2: 102, 3: 103})
            self._configure(project, use_proxies=True)
            def paths(expected):
                return all(self.player._slots[i].controller is not None and
                    self.player._slots[i].controller.path == path for i, path in enumerate(expected, 1))
            self._wait(lambda: paths(proxies))
            self.player.submit('original_cameras', cameras=(1, 2))
            self._wait(lambda: paths(['original-1.mp4', 'original-2.mp4', proxies[2]]))
            self.assertEqual(self.player._program.controller.path, 'original-1.mp4')
            self.player.submit('grade_preview', target=('camera', 1), grade=ColorGrade(exposure=.2))
            self._wait(lambda: self.player._grade_draft is not None)
            draft = copy.deepcopy(self.player._grade_draft)
            self.player.submit('proxy_update', bindings=[], use_proxies=True)
            self.player.submit('original_cameras', cameras=(2, 3))
            self._wait(lambda: paths([proxies[0], 'original-2.mp4', 'original-3.mp4']))
            self.assertEqual(self.player._grade_draft, draft)
            self.assertTrue(self.player._use_proxies)
            self.player.submit('original_cameras', cameras=())
            self._wait(lambda: paths(proxies))
            self._configure(project, use_proxies=True, original_cameras=(1, 3))
            self._wait(lambda: paths(['original-1.mp4', proxies[1], 'original-3.mp4']))

    def test_stack_preview_receives_per_camera_policy(self):
        class Preview:
            def __init__(self): self.requests = []
            def cancel(self): pass
            def close(self): pass
            def update_clock(self, position): pass
            def set_playing(self, playing): return True
            def request(self, *args, **kwargs): self.requests.append(kwargs)
        self._wait(lambda: hasattr(self.player, '_stack_preview'))
        preview = Preview()
        self.player._stack_preview = preview
        self.player.submit('camera_windows', windows={})
        project = MulticamProject([CameraClip('1', 1, 'original.mp4', 0, 10)], [Cut(0, 1)])
        project.video_stack_enabled = True
        self._configure(project, use_proxies=True, original_cameras=(1,))
        self._wait(lambda: bool(preview.requests))
        self.assertTrue(preview.requests[-1]['use_proxies'])
        self.assertEqual(preview.requests[-1]['original_cameras'], {1})
        self.player.submit('original_cameras', cameras=())
        self._wait(lambda: not preview.requests[-1].get('original_cameras', ()))


class NativeGpuOriginalTests(unittest.TestCase):
    setUp = gpu.GpuLiveWorkerTests.setUp
    tearDown = gpu.GpuLiveWorkerTests.tearDown
    wait = gpu.GpuLiveWorkerTests.wait
    drain = gpu.GpuLiveWorkerTests.drain

    def test_transition_sides_apply_original_policy_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            bindings = []
            for clip in self.project.clips:
                proxy = Path(directory) / (clip.id + '.mp4')
                proxy.write_bytes(b'proxy')
                bindings.append(dict(id=clip.id, source=clip.path, proxy=str(proxy)))
            self.player.submit('proxy_update', bindings=bindings, use_proxies=True)
            self.player.submit('seek', time=.55)
            self.wait(lambda: self.player._gpu_context is not None)
            self.player.submit('original_cameras', cameras=(1,))
            self.wait(lambda: self.player._gpu_context is not None and
                self.player._gpu_context['plan'].outgoing.path == self.project.clips[0].path and
                self.player._gpu_context['plan'].incoming.path == bindings[1]['proxy'])
            self.player.submit('original_cameras', cameras=(1, 2))
            self.wait(lambda: self.player._gpu_context['plan'].incoming.path == self.project.clips[1].path)
            self.player.submit('original_cameras', cameras=())
            self.wait(lambda: self.player._gpu_context['plan'].outgoing.path == bindings[0]['proxy'])


if __name__ == '__main__':
    unittest.main()
