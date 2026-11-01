"""Colour navigation follows cuts without changing edits or playing the grid."""
import copy
from pathlib import Path
import tempfile
import unittest

from . import test_realtime_grade_ui as ui
from . import test_multicam_player as playback
from . import test_gpu_live_player as gpu
from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import grade_at, set_grade_on_range


class ColorPlayheadFollowTests(unittest.TestCase):
    setUp = ui.RealtimeGradeWorkspaceTests.setUp
    tearDown = ui.RealtimeGradeWorkspaceTests.tearDown

    def test_selected_clip_preview_follows_program_instead_of_pinning_camera(self):
        self.workspace._audition_grade_target()
        self.player.submit.assert_any_call('audition', camera=None)

    def test_seek_and_frame_steps_follow_both_directions_without_editing(self):
        ws = self.workspace
        before = copy.deepcopy(ws.project)
        ws.seek(7.)
        self.assertEqual(ws.selected_program_range, (7., 12.))
        self.assertIn('机位 1', ws.color_workspace.scope_var.get())
        self.assertEqual(ws.selected_cut, 2)
        ws.step_frame(-1)
        self.assertEqual(ws.selected_program_range, (3., 7.))
        self.assertIn('机位 2', ws.color_workspace.scope_var.get())
        ws.seek(0.)
        self.assertEqual(ws.selected_program_range, (0., 3.))
        ws.seek(12.)
        self.assertEqual(ws.selected_program_range, (7., 12.))
        self.assertEqual(ws.project, before)
        self.assertEqual(ws.undo_stack, [])
        self.assertFalse(ws.dirty)

    def test_playback_state_follows_cut_without_pausing_or_seeking(self):
        ws = self.workspace
        for position, camera, selected in ((7., 1, (7., 12.)), (2., 1, (0., 3.)), (3., 2, (3., 7.))):
            self.player.reset_mock()
            ws.player_queue.put(('state', dict(time=position, playing=True, camera=camera, request_id=ws._transport_request_id)))
            ws._poll_player(reschedule=False)
            self.assertTrue(ws.playing)
            self.assertEqual(ws.selected_program_range, selected)
            self.assertFalse(any(c.args[0] in ('pause', 'seek', 'scrub', 'configure', 'audition')
                                 for c in self.player.submit.call_args_list))

    def test_new_clip_loads_its_grade_and_discards_pending_old_draft(self):
        ws = self.workspace
        set_grade_on_range(ws.project, 7., 12., ColorGrade(exposure=.8))
        ws.color_workspace.refresh()
        ws.preview_active_grade(ColorGrade(exposure=-.7))
        self.assertIsNotNone(ws._grade_preview_pending)
        ws.seek(7.)
        self.assertIsNone(ws._grade_preview_pending)
        self.assertEqual(ws.get_active_grade().exposure, .8)
        ws.apply_active_grade(ColorGrade(exposure=.4))
        self.assertEqual(grade_at(ws.project, 8.).exposure, .4)
        self.assertIsNone(grade_at(ws.project, 4.))

    def test_manual_multi_cut_range_and_explicit_camera_scope_are_preserved(self):
        ws = self.workspace
        ws.select_program_range(0., 8.)
        ws.seek(3.)
        self.assertEqual(ws.selected_program_range, (0., 8.))
        self.player.submit.assert_any_call('audition', camera=None)
        ws.set_grade_target(2)
        ws.seek(9.)
        self.assertEqual(ws.grade_target, 2)
        self.player.submit.assert_any_call('audition', camera=2)
        ws.exit_color_workspace()
        ws.seek(10.)
        self.assertIsNone(ws.selected_program_range)

    def test_same_camera_split_is_a_new_grade_target_at_fractional_frame_rate(self):
        ws = self.workspace
        ws.project.fps = 30000/1001
        boundary = 150/ws.project.fps
        ws.project.cuts = [Cut(0., 1)]
        ws.project.edit_points = [boundary]
        ws.selected_program_range = (0., boundary)
        ws.seek(boundary-1/ws.project.fps)
        self.assertEqual(ws.selected_program_range, (0., boundary))
        ws.step_frame(1)
        self.assertAlmostEqual(ws.selected_program_range[0], boundary)
        self.assertEqual(ws.project.camera_at(ws.current), 1)


class ColorCutPlayerTests(unittest.TestCase):
    setUp = playback.MulticamPlayerTests.setUp
    tearDown = playback.MulticamPlayerTests.tearDown
    _wait = playback.MulticamPlayerTests._wait
    _state = playback.MulticamPlayerTests._state
    _configure = playback.MulticamPlayerTests._configure

    def test_playback_crosses_cuts_using_original_before_ui_policy_catches_up(self):
        with tempfile.TemporaryDirectory() as folder:
            clips = []
            for camera in (1, 2):
                proxy = Path(folder)/f'proxy-{camera}.mp4'; proxy.write_bytes(b'proxy')
                clips.append(CameraClip(str(camera), camera, f'original-{camera}.mp4', 0., 2., proxy_path=str(proxy)))
            project = MulticamProject(clips, [Cut(0., 1), Cut(.2, 2), Cut(1., 1)], fps=30.)
            self._configure(project, color_monitoring=True, original_cameras=(1,), use_proxies=True)
            self._wait(lambda: self.player._program.controller is not None and self.player._program.controller.path == 'original-1.mp4')
            self.player.submit('play')
            self._state(lambda s: .2 <= s['time'] < 1. and s['camera'] == 2 and s['playing'])
            self.assertEqual(self.player._program.controller.path, 'original-2.mp4')
            self.assertTrue(all(not slot.active for slot in self.player._slots.values()))
            self.player.submit('scrub', time=.1, pause=True, request_id=1)
            self._state(lambda s: s['request_id'] == 1 and s['camera'] == 1 and not s['playing'])
            self.assertEqual(self.player._program.controller.path, 'original-1.mp4')
            self.player.submit('color_monitoring', enabled=False)
            self.player.submit('original_cameras', cameras=())
            self._wait(lambda: all(slot.active and 'proxy-' in slot.controller.path for slot in self.player._slots.values()))
            self.assertTrue(self.player._use_proxies)


class ColorGpuCutTests(unittest.TestCase):
    setUp = gpu.GpuLiveWorkerTests.setUp
    tearDown = gpu.GpuLiveWorkerTests.tearDown
    wait = gpu.GpuLiveWorkerTests.wait
    drain = gpu.GpuLiveWorkerTests.drain

    def test_color_crosses_transition_as_single_original_picture(self):
        self.player.submit('color_monitoring', enabled=True)
        self.player.submit('audition', camera=None)
        self.player.submit('scrub', time=.7, pause=True, request_id=1)
        self.wait(lambda: self.player._request_id == 1 and self.player._gpu_context is None)
        backend = self.backends[0]
        visible = backend.controllers[self.player._program.controller.slot]
        self.assertEqual(visible.path, 'camera-b.mp4')
        hidden = backend.controllers[self.player._program_standby.controller.slot]
        self.assertTrue(hidden.paused)
        self.assertTrue(all(not slot.active for slot in self.player._slots.values()))
        self.player.submit('scrub', time=.1, pause=True, request_id=2)
        self.wait(lambda: self.player._request_id == 2 and self.player._gpu_context is None and
                  backend.controllers[self.player._program.controller.slot].path == 'camera-a.mp4')


if __name__ == '__main__': unittest.main()
