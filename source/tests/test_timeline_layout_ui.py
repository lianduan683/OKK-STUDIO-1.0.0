"""Real Tk gestures must resize/fold the view without editing the project."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from . import test_program_edit_ui as fixture


class TimelineLayoutTests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def prepare(self, master=True):
        ws = self.workspace
        ws.workspace_split.sashpos(0, ws._px(190))
        self.root.update()
        ws.project.master_audio = 'master.wav' if master else ''
        ws.project.master_duration = 12. if master else 0.
        ws.draw_timeline()
        return ws

    def click(self, bounds):
        ws = self.workspace
        left, top, right, bottom = bounds
        x = round((left+right)/2 - ws.timeline.canvasx(0))
        y = round((top+bottom)/2 - ws.timeline.canvasy(0))
        ws.timeline.event_generate('<ButtonPress-1>', x=x, y=y)
        ws.timeline.event_generate('<ButtonRelease-1>', x=x, y=y)
        self.root.update()

    def start_drag(self):
        ws = self.workspace
        top, bottom = ws._timeline_divider_bounds
        x, y = 300, round((top+bottom)/2-ws.timeline.canvasy(0))
        ws.timeline.event_generate('<ButtonPress-1>', x=x, y=y)
        self.assertIsNotNone(ws._timeline_divider_drag)
        return x, y

    def test_group_fold_keeps_master_and_sound_settings_and_restores_rows(self):
        ws = self.prepare()
        before = deepcopy(ws.project)
        undo = len(ws.undo_stack)
        self.click(ws._audio_group_button)
        self.assertEqual(set(ws._timeline_audio_track_bounds), {0})
        self.assertTrue(ws.timeline_audio_items)
        self.assertFalse(ws._audio_fold_buttons)
        self.assertEqual(ws.project, before)
        self.assertEqual(len(ws.undo_stack), undo)
        self.assertIsNone(ws._selection_gesture)
        self.click(ws._audio_group_button)
        self.assertEqual(set(ws._timeline_audio_track_bounds), {0, 1, 2})
        self.assertEqual(ws.project, before)

    def test_individual_fold_compacts_one_row_and_keeps_mute_control(self):
        ws = self.prepare()
        before = deepcopy(ws.project)
        master_height = ws._timeline_audio_row_heights[0]
        original = ws._timeline_audio_row_heights[1]
        self.click(ws._audio_fold_buttons[1])
        self.assertLess(ws._timeline_audio_row_heights[1], original)
        self.assertEqual(ws._timeline_audio_row_heights[0], master_height)
        self.assertIn(1, ws._audio_track_buttons)
        self.assertEqual(ws.project, before)
        self.click(ws._audio_fold_buttons[1])
        self.assertAlmostEqual(ws._timeline_audio_row_heights[1], original)

    def test_drag_changes_both_row_heights_without_seeking_selecting_or_dirtying(self):
        ws = self.prepare()
        before = deepcopy(ws.project)
        selected, current, dirty = set(ws._timeline_selection), ws.current, ws.dirty
        undo = len(ws.undo_stack)
        video_before = ws._timeline_track_bounds[1][1]-ws._timeline_track_bounds[1][0]
        audio_before = ws._timeline_audio_row_heights[0]
        x, y = self.start_drag()
        ws.timeline.event_generate('<B1-Motion>', x=x, y=y-45)
        ws.timeline.event_generate('<ButtonRelease-1>', x=x, y=y-45)
        self.root.update()
        video_after = ws._timeline_track_bounds[1][1]-ws._timeline_track_bounds[1][0]
        self.assertLess(video_after, video_before)
        self.assertGreater(ws._timeline_audio_row_heights[0], audio_before)
        self.assertEqual(ws.project, before)
        self.assertEqual((ws.current, ws.dirty, len(ws.undo_stack)), (current, dirty, undo))
        self.assertEqual(ws._timeline_selection, selected)
        self.assertIsNone(ws._selection_gesture)
        self.assertIsNone(ws._timeline_divider_drag)
        self.assertEqual(self.callback_errors, [])

    def test_escape_cancels_drag_and_zoom_is_frozen_during_resize(self):
        ws = self.prepare()
        original = dict(ws._timeline_audio_row_heights)
        x, y = self.start_drag()
        ws.timeline.event_generate('<B1-Motion>', x=x, y=y-40)
        zoom = ws.zoom
        ws.change_zoom(2)
        ws.fit_timeline()
        self.assertEqual(ws.zoom, zoom)
        ws._key(SimpleNamespace(keysym='Escape', char='', state=0, widget=ws.timeline))
        self.assertIsNone(ws._timeline_divider_drag)
        self.assertEqual(ws._timeline_audio_row_heights, original)

    def test_losing_focus_cancels_resize_without_leaving_a_stuck_gesture(self):
        ws = self.prepare()
        original = dict(ws._timeline_audio_row_heights)
        x, y = self.start_drag()
        ws.timeline.event_generate('<B1-Motion>', x=x, y=y-40)
        ws.timeline.event_generate('<FocusOut>')
        self.assertIsNone(ws._timeline_divider_drag)
        self.assertEqual(ws._timeline_audio_row_heights, original)
        self.assertEqual(ws.timeline.cget('cursor'), '')

    def test_divider_limits_and_fold_restore_custom_split(self):
        ws = self.prepare()
        x, y = self.start_drag()
        ws.timeline_motion(SimpleNamespace(x=x, y=y-100000))
        ws.timeline_release(SimpleNamespace(x=x, y=y-100000))
        self.assertGreaterEqual(ws._timeline_track_bounds[1][1]-ws._timeline_track_bounds[1][0],
                                ws._timeline_label_font.metrics('linespace')+ws._px(6))
        heights = dict(ws._timeline_audio_row_heights)
        self.click(ws._audio_group_button)
        self.click(ws._audio_group_button)
        self.assertEqual(ws._timeline_audio_row_heights, heights)
        x, y = self.start_drag()
        ws.timeline_motion(SimpleNamespace(x=x, y=y+100000))
        ws.timeline_release(SimpleNamespace(x=x, y=y+100000))
        for value in ws._timeline_audio_row_heights.values():
            self.assertGreaterEqual(value, ws._px(28))

    def test_no_master_group_can_still_be_reopened_and_headers_follow_horizontal_scroll(self):
        ws = self.prepare(master=False)
        ws.zoom = 5
        ws.draw_timeline()
        ws._scroll_timeline('moveto', .4)
        ws.draw_timeline()
        self.click(ws._audio_group_button)
        self.assertEqual(ws._timeline_audio_track_bounds, {})
        self.assertFalse(ws.timeline_audio_items)
        self.click(ws._audio_group_button)
        self.assertEqual(set(ws._timeline_audio_track_bounds), {1, 2})
        self.assertIsNone(ws._timeline_audio_bounds)
        self.assertEqual(self.callback_errors, [])


if __name__ == '__main__': unittest.main()
