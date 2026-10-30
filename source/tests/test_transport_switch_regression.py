"""User regressions: navigation before startup, space, and monitor cuts.

Tk events are dispatched only into isolated off-screen test widgets. Native
input is never injected into the user's running application.
"""
from __future__ import annotations

import queue
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from multicam_core import CameraClip, Cut, MulticamProject
from multicam_player import MulticamPlayer
import multicam_player as player_module
try:
    from . import test_timeline_drag_merge as fixture
    from .test_multicam_player import _Trace, _controller_type
    from .test_transport_controls import RecordingPlayer
except ImportError:
    import test_timeline_drag_merge as fixture
    from test_multicam_player import _Trace, _controller_type
    from test_transport_controls import RecordingPlayer


class EditorTransportRegressions(unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)

    def tearDown(self):
        fixture.TimelineDropTests.tearDown(self)

    def test_navigation_before_first_play_preserves_state_acknowledgement(self):
        ws = self.workspace
        self.assertIsNone(ws.player)
        ws.seek(4.)
        ws.seek(6.)
        request = ws._transport_request_id
        self.assertGreater(request, 0)
        player = RecordingPlayer()
        self.root.mpv_path = 'fixture-mpv.exe'
        with patch('multicam_ui.MulticamPlayer', return_value=player):
            ws.toggle()
        seeks = [values for action, values in player.commands if action == 'seek']
        self.assertEqual(seeks[-1]['time'], 6.)
        self.assertEqual(seeks[-1]['request_id'], request)
        self.assertEqual(player.commands[-1][0], 'toggle')
        # This was discarded indefinitely before startup carried the fence.
        ws.player_queue.put(('state', dict(time=6.5, playing=True,
                                          camera=1, request_id=request)))
        ws._poll_player(reschedule=False)
        self.assertTrue(ws.playing)
        self.assertEqual(ws.current, 6.5)
        ws.toggle()
        self.assertEqual(player.commands[-1][0], 'toggle')
        self.assertFalse(self.errors)

    def test_monitor_picture_click_creates_a_cut_in_normal_multicam_mode(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        ws.current = 3.
        self.assertFalse(ws._color_mode)
        self.assertFalse(ws.project.video_stack_enabled)
        ws.camera_surfaces[2].event_generate('<Button-1>', x=5, y=5, when='now')
        self.assertEqual(ws.project.camera_at(3.), 2)
        self.assertEqual(ws.project.camera_at(2.), 1)
        self.assertEqual(len(ws.undo_stack), 1)
        self.assertEqual(ws._displayed_camera, 2)
        self.assertFalse(self.errors)

    def test_space_on_camera_button_does_not_invoke_that_button(self):
        ws, button = self.workspace, self.workspace.camera_buttons[2]
        ws.player = RecordingPlayer()
        ws.current = 3.
        event = '<<EditorSpaceBindingRegression>>'
        transport = button.bind_class(ws._transport_tag, '<KeyPress-space>')
        native = button.bind_class('Button', '<KeyPress-space>')
        self.assertTrue(transport)
        self.assertTrue(native)
        scripts = {ws._transport_tag: transport, 'Button': native}
        for tag, script in scripts.items():
            button.bind_class(tag, event,
                              script.replace('%K', 'space').replace('%A', '{ }').replace('%s', '0'))
        try:
            button.event_generate(event, when='now')
            self.assertEqual(ws.player.commands[-1][0], 'toggle')
            self.assertEqual(ws.project.camera_at(3.), 1)
            for _ in range(5):
                button.event_generate(event, when='now')
            self.assertEqual(sum(action == 'toggle' for action, _ in ws.player.commands), 1)
            ws._transport_key_release(SimpleNamespace(keysym='space'))
            button.event_generate(event, when='now')
            self.assertEqual(sum(action == 'toggle' for action, _ in ws.player.commands), 2)
        finally:
            for tag in scripts:
                button.unbind_class(tag, event)
        self.assertFalse(self.errors)

    def test_existing_playback_can_be_paused_while_an_independent_job_is_busy(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        ws.playing = True
        ws.busy = True
        self.root.working = True
        ws.toggle()
        self.assertEqual(ws.player.commands, [('toggle', {})])
        ws.busy = False
        self.root.working = False

    def test_input_fields_keep_space_for_text_entry(self):
        import tkinter as tk
        ws = self.workspace
        ws.player = RecordingPlayer()
        field = tk.Entry(ws)
        try:
            ws._key(SimpleNamespace(widget=field, state=0, char=' ', keysym='space'))
            self.assertEqual(ws.player.commands, [])
        finally:
            field.destroy()


class WorkerTransportRegressions(unittest.TestCase):
    def setUp(self):
        self.trace = _Trace()
        controller = _controller_type(self.trace)
        self.patches = [patch.object(player_module, 'MpvController', controller),
                        patch.object(player_module, '_AudioController', controller),
                        patch.object(MulticamPlayer, 'TICK', .005)]
        for item in self.patches:
            item.start()
        self.events = queue.Queue()
        self.player = MulticamPlayer('mpv', {1: 101}, 200, 300, self.events)
        project = MulticamProject(clips=[CameraClip('one', 1, 'one.mp4', 0., 20.)],
                                  cuts=[Cut(0., 1)], master_audio='master.wav', master_duration=20.)
        self.player.submit('configure', project=project)
        self.wait_state(lambda state: state['ready'])

    def tearDown(self):
        self.player.close()
        self.assertTrue(self.player.wait_closed(3.))
        for item in reversed(self.patches):
            item.stop()

    def wait_state(self, predicate):
        deadline = time.monotonic() + 3.
        errors = []
        while time.monotonic() < deadline:
            try:
                kind, payload = self.events.get(timeout=.05)
            except queue.Empty:
                continue
            if kind == 'error':
                errors.append(payload)
            if kind == 'state' and predicate(payload):
                self.assertFalse(errors)
                return payload
        self.fail(f'No acknowledged transport state; errors={errors}')

    def test_startup_seek_fence_and_repeated_toggles_follow_worker_state(self):
        self.player.submit('seek', time=4., request_id=7)
        self.player.submit('toggle')
        state = self.wait_state(lambda item: item['request_id'] == 7 and item['playing'])
        self.assertGreaterEqual(state['time'], 4.)
        self.player.submit('toggle')
        stopped = self.wait_state(lambda item: item['request_id'] == 7 and not item['playing'])
        self.assertGreaterEqual(stopped['time'], 4.)
        # Two rapid presses are resolved in order even before UI polls an ack.
        self.player.submit('toggle')
        self.player.submit('toggle')
        self.player.submit('seek', time=5., request_id=8)
        state = self.wait_state(lambda item: item['request_id'] == 8)
        self.assertFalse(state['playing'])
        self.assertAlmostEqual(state['time'], 5., places=3)


if __name__ == '__main__':
    unittest.main()
