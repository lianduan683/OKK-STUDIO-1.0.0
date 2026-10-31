"""Wheel modifier routing and playhead-anchored navigation on real Tk canvases."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from editor_core import format_time
from multicam_core import CameraClip, Cut, MulticamProject
from . import test_timeline_drag_merge as fixture
from .test_transport_controls import RecordingPlayer


class TimelineWheelTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.TimelineDropTests()
        self.fixture.setUp()
        self.ws = self.fixture.workspace
        self.root = self.fixture.root

    def tearDown(self):
        self.fixture.tearDown()

    def flush(self):
        self.root.after(35, self.root.quit)
        self.root.mainloop()
        self.assertEqual(self.fixture.errors, [])

    def dense(self):
        self.ws.project = MulticamProject(
            clips=[CameraClip(str(i), i, f'camera-{i}.mp4', 0., 3600.)
                   for i in range(1, 37)], cuts=[Cut(0., 1)])
        self.ws.zoom = 50
        self.ws.draw_timeline()
        self.ws.timeline.xview_moveto(.3)
        self.ws.timeline.yview_moveto(.4)
        self.flush()

    def wheel(self, delta, modifier=''):
        self.ws.timeline.event_generate('<'+modifier+'MouseWheel>', x=230, y=45, delta=delta)

    def head_x(self):
        return self.ws._timeline_x(self.ws.current)-self.ws.timeline.canvasx(0)

    def test_plain_wheel_pans_horizontally_without_moving_playhead_or_tracks(self):
        self.dense()
        ws = self.ws
        view, vertical = ws.timeline.xview(), ws.timeline.yview()
        left = ws.timeline.canvasx(0)
        before = copy.deepcopy(ws.project)
        self.wheel(-120)
        self.assertGreater(ws.timeline.xview()[0], view[0])
        self.assertEqual(ws.timeline.yview(), vertical)
        self.assertEqual((ws.current, ws.zoom), (0., 50))
        self.wheel(120)
        self.assertAlmostEqual(ws.timeline.canvasx(0), left, delta=1.1)
        self.assertEqual(ws.project, before)
        self.assertEqual(ws.undo_stack, [])
        self.flush()

    def test_multi_notch_and_precision_deltas_have_matching_pan_distance(self):
        self.dense()
        ws = self.ws
        initial = ws.timeline.xview()[0]
        self.wheel(-120)
        one = ws.timeline.canvasx(0)
        ws.timeline.xview_moveto(initial)
        self.wheel(-40)
        self.wheel(-40)
        self.assertNotEqual(ws.timeline.canvasx(0), one)
        self.wheel(-40)
        self.assertEqual(ws.timeline.canvasx(0), one)
        ws.timeline.xview_moveto(initial)
        self.wheel(-240)
        two = ws.timeline.canvasx(0)
        ws.timeline.xview_moveto(initial)
        self.wheel(-120)
        self.wheel(-120)
        self.assertAlmostEqual(ws.timeline.canvasx(0), two, delta=1.1)

    def test_alt_wheel_steps_frames_and_pauses_in_one_seek(self):
        ws = self.ws
        ws.player = RecordingPlayer()
        for fps in (24., 30., 30000/1001):
            with self.subTest(fps=fps):
                ws.project.fps = fps
                ws.current = 100/fps
                ws.playing = True
                ws.player.commands.clear()
                view, vertical, zoom = ws.timeline.xview(), ws.timeline.yview(), ws.zoom
                self.wheel(-240, 'Alt-')
                self.assertAlmostEqual(ws.current, 102/fps)
                self.assertFalse(ws.playing)
                self.assertEqual(len(ws.player.scrubs), 1)
                self.assertTrue(ws.player.scrubs[0]['pause'])
                self.assertEqual(ws.player.scrubs[0]['time'], ws.current)
                self.assertEqual(ws.seek_var.get(), format_time(ws.current))
                self.assertEqual((ws.timeline.xview(), ws.timeline.yview(), ws.zoom), (view, vertical, zoom))
                self.wheel(120, 'Alt-')
                self.assertAlmostEqual(ws.current, 101/fps)
        self.assertEqual(ws.undo_stack, [])

    def test_alt_wheel_respects_first_last_frame_busy_and_empty_project(self):
        ws = self.ws
        ws.current = 0.
        self.wheel(240, 'Alt-')
        self.assertEqual(ws.current, 0.)
        ws.current = ws.project.duration
        self.wheel(-240, 'Alt-')
        self.assertEqual(ws.current, ws._last_transport_frame()/ws.project.fps)
        ws.busy = True
        before = ws.current
        self.wheel(120, 'Alt-')
        self.assertEqual(ws.current, before)
        ws.busy = False
        ws.project = MulticamProject()
        ws.current = 0.
        self.wheel(-120, 'Alt-')
        self.assertEqual(ws.current, 0.)

    def test_alt_wheel_follows_playhead_back_into_view(self):
        self.dense()
        ws = self.ws
        ws.current = 500.
        self.assertLess(self.head_x(), ws._timeline_left)
        vertical, zoom = ws.timeline.yview(), ws.zoom
        self.wheel(-120, 'Alt-')
        self.assertAlmostEqual(ws.current, 500.+1/ws.project.fps)
        self.assertGreaterEqual(self.head_x(), ws._timeline_left)
        self.assertLess(self.head_x(), ws.timeline.winfo_width())
        self.assertEqual((ws.timeline.yview(), ws.zoom), (vertical, zoom))

    def test_all_zoom_actions_anchor_to_playhead_and_repeated_zoom_stays_stable(self):
        self.dense()
        ws = self.ws
        ws.current = ws._timeline_origin()+(ws.timeline.canvasx(480)-ws._timeline_left)/ws._timeline_scale()
        current, position, vertical = ws.current, self.head_x(), ws.timeline.yview()
        for _ in range(4):
            self.wheel(120, 'Control-')
            self.assertAlmostEqual(self.head_x(), position, delta=1.1)
            ws.change_zoom(.5)
            self.assertAlmostEqual(self.head_x(), position, delta=1.1)
        self.assertEqual((ws.current, ws.zoom, ws.timeline.yview()), (current, 50, vertical))
        self.assertEqual(ws.undo_stack, [])
        self.flush()
        self.assertAlmostEqual(self.head_x(), position, delta=1.1)

    def test_zoom_brings_offscreen_playhead_to_center(self):
        self.dense()
        ws = self.ws
        for current in (500., 2600.):
            with self.subTest(current=current):
                ws.current = current
                ws.timeline.xview_moveto(.3)
                expected = (ws._timeline_left+ws.timeline.winfo_width()-ws._px(16))/2
                self.wheel(120, 'Control-')
                self.assertAlmostEqual(self.head_x(), expected, delta=1.1)
                self.assertEqual(ws.current, current)

    def test_ctrl_takes_priority_over_alt_and_zoom_limits_are_stable(self):
        self.dense()
        ws = self.ws
        ws.current = 1800.
        self.wheel(120, 'Control-Alt-')
        self.assertEqual((ws.current, ws.zoom), (1800., 100))
        self.wheel(120*30, 'Control-')
        self.assertEqual(ws.zoom, 10000)
        view = ws.timeline.xview()
        self.wheel(120, 'Control-')
        self.assertEqual(ws.timeline.xview(), view)
        self.wheel(-120*30, 'Control-')
        self.assertEqual(ws.zoom, 1)
        self.wheel(-120, 'Control-')
        self.assertEqual(ws.zoom, 1)
        self.assertEqual(ws.current, 1800.)

    def test_shift_does_not_override_alt_or_ctrl(self):
        ws = self.ws
        ws.current = 5.
        self.wheel(-120, 'Alt-Shift-')
        self.assertAlmostEqual(ws.current, 5.+1/ws.project.fps)
        current = ws.current
        self.wheel(120, 'Control-Shift-')
        self.assertEqual((ws.current, ws.zoom), (current, 2))

    def test_pan_clamps_to_timeline_ends(self):
        self.dense()
        ws = self.ws
        for fraction, delta in ((0., 120), (1., -120)):
            ws.timeline.xview_moveto(fraction)
            view = ws.timeline.xview()
            self.wheel(delta)
            self.assertEqual(ws.timeline.xview(), view)

    def test_precision_remainder_does_not_leak_between_modes(self):
        ws = self.ws
        ws.current = 5.
        self.wheel(80)
        self.wheel(-40, 'Alt-')
        self.assertEqual(ws.current, 5.)
        self.wheel(-80, 'Alt-')
        self.assertAlmostEqual(ws.current, 5.+1/ws.project.fps)
        self.wheel(40, 'Control-')
        self.assertEqual(ws.zoom, 1)
        self.wheel(80, 'Control-')
        self.assertEqual(ws.zoom, 2)
        state = (ws.current, ws.zoom, ws.timeline.xview(), ws.timeline.yview())
        for modifier in ('', 'Alt-', 'Control-'):
            self.wheel(0, modifier)
        self.assertEqual((ws.current, ws.zoom, ws.timeline.xview(), ws.timeline.yview()), state)

    def test_wheel_navigation_is_frozen_during_edit_gestures(self):
        self.dense()
        ws = self.ws
        for gesture in ('timeline_drag', 'subtitle_drag', '_program_boundary_drag',
                        '_video_stack_drag', '_selection_gesture', '_timeline_scrubbing',
                        '_timeline_divider_drag', '_timeline_drag_geometry'):
            with self.subTest(gesture=gesture):
                setattr(ws, gesture, {'active': True})
                state = (ws.current, ws.zoom, ws.timeline.xview(), ws.timeline.yview())
                for modifier in ('', 'Alt-', 'Control-'):
                    self.wheel(120, modifier)
                self.assertEqual((ws.current, ws.zoom, ws.timeline.xview(), ws.timeline.yview()), state)
                setattr(ws, gesture, None)

    def test_wheel_navigation_survives_save_and_reopen(self):
        self.dense()
        ws = self.ws
        ws.current = 1200.
        self.wheel(-120, 'Alt-')
        self.wheel(120, 'Control-')
        self.wheel(-120)
        self.flush()
        expected = ws._capture_timeline_view()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'wheel.multicam.json'
            ws.project_path = path
            self.assertTrue(ws.save())
            ws.new()
            with patch('multicam_ui.messagebox.showwarning'):
                ws.load_project(path)
            self.root.update()
            actual = ws._capture_timeline_view()
            self.assertEqual(actual['position'], expected['position'])
            self.assertEqual(actual['zoom'], expected['zoom'])
            self.assertAlmostEqual(actual['x_fraction'], expected['x_fraction'], delta=.001)
            self.assertAlmostEqual(actual['y_fraction'], expected['y_fraction'], delta=.01)
        self.assertEqual(self.fixture.errors, [])


if __name__ == '__main__':
    unittest.main()
