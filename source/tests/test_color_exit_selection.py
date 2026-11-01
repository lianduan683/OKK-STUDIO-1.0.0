"""Returning from colour leaves editing ready to insert a new cut."""
import copy
import unittest

from . import test_finishing_workspace as full
from . import test_realtime_grade_ui as embedded
from creative_core import ColorGrade


class ColorPageExitSelectionTests(unittest.TestCase):
    setUp = full.FinishingPageTests.setUp
    tearDown = full.FinishingPageTests.tearDown
    _create_app = full.FinishingPageTests._create_app
    _destroy_app = full.FinishingPageTests._destroy_app
    _settle = full.FinishingPageTests._settle
    seed_project = full.FinishingPageTests.seed_project

    def check_return(self, return_button):
        ws = self.workspace
        self.seed_project()
        self.app._select_page('multicam')
        ws.select_program_range(0., 3.)
        self.app._select_page('color')
        ws.seek(4.)
        ws.apply_active_grade(ColorGrade(exposure=.3))
        self.assertEqual(ws.selected_program_range, (3., 6.))
        before = copy.deepcopy(ws.project)
        undo = len(ws.undo_stack)
        position, dirty = ws.current, ws.dirty
        if return_button: ws.exit_color_workspace()
        else: self.app.page_buttons['multicam'].invoke()
        self._settle()
        self.assertEqual(self.app.active_page, 'multicam')
        self.assertFalse(ws._color_mode)
        self.assertIsNone(ws.selected_program_range)
        self.assertIsNone(ws.selected_cut)
        self.assertEqual(ws._timeline_selection, set())
        self.assertEqual(ws.cut_tree.selection(), ())
        self.assertEqual(ws.media_tree.selection(), ())
        self.assertFalse(ws.timeline.find_withtag('selection'))
        self.assertFalse(ws.timeline.find_withtag('timeline-selected'))
        self.assertEqual(ws.project, before)
        self.assertEqual(len(ws.undo_stack), undo)
        self.assertEqual((ws.current, ws.dirty), (position, dirty))

    def test_return_button_clears_selected_cut_and_retains_saved_grade_and_position(self):
        self.check_return(True)

    def test_bottom_multicam_tab_clears_selected_cut_and_retains_saved_grade_and_position(self):
        self.check_return(False)

    def test_color_to_delivery_retains_its_existing_selection_behavior(self):
        self.seed_project()
        self.app._select_page('color')
        ws = self.workspace
        ws.select_program_range(0., 3.)
        self.app._select_page('delivery')
        self.assertEqual(ws.selected_program_range, (0., 3.))


class EmbeddedColorExitSelectionTests(unittest.TestCase):
    setUp = embedded.RealtimeGradeWorkspaceTests.setUp
    tearDown = embedded.RealtimeGradeWorkspaceTests.tearDown

    def test_embedded_color_exit_clears_range_and_next_camera_click_inserts_cut(self):
        ws = self.workspace
        ws._set_timeline_selection({('program', (3., 7.))})
        ws.exit_color_workspace()
        self.assertIsNone(ws.selected_program_range)
        self.assertEqual(ws._timeline_selection, set())
        ws.switch(1, record=True)
        self.assertEqual(ws.project.camera_at(3.5), 2)
        self.assertEqual(ws.project.camera_at(4.), 1)
        self.assertIn(4., [cut.time for cut in ws.project.cuts])


if __name__ == '__main__': unittest.main()
