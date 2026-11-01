"""Source-library positions are display numbers; edit references remain stable."""
from copy import deepcopy
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from camera_display import camera_number, camera_id_for_number
from color_workspace import grade_target_label, grade_target_from_label
from multicam_core import CameraClip, Cut
from tests import test_camera_order_ui as fixture
from timeline_edits import delete_source_clips, finalize_source_edit


class CameraNumberTests(unittest.TestCase):
    setUp = fixture.CameraOrderUITests.setUp
    tearDown = fixture.CameraOrderUITests.tearDown
    prepare = fixture.CameraOrderUITests.prepare
    point = fixture.CameraOrderUITests.point
    press = fixture.CameraOrderUITests.press
    drag = fixture.CameraOrderUITests.drag

    def header_numbers(self, prefix):
        canvas = self.workspace.timeline
        labels = [(canvas.coords(item)[1], canvas.itemcget(item, 'text'))
            for item in canvas.find_all() if canvas.type(item) == 'text'
            and re.fullmatch(prefix+r'\d+', canvas.itemcget(item, 'text'))]
        return list(dict.fromkeys(text for _, text in sorted(labels)))

    def assert_labels(self, cameras):
        ws = self.workspace
        for number, camera in enumerate(cameras, 1):
            self.assertEqual(camera_number(ws.project, camera), number)
            self.assertTrue(ws.media_tree.item(f'camera_{camera}', 'text').startswith(f'机位 {number} · '))
            self.assertTrue(ws._label_camera(camera).startswith(f'机位 {number} · '))
            if camera in ws.camera_buttons:
                self.assertTrue(ws.camera_buttons[camera].cget('text').startswith(f'机位 {number} · '))
        self.assertEqual(self.header_numbers('V'), [f'V{n}' for n in range(1, len(cameras)+1)])
        self.assertEqual(self.header_numbers('A'), [f'A{n}' for n in range(1, len(cameras)+1)])
        self.assertEqual(self.errors, [])

    def test_drag_renumbers_all_surfaces_live_and_undo_redo_without_rewriting_edit(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        original = deepcopy(ws.project)
        self.drag('camera_3', 'camera_1')
        self.assert_labels([3, 1, 2])
        self.assertEqual(ws.project.clips, original.clips)
        self.assertEqual(ws.project.cuts, original.cuts)
        self.assertEqual(ws.project.camera_names, original.camera_names)
        ws.undo()
        self.assert_labels([1, 2, 3])
        ws.redo()
        self.assert_labels([3, 1, 2])

    def test_sparse_internal_ids_display_contiguous_library_numbers(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        ws.project.clips = [CameraClip('a', 31, 'a.mp4', 0., 10.),
                            CameraClip('b', 14, 'b.mp4', 0., 10.)]
        ws.project.camera_names = {31: '全景', 14: '侧面'}
        ws.project.cuts = [Cut(0., 31)]
        ws.project.video_stack_order = [31, 14]
        ws._refresh()
        self.assert_labels([31, 14])
        self.assertEqual(camera_id_for_number(ws.project, '1'), 31)
        self.assertEqual(ws.cut_camera_combo.cget('values'), ('1', '2'))
        self.assertEqual(ws.cut_tree.item('0', 'values')[1], '1')
        self.assertEqual(ws.project.camera_at(0.), 31)

    def test_cut_editor_numbers_resolve_to_the_correct_source_after_reorder(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        ws.cut_tree.selection_set('0')
        ws._select_cut()
        self.drag('camera_3', 'camera_1')
        self.assertEqual(ws.cut_camera_var.get(), '2')
        self.assertEqual(ws.cut_tree.item('0', 'values')[1], '2')
        ws.selected_cut = None
        ws.cut_time_var.set('00:00:03.000')
        ws.cut_camera_var.set('1')
        ws.edit_cut()
        self.assertEqual(ws.project.camera_at(3.), 3)
        self.assertEqual(ws.project.camera_at(0.), 1)
        self.assertEqual(self.errors, [])

    def test_grade_and_reference_numbers_do_not_target_previous_camera_id(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        self.drag('camera_3', 'camera_1')
        self.assertEqual(grade_target_label(ws, 3), '整机位 1')
        self.assertEqual(grade_target_from_label('整机位 1', ws), 3)
        ws.creative_panel.grade_scope_var.set('整机位 1')
        self.assertEqual(ws.creative_panel._scope(), 3)
        label = next(label for label, camera in ws._sync_reference_choices.items() if camera == 3)
        self.assertTrue(label.startswith('机位 1 · '))
        ws.sync_reference_var.set(label)
        ws._sync_reference_changed()
        self.assertEqual(ws.project.alignment_reference_camera, 3)
        with patch.object(ws, 'switch') as switch:
            ws._key(SimpleNamespace(widget=ws.timeline, state=0, keysym='1', char='1'))
            switch.assert_called_once_with(3)

    def test_deleting_a_camera_closes_number_gap_and_new_source_appends(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        self.drag('camera_3', 'camera_1')
        delete_source_clips(ws.project, ['one'])
        finalize_source_edit(ws.project)
        ws._refresh()
        self.assert_labels([3, 2])
        ws.project.clips.append(CameraClip('new', 14, 'new.mp4', 0., 10.))
        ws._refresh()
        self.assert_labels([3, 2, 14])
        for invalid in ('0', '-1', '4', 'bad'):
            with self.assertRaises(ValueError): camera_id_for_number(ws.project, invalid)

    def test_metadata_refresh_and_project_payload_keep_number_to_media_mapping(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        self.drag('camera_3', 'camera_1')
        ws.project.camera_names.clear()
        ws.project.clips[2].camera_model = 'ILCE-7M4'
        ws._refresh_camera_labels()
        self.assertTrue(ws._label_camera(3).startswith('机位 1 · '))
        payload = ws.project.to_payload('.')
        restored = type(ws.project).from_payload(payload, '.')
        self.assertEqual(restored.video_stack_order, [3, 1, 2])
        self.assertEqual([clip.camera for clip in restored.clips], [1, 2, 3])
        ws.project = restored
        ws._refresh()
        self.assert_labels([3, 1, 2])


if __name__ == '__main__': unittest.main()
