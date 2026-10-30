"""Whole-session save and edit history retain audio choices and markers."""
import copy
import unittest

from multicam_core import MulticamProject
from project_file import build_project_payload, read_project_file, state_digest, write_project_file
from studio_state import collect_okk, restore_okk
from timeline_edits import delete_source_clips, finalize_source_edit
from tests import test_program_edit_ui as fixture


class TimelineAudioSessionTests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def test_deleted_video_and_audio_rows_survive_whole_project_save(self):
        from timeline_tracks import video_tracks, audio_tracks
        workspace = self.workspace
        self.root.multicam_workspace = workspace
        workspace._delete_tracks('V', [1])
        workspace._delete_tracks('A', [2])
        original = copy.deepcopy(workspace.project)
        path = self.folder/'轨道删除.rtxproj'
        write_project_file(path, build_project_payload(self.root, app_version='1.0.4'))
        workspace.project = MulticamProject()
        restore_okk(self.root, read_project_file(path)['state']['okk_multicam'])
        self.assertEqual(workspace.project, original)
        self.assertNotIn(1, video_tracks(workspace.project))
        self.assertNotIn(2, audio_tracks(workspace.project, video_tracks(workspace.project)))

    def test_rtxproj_roundtrip_preserves_independent_audio_and_timeline_switches(self):
        from timeline_link import move_source_audio
        workspace = self.workspace
        self.root.multicam_workspace = workspace
        workspace.project.timeline_link_enabled = workspace.project.timeline_snap_enabled = False
        move_source_audio(workspace.project, {workspace.project.clips[0].id}, 1.)
        original = copy.deepcopy(workspace.project)
        path = self.folder/'独立音视频.rtxproj'
        write_project_file(path, build_project_payload(self.root, app_version='1.0.2'))
        workspace.project = MulticamProject()
        restore_okk(self.root, read_project_file(path)['state']['okk_multicam'])
        self.assertEqual(workspace.project, original)
        self.assertFalse(workspace.timeline_snap_var.get())
        self.assertFalse(workspace.timeline_link_var.get())

    def test_rtxproj_roundtrip_preserves_names_audio_flags_and_markers(self):
        workspace = self.workspace
        self.root.multicam_workspace = workspace
        before = state_digest(collect_okk(self.root))
        workspace._set_timeline_audio_track(2, True, solo=True)
        workspace.add_timeline_marker(3.)
        workspace.project.set_camera_name(2, '侧面收音')
        path = self.folder/'音轨标记.rtxproj'
        write_project_file(path, build_project_payload(self.root, app_version='1.0.1'))
        self.assertNotEqual(state_digest(collect_okk(self.root)), before)
        workspace.project = MulticamProject()
        restore_okk(self.root, read_project_file(path)['state']['okk_multicam'])
        self.assertEqual(workspace.project.audio_track_enabled, {1: False, 2: True})
        self.assertEqual(workspace.project.timeline_markers, [3.])
        self.assertEqual(workspace.project.camera_names, {2: '侧面收音'})
        self.assertTrue(workspace.timeline_marker_items)
        self.assertEqual(self.callback_errors, [])

    def test_deleted_camera_drops_its_audio_state_and_keeps_independent_markers(self):
        project = self.workspace.project
        project.audio_track_enabled = {0: False, 1: True, 2: False}
        project.timeline_markers = [8.]
        delete_source_clips(project, ['b'])
        finalize_source_edit(project)
        self.assertEqual(project.audio_track_enabled, {0: False, 1: True})
        self.assertEqual(project.timeline_markers, [8.])


if __name__ == '__main__':
    unittest.main()
