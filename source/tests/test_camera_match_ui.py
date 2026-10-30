"""Batch apply is atomic, reference-safe, cancelable and project-persistent."""
import copy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_auto_color_workspace as fixture
from camera_match import CameraMatchResult
from creative_core import ColorGrade
from multicam_core import CameraClip, MulticamProject


class CameraMatchUITests(unittest.TestCase):
    wait = fixture.AutoColorWorkspaceTests.wait
    tearDown = fixture.AutoColorWorkspaceTests.tearDown

    def setUp(self):
        fixture.AutoColorWorkspaceTests.setUp(self)
        third = self.folder / 'c.mp4'; third.write_bytes(b'fixture')
        self.workspace.project.clips.append(CameraClip('c', 3, str(third), 0, 12))
        self.workspace.project.camera_grades[1] = ColorGrade(exposure=.35, gamma_r=.02)
        self.workspace.set_color_reference_camera(1)
        self.workspace._refresh(reconfigure=False)
        self.workspace.undo_stack.clear()
        self.panel = self.workspace.color_workspace.camera_match_panel

    def result(self, project, reference, target, *args, **kwargs):
        return CameraMatchResult(target, ColorGrade(exposure=.2 * target), 5, .4, .1, 'matched')

    def run_match(self, **options):
        with patch('camera_match_workspace.match_camera', side_effect=self.result) as core:
            self.assertTrue(self.workspace.start_camera_match(1, [2, 3], reference_time=5., **options))
            self.wait(lambda: not self.workspace._auto_color_running)
        return core

    def test_batch_updates_only_other_cameras_and_is_one_undo_step(self):
        ws = self.workspace
        original = copy.deepcopy(ws.project)
        core = self.run_match()
        self.assertEqual(core.call_count, 2)
        self.assertEqual(core.call_args.args[3:5], (4., 8.))
        self.assertEqual(core.call_args.kwargs['reference_time'], 5.)
        self.assertEqual(ws.project.camera_grades[1], original.camera_grades[1])
        self.assertEqual(ws.project.global_grade, original.global_grade)
        self.assertEqual(ws.project.clip_grades, original.clip_grades)
        self.assertEqual(ws.project.cuts, original.cuts)
        self.assertEqual(set(ws.project.camera_grades), {1, 2, 3})
        self.assertEqual(len(ws.undo_stack), 1)
        path = self.folder / 'batch.multicam.json'
        ws.project.save(path)
        restored = MulticamProject.load(path)
        self.assertEqual(restored.camera_grades, ws.project.camera_grades)
        self.assertEqual(restored.color_reference_camera, 1)
        ws.undo()
        self.assertEqual(ws.project, original)
        self.assertEqual(self.panel.targets.item('2', 'values')[1], '待匹配')

    def test_cancel_discards_all_results_even_when_one_camera_already_finished(self):
        ws = self.workspace
        entered, release = threading.Event(), threading.Event()
        original = copy.deepcopy(ws.project)

        def analyze(project, ref, target, *args, **kwargs):
            if target == 3:
                entered.set(); release.wait(timeout=2)
            return self.result(project, ref, target)

        with patch('camera_match_workspace.match_camera', side_effect=analyze):
            ws.start_camera_match(1, [2, 3])
            self.wait(entered.is_set)
            ws.cancel_camera_match(); release.set()
            self.wait(lambda: not ws._auto_color_running)
        self.assertEqual(ws.project, original)
        self.assertEqual(ws.undo_stack, [])
        self.assertIn('已取消', ws.camera_match_status.get())

    def test_manual_change_during_analysis_discards_stale_batch(self):
        ws = self.workspace
        entered, release = threading.Event(), threading.Event()

        def analyze(*args, **kwargs):
            entered.set(); release.wait(timeout=2)
            return self.result(*args, **kwargs)

        with patch('camera_match_workspace.match_camera', side_effect=analyze):
            ws.start_camera_match(1, [2])
            self.wait(entered.is_set)
            ws.apply_active_grade(ColorGrade(exposure=-.1))
            changed = copy.deepcopy(ws.project)
            release.set(); self.wait(lambda: not ws._auto_color_running)
        self.assertEqual(ws.project, changed)
        self.assertNotIn(2, ws.project.camera_grades)
        self.assertIn('未应用', ws.camera_match_status.get())

    def test_one_missing_camera_is_reported_while_other_success_applies(self):
        ws = self.workspace

        def analyze(project, ref, target, *args, **kwargs):
            if target == 3:
                raise ValueError('原片缺失')
            return self.result(project, ref, target)

        with patch('camera_match_workspace.match_camera', side_effect=analyze):
            ws.start_camera_match(1, [2, 3])
            self.wait(lambda: not ws._auto_color_running)
        self.assertIn(2, ws.project.camera_grades)
        self.assertNotIn(3, ws.project.camera_grades)
        self.assertIn('原片缺失', self.panel.targets.item('3', 'values')[1])
        self.assertEqual(len(ws.undo_stack), 1)

    def test_panel_excludes_reference_and_dispatches_explicit_scope_and_strength(self):
        self.panel.refresh_choices()
        self.assertEqual(set(self.panel._reference_choices.values()), {1, 2, 3})
        self.assertEqual(set(self.panel.targets.get_children()), {'2', '3'})
        self.panel.clear_selection(); self.assertEqual(self.panel._checked, set())
        self.panel.select_all(); self.assertEqual(self.panel._checked, {2, 3})
        self.panel.range_var.set('整个工程')
        self.panel.strength_var.set(65)
        self.panel.reference_time_var.set('00:00:05.000')
        with patch.object(self.workspace, 'start_camera_match') as start:
            self.panel.apply()
        start.assert_called_once_with(1, [2, 3], range_mode='project', method='anchors', strength=.65, reference_time=5.)
        self.workspace.color_workspace.camera_match_button.invoke()
        self.root.update()
        self.assertEqual(self.workspace.color_workspace.grading_tabs.select(), str(self.panel))

    def test_reference_target_conflict_and_zero_strength_are_rejected(self):
        ws = self.workspace
        with patch('camera_match_workspace.messagebox.showerror') as error, \
                patch('camera_match_workspace.match_camera') as core:
            self.assertFalse(ws.start_camera_match(1, [1, 2]))
            self.assertFalse(ws.start_camera_match(1, [2], strength=0))
        self.assertEqual(error.call_count, 2)
        core.assert_not_called()
        self.assertFalse(ws._auto_color_running)


if __name__ == '__main__':
    unittest.main()
