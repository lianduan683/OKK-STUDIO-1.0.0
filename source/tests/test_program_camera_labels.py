"""Existing programme cuts relabel by library order without changing footage."""
from copy import deepcopy
import unittest

from creative_core import ColorGrade
from multicam_core import Cut, MulticamProject
from program_edits import ClipGrade, program_spans
from tests import test_camera_order_ui as fixture


class ProgramCameraLabelTests(unittest.TestCase):
    setUp = fixture.CameraOrderUITests.setUp
    tearDown = fixture.CameraOrderUITests.tearDown
    prepare = fixture.CameraOrderUITests.prepare
    point = fixture.CameraOrderUITests.point
    press = fixture.CameraOrderUITests.press
    drag = fixture.CameraOrderUITests.drag

    def setup_edit(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        ws.project.cuts = [Cut(0., 1), Cut(3., 3), Cut(6., 2)]
        ws.project.clip_grades = [ClipGrade(3., 6., ColorGrade(exposure=.5))]
        ws._refresh()
        return ws

    def titles(self):
        ws = self.workspace
        return {span: ws.timeline.itemcget(item, 'text')
                for item, span in ws.timeline_program_items.items()
                if ws.timeline.type(item) == 'text'}

    def media_at(self):
        project = self.workspace.project
        return [(time, project.camera_at(time), project.clip_at(project.camera_at(time), time).id,
                 project.clip_at(project.camera_at(time), time).path,
                 project.clip_at(project.camera_at(time), time).source_in)
                for time in (0., 3.5, 8.)]

    def assert_relabelled(self):
        titles = self.titles()
        self.assertTrue(titles[(0., 3.)].startswith('2 · 主机位'), titles)
        self.assertTrue(titles[(3., 6.)].startswith('1 · 全景'), titles)
        self.assertIn('调色', titles[(3., 6.)])
        self.assertTrue(titles[(6., 10.)].startswith('3 · 侧面'), titles)
        self.assertEqual(self.errors, [])

    def test_drag_updates_existing_cut_labels_and_preserves_exact_project_edit(self):
        ws = self.setup_edit()
        original = deepcopy(ws.project)
        sources, spans = self.media_at(), program_spans(ws.project)
        self.drag('camera_3', 'camera_1')
        self.assert_relabelled()
        expected = deepcopy(original)
        expected.video_stack_order = [3, 1, 2]
        self.assertEqual(ws.project, expected)
        self.assertEqual(self.media_at(), sources)
        self.assertEqual(program_spans(ws.project), spans)
        ws.undo()
        self.assertEqual(ws.project, original)
        self.assertTrue(self.titles()[(3., 6.)].startswith('3 · 全景'))
        ws.redo()
        self.assert_relabelled()
        self.assertEqual(self.media_at(), sources)

    def test_opening_already_reordered_edit_only_redraws_labels(self):
        ws = self.setup_edit()
        ws.project.video_stack_order = [3, 1, 2]
        payload = ws.project.to_payload('.')
        ws.project = MulticamProject.from_payload(payload, '.')
        before, sources = deepcopy(ws.project), self.media_at()
        ws._refresh()
        self.assert_relabelled()
        ws.draw_timeline()
        self.assert_relabelled()
        self.assertEqual(ws.project, before)
        self.assertEqual(self.media_at(), sources)

    def test_disabled_and_graded_cut_labels_keep_status_and_correct_source(self):
        ws = self.setup_edit()
        ws.project.clips[0].enabled = False
        ws.project.video_stack_order = [3, 1, 2]
        before = deepcopy(ws.project)
        ws._refresh()
        titles = self.titles()
        self.assertTrue(titles[(0., 3.)].startswith('已禁用 · 2 · 主机位'))
        self.assertIn('1 · 全景', titles[(3., 6.)])
        self.assertIn('调色', titles[(3., 6.)])
        self.assertEqual(ws.project, before)
        self.assertEqual(self.errors, [])


if __name__ == '__main__': unittest.main()
