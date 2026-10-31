"""Crop entry without an explicit programme selection, with visible edit scope."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clip_transform import ClipTransform, transform_at
from multicam_core import CameraClip, Cut, MulticamProject
from tests import test_timeline_drag_merge as fixture


class InspectorTargetTests(unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)
        self.ws = self.workspace
        self.ws.project.cuts = [Cut(0, 1), Cut(4, 2), Cut(7, 1)]
        self.ws.current = 5
        self.ws._refresh()
        self.ws._set_timeline_selection(set())
        self.ws.show_inspector('edit')
        self.panel = self.ws.clip_inspector

    tearDown = fixture.TimelineDropTests.tearDown

    def test_no_selection_shows_real_playhead_target_and_enables_crop_without_editing(self):
        before = deepcopy(self.ws.project)
        self.panel.refresh()
        self.assertIsNone(self.ws.selected_program_range)
        self.assertEqual(self.ws.program_range_at_playhead(), (4, 7))
        self.assertIn('机位 2', self.panel.target_text.get())
        self.assertIn('点击裁切将自动选中', self.panel.target_text.get())
        self.assertEqual(str(self.panel.select_current_button.cget('state')), 'normal')
        self.assertTrue(all(str(button.cget('state')) == 'normal' for button in self.panel.framing_actions))
        other = [button for button in self.panel.action_widgets if button not in self.panel.framing_actions]
        self.assertTrue(all(str(button.cget('state')) == 'disabled' for button in other))
        self.assertEqual(self.ws.project, before)
        self.assertEqual(self.ws.undo_stack, [])

    def test_manual_and_auto_select_actual_programme_not_unapplied_camera_choice(self):
        ws, panel = self.ws, self.panel
        original = deepcopy(ws.project)
        panel.camera_var.set(next(name for name, camera in panel.camera_choices.items() if camera == 1))
        with patch('crop_editor.open_crop_editor', return_value='editor') as open_editor:
            self.assertEqual(panel.manual_framing(), 'editor')
            open_editor.assert_called_with(ws)
            self.assertEqual(ws.selected_program_range, (4, 7))
            self.assertEqual(ws._timeline_selection, {('program', (4, 7))})
            self.assertEqual(ws.project.camera_at(5), 2)
            ws._set_timeline_selection(set())
            self.assertEqual(panel.auto_framing(), 'editor')
            open_editor.assert_called_with(ws, automatic=True)
        self.assertEqual(ws.project, original)
        self.assertEqual(ws.undo_stack, [])

    def test_quick_select_enables_all_modules_without_camera_or_colour_changes(self):
        original = deepcopy(self.ws.project)
        self.panel.select_current_button.invoke()
        self.assertEqual(self.ws.selected_program_range, (4, 7))
        self.assertTrue(all(str(button.cget('state')) == 'normal' for button in self.panel.action_widgets))
        self.assertFalse(self.panel.select_current_button.winfo_manager())
        self.assertEqual(self.ws.project, original)
        self.assertEqual(self.ws.undo_stack, [])

    def test_numeric_crop_values_survive_automatic_selection_and_stay_in_range(self):
        self.panel.framing_mode.set('填满画幅 · 等比裁切')
        self.panel.framing_zoom.set('150')
        self.panel.framing_x.set('-20')
        self.panel.framing_y.set('35')
        self.assertTrue(self.panel.apply_framing())
        self.assertEqual(transform_at(self.ws.project, 5), ClipTransform('fill', 1.5, -.2, .35))
        self.assertEqual(transform_at(self.ws.project, 3), ClipTransform())
        self.assertEqual(transform_at(self.ws.project, 7), ClipTransform())
        self.assertEqual(len(self.ws.undo_stack), 1)

    def test_selection_clear_and_paused_seek_update_scope_and_button_states(self):
        self.panel.select_current()
        self.ws._set_timeline_selection({('clip', 'one')})
        self.assertIsNone(self.ws.selected_program_range)
        self.assertIn('点击裁切将自动选中', self.panel.target_text.get())
        self.ws.current = 8
        self.ws._show_time()
        self.assertIn('机位 1', self.panel.target_text.get())
        self.assertIn('00:00:07.000', self.panel.target_text.get())
        self.assertIsNone(self.ws.selected_program_range)
        self.assertEqual(self.ws.undo_stack, [])

    def test_gaps_empty_project_and_end_do_not_select_or_crop_a_neighbour(self):
        ws = self.ws
        ws.project = MulticamProject(clips=[CameraClip('later', 1, 'later.mp4', 2, 3)],
                                     master_duration=8, cuts=[Cut(0, 1)])
        ws.current = 1
        ws._refresh()
        self.assertIsNone(ws.program_range_at_playhead())
        self.assertTrue(all(str(button.cget('state')) == 'disabled' for button in self.panel.framing_actions))
        self.assertIn('没有可用', self.panel.target_text.get())
        self.assertFalse(self.panel.select_current())
        ws.current = 3
        ws._show_time()
        self.assertEqual(ws.program_range_at_playhead(), (2, 5))
        self.assertTrue(all(str(button.cget('state')) == 'normal' for button in self.panel.framing_actions))
        for position in (6, 8):
            ws.current = position
            self.assertIsNone(ws.program_range_at_playhead())
        ws.project = MulticamProject()
        ws.current = 0
        ws._refresh()
        self.assertIsNone(ws.program_range_at_playhead())
        self.assertFalse(self.panel.select_current())
        self.assertEqual(ws.undo_stack, [])


if __name__ == '__main__': unittest.main()
