"""The editor file menu routes full legacy sessions to the session loader."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from multicam_core import MulticamProject
from tests import test_program_edit_ui as fixture


class ProjectOpenCompatibilityTests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def test_picker_offers_old_rtxproj_and_delegates_without_standalone_parser(self):
        path = str(self.folder/'旧工程.rtxproj')
        self.root._project_open = Mock()
        self.workspace.dirty = True
        with patch('multicam_ui.filedialog.askopenfilename', return_value=path) as picker, \
                patch('multicam_ui.MulticamProject.load') as standalone:
            self.workspace.open()
        self.assertIn('*.rtxproj', picker.call_args.kwargs['filetypes'][0][1])
        self.root._project_open.assert_called_once_with(path)
        standalone.assert_not_called()
        self.dialogs[3].assert_not_called()  # host owns the single discard prompt

    def test_startup_load_routes_case_insensitive_full_project_suffix(self):
        self.root._project_open = Mock()
        self.workspace.load_project('legacy.RTXPROJ')
        self.root._project_open.assert_called_once_with('legacy.RTXPROJ')

    def test_cancelling_picker_does_not_prompt_discard_or_change_project(self):
        before = self.workspace.project
        self.workspace.dirty = True
        with patch('multicam_ui.filedialog.askopenfilename', return_value=''):
            self.workspace.open()
        self.assertIs(self.workspace.project, before)
        self.dialogs[3].assert_not_called()

    def test_standalone_multicam_still_loads_in_the_editor(self):
        path = self.folder/'原多机位.multicam.json'
        self.workspace.project.fps = 29.97003
        self.workspace.project.save(path)
        self.workspace.project = MulticamProject()
        self.workspace.load_project(path)
        self.assertEqual(self.workspace.project.cameras, [1, 2])
        self.assertEqual(self.workspace.project_path, path)
        self.assertAlmostEqual(self.workspace._read_range()[2], 29.97003, places=10)
        self.assertEqual(self.callback_errors, [])

    def test_save_and_new_use_full_session_lifecycle_after_rtxproj_open(self):
        self.root.project_path = self.folder/'完整工程.rtxproj'
        self.root._project_save = Mock(return_value=True)
        self.root._project_new = Mock()
        self.assertTrue(self.workspace.save())
        self.root._project_save.assert_called_once()
        self.workspace.new()
        self.root._project_new.assert_called_once()

    def test_editing_full_session_shows_its_filename_in_editor(self):
        self.root.project_path = self.folder/'旧完整工程.rtxproj'
        self.workspace._mark_dirty()
        self.assertEqual(self.workspace.project_label.cget('text'), '旧完整工程.rtxproj *')

    def test_standalone_save_does_not_write_over_the_full_session(self):
        self.root.project_path = self.folder/'完整工程.rtxproj'
        self.root._project_save = Mock()
        standalone = self.folder/'剪辑.multicam.json'
        self.workspace.project_path = standalone
        self.assertTrue(self.workspace.save())
        self.root._project_save.assert_not_called()
        self.assertEqual(MulticamProject.load(standalone).cameras, [1, 2])


if __name__ == '__main__':
    unittest.main()
