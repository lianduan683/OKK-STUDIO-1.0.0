"""Real colour-strip navigation, including zoomed selection coordinates."""
import copy
from types import SimpleNamespace
import unittest

from . import test_realtime_grade_ui as fixture


class ColorTimelineWheelTests(unittest.TestCase):
    setUp = fixture.RealtimeGradeWorkspaceTests.setUp
    tearDown = fixture.RealtimeGradeWorkspaceTests.tearDown

    def wheel(self, delta, modifier=''):
        self.workspace.color_workspace.strip.event_generate('<'+modifier+'MouseWheel>', x=230, y=40, delta=delta)

    def test_ctrl_zoom_anchors_playhead_and_plain_wheel_pans_without_edits(self):
        ws = self.workspace
        panel = ws.color_workspace
        original = copy.deepcopy(ws.project)
        before = (ws.current, ws.selected_program_range, len(ws.undo_stack))
        anchor = panel._x_from_time(ws.current)
        for _ in range(4):
            self.wheel(120, 'Control-')
            self.assertAlmostEqual(panel._x_from_time(ws.current), anchor, delta=1e-6)
        self.assertEqual(panel._strip_zoom, 16)
        left = panel._strip_start
        self.wheel(-120)
        self.assertGreater(panel._strip_start, left)
        self.wheel(120)
        self.assertAlmostEqual(panel._strip_start, left)
        self.assertEqual((ws.current, ws.selected_program_range, len(ws.undo_stack)), before)
        self.assertEqual(ws.project, original)
        self.assertFalse(any(c.args[0] in ('seek', 'scrub') for c in self.player.submit.call_args_list))

    def test_offscreen_zoom_centers_playhead_and_zoom_limits_and_fit_are_stable(self):
        ws = self.workspace
        panel = ws.color_workspace
        panel._strip_zoom, panel._strip_start = 50., 8.
        panel._draw_strip()
        self.wheel(120, 'Control-Alt-')
        self.assertEqual(panel._strip_zoom, 100)
        self.assertAlmostEqual(panel._x_from_time(ws.current), panel.strip.winfo_width()/2, delta=1e-6)
        self.wheel(120*30, 'Control-')
        self.assertEqual(panel._strip_zoom, 10000)
        start = panel._strip_start
        self.wheel(120, 'Control-')
        self.assertEqual(panel._strip_start, start)
        self.wheel(-120*30, 'Control-')
        self.assertEqual((panel._strip_zoom, panel._strip_start), (1., 0.))
        self.wheel(120*3, 'Control-')
        panel._fit_strip()
        self.assertEqual((panel._strip_zoom, panel._strip_start), (1., 0.))
        self.assertEqual(tuple(map(float, panel.strip_scroll.get())), (0., 1.))

    def test_precision_notches_and_modifier_priority_match_editing_without_shared_remainder(self):
        ws = self.workspace
        panel = ws.color_workspace
        main_wheel = (ws._timeline_wheel_mode, ws._timeline_wheel_remainder)
        for _ in range(2):self.wheel(40, 'Control-')
        self.assertEqual(panel._strip_zoom, 1)
        self.wheel(40, 'Control-Shift-')
        self.assertEqual(panel._strip_zoom, 2)
        current = ws.current
        self.wheel(-40, 'Alt-Shift-')
        self.wheel(-80, 'Alt-')
        self.assertAlmostEqual(ws.current, current+1/ws.project.fps)
        self.wheel(120, 'Control-Alt-')
        self.assertEqual(panel._strip_zoom, 4)
        self.assertEqual((ws._timeline_wheel_mode, ws._timeline_wheel_remainder), main_wheel)

    def test_alt_wheel_pauses_seeks_once_and_follows_frame_back_into_view(self):
        ws = self.workspace
        panel = ws.color_workspace
        panel._strip_zoom, panel._strip_start = 50., 8.
        panel._draw_strip()
        self.player.submit.reset_mock()
        self.wheel(-240, 'Alt-')
        self.assertAlmostEqual(ws.current, 4.+2/ws.project.fps)
        seeks = [c for c in self.player.submit.call_args_list if c.args[0] in ('seek', 'scrub')]
        self.assertEqual(len(seeks), 1)
        self.assertTrue(seeks[0].kwargs['pause'])
        self.assertTrue(panel.px(8) <= panel._x_from_time(ws.current) <= panel.strip.winfo_width()-panel.px(8))
        ws.current = 0.
        self.wheel(240, 'Alt-')
        self.assertEqual(ws.current, 0.)
        ws.current = ws.project.duration
        self.wheel(-240, 'Alt-')
        self.assertEqual(ws.current, ws._last_transport_frame()/ws.project.fps)

    def test_zoomed_click_ruler_and_range_drag_resolve_visible_times(self):
        ws = self.workspace
        panel = ws.color_workspace
        self.wheel(240, 'Control-')
        def event(time, y=40, shift=False):
            return SimpleNamespace(x=panel._x_from_time(time), y=y, state=1 if shift else 0)
        panel._strip_press(event(5))
        panel._strip_release(event(5))
        self.assertEqual(ws.selected_program_range, (3., 7.))
        self.assertAlmostEqual(ws.current, 5.)
        panel._strip_press(event(4.5, y=10))
        panel._strip_release(event(4.5, y=10))
        self.assertAlmostEqual(ws.current, 4.5)
        panel._strip_press(event(3.2, shift=True))
        panel._strip_motion(event(5.5, shift=True))
        panel._strip_release(event(5.5, shift=True))
        self.assertEqual(ws.selected_program_range, (3.2, 5.5))
        self.assertEqual(ws.project.clip_grades, [])

    def test_middle_drag_scrollbar_and_bounds_keep_playhead_and_selection(self):
        ws = self.workspace
        panel = ws.color_workspace
        self.wheel(360, 'Control-')
        before = (ws.current, ws.selected_program_range)
        left = panel._strip_start
        panel._strip_pan_press(SimpleNamespace(x=400))
        panel._strip_pan_motion(SimpleNamespace(x=200))
        panel._strip_pan_release()
        self.assertGreater(panel._strip_start, left)
        panel._scroll_strip('moveto', '.4')
        self.assertAlmostEqual(panel._strip_start, ws.project.duration*.4)
        panel._scroll_strip('moveto', '1')
        end = panel._strip_start
        self.wheel(-120)
        self.assertEqual(panel._strip_start, end)
        panel._scroll_strip('moveto', '0')
        self.wheel(120)
        self.assertEqual(panel._strip_start, 0.)
        self.assertEqual((ws.current, ws.selected_program_range), before)

    def test_wheel_does_not_interrupt_selection_or_an_editing_gesture_and_view_survives_return(self):
        ws = self.workspace
        panel = ws.color_workspace
        self.wheel(360, 'Control-')
        state = (panel._strip_zoom, panel._strip_start, ws.current)
        for target, name in ((panel, '_drag'), (ws, 'timeline_drag')):
            setattr(target, name, {'active': True})
            for modifier in ('', 'Control-', 'Alt-'):self.wheel(120, modifier)
            self.assertEqual((panel._strip_zoom, panel._strip_start, ws.current), state)
            setattr(target, name, None)
        ws.exit_color_workspace()
        ws.enter_color_workspace()
        self.assertEqual((panel._strip_zoom, panel._strip_start, ws.current), state)


if __name__ == '__main__':
    unittest.main()
