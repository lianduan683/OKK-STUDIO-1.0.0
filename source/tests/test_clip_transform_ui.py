"""Selected-range framing, undo, persistence and cache invalidation."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clip_transform import ClipTransform, transform_at, set_transform_on_range
from creative_core import ColorGrade
from multicam_core import MulticamProject
from prerender import picture_signature, needs_render, PreviewBlock
from program_edits import grade_at, set_grade_on_range
from tests import test_clip_inspector as fixture


class ClipTransformInspectorTests(unittest.TestCase):
    setUp = fixture.ClipInspectorTests.setUp
    tearDown = fixture.ClipInspectorTests.tearDown

    def test_one_click_16_9_sets_canvas_and_only_selected_range_with_one_undo(self):
        ws, panel = self.workspace, self.panel
        ws.project.project_settings = dict(width=4096, height=3072, sample_rate=96000, bit_depth='32bit 浮点')
        original = copy.deepcopy(ws.project)
        self.assertTrue(panel.fill_16_9())
        settings = ws.project.project_settings
        self.assertEqual((settings['width'], settings['height']), (4096, 2304))
        self.assertEqual((settings['sample_rate'], settings['bit_depth']), (96000, '32bit 浮点'))
        self.assertEqual(transform_at(ws.project, 2), ClipTransform(mode='fill'))
        self.assertEqual(transform_at(ws.project, 4), ClipTransform())
        self.assertEqual(ws.project.clips, original.clips)
        self.assertEqual(len(ws.undo_stack), 1)
        self.assertEqual(panel.framing_zoom.get(), '100')
        self.assertIn('16:9', panel.framing_text.get())
        ws.undo()
        self.assertEqual(ws.project, original)
        ws.redo()
        self.assertEqual(transform_at(ws.project, 2).mode, 'fill')
        self.dialog.assert_not_called()

    def test_adjust_zoom_pan_preserves_grade_then_reset_preserves_other_range(self):
        ws, panel = self.workspace, self.panel
        set_grade_on_range(ws.project, 0, 4, ColorGrade(exposure=.75))
        set_transform_on_range(ws.project, 4, 7, ClipTransform(mode='fill', zoom=1.5))
        panel.framing_mode.set('填满画幅 · 等比裁切')
        panel.framing_zoom.set('125'); panel.framing_x.set('-25'); panel.framing_y.set('40')
        self.assertTrue(panel.apply_framing())
        self.assertEqual(transform_at(ws.project, 2), ClipTransform(mode='fill', zoom=1.25, x=-.25, y=.4))
        self.assertEqual(grade_at(ws.project, 2).exposure, .75)
        self.assertEqual(panel.framing_y.get(), '40')
        ws.select_program_range(4, 7)
        self.assertEqual(panel.framing_zoom.get(), '150')
        ws.select_program_range(0, 4)
        self.assertTrue(panel.reset_framing())
        self.assertEqual(transform_at(ws.project, 2), ClipTransform())
        self.assertEqual(transform_at(ws.project, 5).zoom, 1.5)
        self.assertEqual(grade_at(ws.project, 2).exposure, .75)
        self.dialog.assert_not_called()

    def test_invalid_values_never_change_project_or_undo(self):
        ws, panel = self.workspace, self.panel
        original = copy.deepcopy(ws.project)
        for variable, value in ((panel.framing_zoom, 'NaN'), (panel.framing_zoom, '99'),
                                (panel.framing_zoom, '401'), (panel.framing_x, '101'),
                                (panel.framing_y, '-101')):
            with self.subTest(value=value):
                panel.framing_zoom.set('100'); panel.framing_x.set('0'); panel.framing_y.set('0')
                variable.set(value)
                panel.apply_framing()
                self.assertEqual(ws.project, original)
                self.assertEqual(ws.undo_stack, [])
        self.assertEqual(self.dialog.call_count, 5)

    def test_saved_framing_reloads_and_changes_render_signatures(self):
        ws = self.workspace
        before = picture_signature(ws.project)
        self.panel.fill_16_9()
        self.assertNotEqual(picture_signature(ws.project), before)
        self.assertEqual(needs_render(ws.project, PreviewBlock(0, 120, 30), {}), '裁切与缩放')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'framing.multicam.json'
            ws.project.save(path)
            loaded = MulticamProject.load(path)
        self.assertEqual(loaded.clip_transforms, ws.project.clip_transforms)
        self.assertEqual(loaded.project_settings, ws.project.project_settings)
        ws.selected_program_range = None
        original = copy.deepcopy(ws.project)
        self.assertFalse(ws.apply_selected_transform(ClipTransform(mode='fill')))
        self.assertEqual(ws.project, original)


if __name__ == '__main__':
    unittest.main()
