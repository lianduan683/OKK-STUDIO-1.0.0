"""Camera names survive both project formats and native right-click edits."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from multicam_core import CameraClip, Cut, MulticamProject
from project_file import (build_project_payload, collect_state, read_project_file,
                          state_digest, write_project_file)
from studio_state import restore_okk
from timeline_edits import delete_source_clips, finalize_source_edit

try:
    from . import test_program_edit_ui as fixture
except ImportError:
    import test_program_edit_ui as fixture


def project():
    return MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', 0., 12.),
                                  CameraClip('b', 2, 'b.mp4', 0., 12.)],
                           cuts=[Cut(0., 1)])


class CameraNameModelTests(unittest.TestCase):
    def test_standalone_roundtrip_and_names_do_not_change_edit(self):
        model = project()
        before = copy.deepcopy(model)
        model.set_camera_name(1, '  主机位 · 正面 🎥  ')
        model.set_camera_name(2, '侧面特写')
        self.assertEqual(model.camera_names, {1: '主机位 · 正面 🎥', 2: '侧面特写'})
        self.assertEqual(model.clips, before.clips)
        self.assertEqual(model.cuts, before.cuts)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'重命名.multicam.json'
            model.save(path)
            restored = MulticamProject.load(path)
            self.assertEqual(restored.camera_names, model.camera_names)
            self.assertTrue(all(type(key) is int for key in restored.camera_names))
            self.assertEqual(json.loads(path.read_text('utf-8'))['version'], 10)

    def test_old_project_without_names_still_loads_and_keeps_its_version(self):
        payload = project().to_payload('.')
        self.assertNotIn('camera_names', payload)
        self.assertEqual(payload['version'], 8)
        self.assertEqual(MulticamProject.from_payload(payload, '.').camera_names, {})

    def test_rejects_invalid_names_without_mutation(self):
        model = project()
        for name in ('', '   ', 'a\nb', 'a\rb', 'a\x00b', 'a\tb', 'a'*101, None, 3):
            with self.subTest(name=name), self.assertRaises(ValueError):
                model.set_camera_name(1, name)
        with self.assertRaises(ValueError): model.set_camera_name(99, '不存在')
        self.assertEqual(model.camera_names, {})

    def test_rejects_corrupt_serialized_names(self):
        payload = project().to_payload('.')
        for names in ([], {'0': 'x'}, {'01': 'x'}, {'True': 'x'}, {'１': 'x'},
                      {'1': ''}, {'1': 2}, {'1': 'a\nb'}):
            with self.subTest(names=names), self.assertRaises(ValueError):
                MulticamProject.from_payload(dict(payload, camera_names=names), '.')

    def test_deleting_last_clip_clears_name_before_camera_id_is_reused(self):
        model = project()
        model.set_camera_name(2, '旧机位')
        delete_source_clips(model, ['b'])
        finalize_source_edit(model)
        self.assertNotIn(2, model.camera_names)
        model.clips.append(CameraClip('new', 2, 'new.mp4', 0., 12.))
        self.assertNotIn(2, model.camera_names)

    def test_empty_track_keeps_its_name(self):
        model = project()
        model.timeline_empty_tracks = [2]
        model.set_camera_name(2, '保留轨道')
        delete_source_clips(model, ['b'])
        finalize_source_edit(model)
        self.assertEqual(model.camera_names, {2: '保留轨道'})
        model.validate()


class CameraRenameWorkspaceTests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def context(self, row):
        tree = self.workspace.media_tree
        self.root.update()
        bounds = tree.bbox(row)
        self.assertTrue(bounds)
        x, y, width, height = bounds
        with patch('multicam_ui.tk.Menu.tk_popup') as popup:
            result = self.workspace._camera_context(SimpleNamespace(
                x=x+10, y=y+height//2, x_root=10000+x, y_root=10000+y))
        return result, popup

    def test_right_click_renames_clicked_camera_and_updates_labels_with_undo(self):
        workspace = self.workspace
        before = copy.deepcopy(workspace.project)
        workspace.media_tree.selection_set('camera_1')
        result, popup = self.context('camera_2')
        self.assertEqual(result, 'break')
        popup.assert_called_once()
        self.assertTrue(workspace.media_tree.bind('<Button-3>'))
        self.assertEqual(workspace.media_tree.selection(), ('camera_2',))
        with patch('multicam_ui.simpledialog.askstring', return_value='侧面特写'):
            workspace.camera_context_menu.invoke(0)
        self.assertEqual(workspace.project.camera_names, {2: '侧面特写'})
        self.assertTrue(workspace.dirty)
        self.assertIn('侧面特写', workspace.media_tree.item('camera_2', 'text'))
        self.assertIn('侧面特写', workspace.camera_buttons[2].cget('text'))
        self.assertIn('侧面特写', '\n'.join(workspace.timeline.itemcget(item, 'text')
            for item in workspace.timeline.find_all() if workspace.timeline.type(item) == 'text'))
        self.assertEqual(workspace.project.clips, before.clips)
        self.assertEqual(workspace.project.cuts, before.cuts)
        workspace.undo()
        self.assertEqual(workspace.project, before)
        workspace.redo()
        self.assertEqual(workspace.project.camera_names, {2: '侧面特写'})
        self.assertEqual(self.callback_errors, [])

    def test_cancel_and_unchanged_name_do_not_create_undo_entries(self):
        workspace = self.workspace
        with patch('multicam_ui.simpledialog.askstring', return_value=None):
            workspace.rename_camera(1)
        self.assertFalse(workspace.dirty)
        self.assertEqual(workspace.undo_stack, [])
        workspace.project.set_camera_name(1, '正面')
        with patch('multicam_ui.simpledialog.askstring', return_value='正面'):
            workspace.rename_camera(1)
        self.assertEqual(workspace.undo_stack, [])

    def test_blank_name_is_rejected_then_valid_name_can_be_entered(self):
        workspace = self.workspace
        with patch('multicam_ui.simpledialog.askstring', side_effect=['   ', '主机位']):
            workspace.rename_camera(1)
        self.dialogs[0].assert_called_once()
        self.assertEqual(workspace.project.camera_names, {1: '主机位'})
        self.assertEqual(len(workspace.undo_stack), 1)

    def test_reset_is_undoable_and_restores_metadata_label(self):
        workspace = self.workspace
        workspace.project.set_camera_name(1, '正面')
        workspace.reset_camera_name(1)
        self.assertEqual(workspace.project.camera_names, {})
        self.assertIn('未知相机', workspace.media_tree.item('camera_1', 'text'))
        workspace.undo()
        self.assertEqual(workspace.project.camera_names, {1: '正面'})

    def test_clip_opens_proxy_menu_and_empty_space_does_not_open_menu(self):
        _, popup = self.context('a')
        popup.assert_called_once()
        menu = self.workspace.camera_context_menu
        labels = [menu.entrycget(index, 'label') for index in range(menu.index('end')+1)]
        self.assertIn('转代理…', labels)
        self.assertNotIn('重命名机位…', labels)
        with patch('multicam_ui.tk.Menu.tk_popup') as popup:
            self.workspace._camera_context(SimpleNamespace(y=100000))
        popup.assert_not_called()

    def test_busy_state_disables_actions_and_does_not_show_rename_dialog(self):
        workspace = self.workspace
        workspace.busy = True
        self.context('camera_1')
        self.assertEqual(workspace.camera_context_menu.entrycget(0, 'state'), 'disabled')
        with patch('multicam_ui.simpledialog.askstring') as dialog:
            workspace.rename_camera(1)
        dialog.assert_not_called()
        self.assertEqual(workspace.undo_stack, [])
        workspace.busy = False

    def test_metadata_refresh_and_reference_selection_preserve_name_and_id(self):
        workspace = self.workspace
        workspace.project.alignment_reference_camera = 1
        workspace.project.clips[0].camera_make = 'Sony'
        workspace.project.clips[0].camera_model = 'ILCE-7M4'
        with patch('multicam_ui.simpledialog.askstring', return_value='正面主机位'):
            workspace.rename_camera(1)
        workspace._refresh_camera_labels()
        self.assertIn('正面主机位', workspace.program_var.get())
        self.assertEqual(workspace._sync_reference_choices[workspace.sync_reference_var.get()], 1)
        self.assertEqual(workspace.project.clips[0].camera_model, 'ILCE-7M4')

    def test_whole_session_file_roundtrip_and_dirty_digest_include_names(self):
        workspace = self.workspace
        self.root.multicam_workspace = workspace
        before = state_digest(collect_state(self.root))
        with patch('multicam_ui.simpledialog.askstring', return_value='正面主机位'):
            workspace.rename_camera(1)
        payload = build_project_payload(self.root, project_path='', app_version='1.0.1')
        self.assertNotEqual(state_digest(payload['state']), before)
        path = self.folder/'重命名.rtxproj'
        write_project_file(path, payload)
        workspace.project = MulticamProject()
        restore_okk(self.root, read_project_file(path)['state']['okk_multicam'])
        self.assertEqual(workspace.project.camera_names, {1: '正面主机位'})
        self.assertIn('正面主机位', workspace.media_tree.item('camera_1', 'text'))
        self.assertEqual(self.callback_errors, [])


if __name__ == '__main__':
    unittest.main()
