"""Blank timeline clicks clear programme state and every selection outline."""
from copy import deepcopy
import unittest

from . import test_timeline_drag_merge as fixture
from . import test_program_edit_ui as program_fixture


class TimelineBlankDeselectTests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown
    program_event = program_fixture.ProgramEditWorkspaceTests.program_event

    def click(self, x, y, state=0):
        canvas = self.workspace.timeline
        canvas.event_generate('<ButtonPress-1>', x=int(x), y=int(y), state=state)
        canvas.event_generate('<ButtonRelease-1>', x=int(x), y=int(y), state=state)
        self.root.update()
        self.assertFalse(self.errors)
        self.mocks[2].assert_not_called()

    def select_middle(self):
        ws = self.workspace
        ws.project.edit_points = [2., 5., 8.]
        ws.project.timeline_multicam_collapsed = True
        ws._refresh()
        ws.seek(3.)
        event = self.program_event((2., 5.))
        self.click(event.x, event.y)
        self.assertEqual(ws.selected_program_range, (2., 5.))
        self.assertTrue(ws.timeline.find_withtag('selection'))
        return ws

    def blank_point(self):
        ws = self.workspace
        top, bottom = ws._timeline_subtitle_bounds
        return (ws._timeline_x(4.)-ws.timeline.canvasx(0),
                (top+bottom)/2-ws.timeline.canvasy(0))

    def assert_deselected(self):
        ws = self.workspace
        self.assertIsNone(ws.selected_program_range)
        self.assertFalse(ws._timeline_selection)
        self.assertFalse(ws.timeline.find_withtag('selection'))
        self.assertFalse(ws.timeline.find_withtag('timeline-selected'))
        for item in ws.timeline_program_items:
            if ws.timeline.type(item) == 'rectangle':
                self.assertEqual(ws.timeline.itemcget(item, 'outline'), '#91a0ac')
                self.assertEqual(float(ws.timeline.itemcget(item, 'width')), 1.)

    def test_blank_subtitle_lane_clears_state_outline_inspector_and_cut_tree(self):
        ws = self.select_middle()
        before = deepcopy(ws.project)
        self.click(*self.blank_point())
        self.assert_deselected()
        self.assertIsNone(ws.selected_cut)
        self.assertFalse(ws.cut_tree.selection())
        self.assertIsNone(ws.clip_inspector._target)
        self.assertEqual(ws.current, 3.)
        self.assertEqual(ws.project, before)
        self.assertFalse(ws.undo_stack)
        self.assertIn('取消', ws.status.get())

    def test_blank_ruler_seeks_and_clears_the_selected_segment(self):
        ws = self.select_middle()
        self.click(ws._timeline_x(6.)-ws.timeline.canvasx(0), ws._timeline_ruler_h/2)
        self.assert_deselected()
        self.assertAlmostEqual(ws.current, 6., delta=1/ws.project.fps)
        self.assertFalse(ws.undo_stack)

    def test_ctrl_ruler_seek_preserves_explicit_selection(self):
        ws = self.select_middle()
        self.click(ws._timeline_x(6.)-ws.timeline.canvasx(0), ws._timeline_ruler_h/2, state=4)
        self.assertEqual(ws.selected_program_range, (2., 5.))

    def test_blank_click_then_camera_click_creates_a_playhead_cut(self):
        ws = self.select_middle()
        self.click(*self.blank_point())
        self.assert_deselected()
        ws.camera_buttons[2].invoke()
        self.assertEqual([ws.project.camera_at(t) for t in (1., 2., 2.9, 3., 4.9, 7.)],
                         [1, 1, 1, 2, 2, 2])
        self.assertEqual(len(ws.undo_stack), 1)

    def test_selection_can_be_cleared_repeatedly_without_redrawing_or_editing_project(self):
        ws = self.select_middle()
        before = deepcopy(ws.project)
        for _ in range(3):
            self.click(*self.blank_point())
            self.assert_deselected()
            event = self.program_event((5., 8.))
            self.click(event.x, event.y)
            self.assertEqual(ws.selected_program_range, (5., 8.))
            self.assertTrue(ws.timeline.find_withtag('selection'))
        self.assertEqual(ws.project, before)
        self.assertFalse(ws.undo_stack)

    def test_ctrl_clicking_selected_segment_removes_all_of_its_highlights(self):
        ws = self.select_middle()
        event = self.program_event((2., 5.))
        self.click(event.x, event.y, state=4)
        self.assert_deselected()


if __name__ == '__main__':
    unittest.main()
