"""Regression for independent track removal, menus and persistent layout."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from multicam_core import MulticamProject, CameraClip, Cut
from timeline_audio import source_audio_clips, audio_sources
from timeline_tracks import remove_tracks, video_tracks, audio_tracks, track_empty
from tests import test_timeline_drag_merge as fixture


class TrackRemovalModelTests(unittest.TestCase):
    def project(self):
        return MulticamProject(clips=[CameraClip('one', 1, 'one.mov', 0., 10., has_audio=True),
                                     CameraClip('two', 2, 'two.mov', 2., 10., has_audio=False)],
                               cuts=[Cut(0., 1)], timeline_empty_tracks=[3])

    def test_delete_video_keeps_audio_at_original_time_even_when_linked(self):
        project = self.project()
        before = audio_sources(project)
        remove_tracks(project, 'V', [1])
        self.assertEqual([clip.id for clip in project.clips], ['two'])
        self.assertEqual(audio_sources(project), before)
        self.assertNotIn(1, video_tracks(project))
        self.assertIn(1, audio_tracks(project, video_tracks(project)))
        self.assertTrue(project.timeline_link_enabled)

    def test_delete_audio_keeps_picture_and_all_other_sound_states(self):
        project = self.project()
        before = deepcopy(project.clips)
        remove_tracks(project, 'A', [1])
        self.assertEqual(project.clips, before)
        self.assertNotIn(1, audio_tracks(project, video_tracks(project)))
        self.assertEqual([clip.id for clip in source_audio_clips(project)], ['two'])
        self.assertIn(1, video_tracks(project))

    def test_blank_audio_cleanup_retains_video_with_no_audio(self):
        project = self.project()
        tracks = audio_tracks(project, video_tracks(project))
        empty = [track for track in tracks if track_empty(project, 'A', track)]
        self.assertEqual(empty, [2, 3])
        remove_tracks(project, 'A', empty)
        self.assertEqual(audio_tracks(project, video_tracks(project)), [1])
        self.assertEqual(video_tracks(project), [1, 2, 3])
        self.assertEqual(len(project.clips), 2)

    def test_removed_rows_persist_in_standalone_and_embedded_payload(self):
        project = self.project()
        remove_tracks(project, 'V', [1, 3])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'edit.json'
            project.save(path)
            restored = MulticamProject.load(path)
            self.assertEqual(video_tracks(restored), [2])
            self.assertIn(1, audio_tracks(restored, video_tracks(restored)))
            payload = project.to_payload(folder)
            self.assertEqual(payload['version'], 13)
            self.assertEqual(MulticamProject.from_payload(payload, folder).timeline_removed_tracks,
                             project.timeline_removed_tracks)

    def test_deleting_master_audio_does_not_enable_muted_camera(self):
        project = self.project()
        project.master_audio, project.master_duration = 'master.wav', 20.
        remove_tracks(project, 'A', [0])
        self.assertEqual(project.master_audio, '')
        self.assertEqual(audio_sources(project), [])

    def test_invalid_removed_track_settings_rejected(self):
        for value in ('V1', [True], ['V0'], ['A-1'], ['V1', 'V1'], ['V01'], ['X1']):
            project = self.project()
            project.timeline_removed_tracks = value
            with self.assertRaises(ValueError): project.validate()


class TrackRemovalUITests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown

    def event(self, kind, track, header=True):
        w = self.workspace
        bounds = w._timeline_track_bounds if kind == 'V' else w._timeline_audio_track_bounds
        top, bottom = bounds[track]
        return SimpleNamespace(x=8 if header else w._timeline_left+15,
            y=(top+bottom)/2-w.timeline.canvasy(0), x_root=0, y_root=0)

    def labels(self, menu):
        return [menu.entrycget(i, 'label') for i in range(menu.index('end')+1)
                if menu.type(i) != 'separator']

    def test_video_and_audio_header_menus_have_both_commands(self):
        w = self.workspace
        for kind, attribute in [('V', 'video_stack_context_menu'), ('A', 'audio_track_context_menu')]:
            with patch('tkinter.Menu.tk_popup'): w.timeline_context(self.event(kind, 1))
            labels = self.labels(getattr(w, attribute))
            self.assertIn('删除轨道', labels)
            self.assertIn('删除空白轨道', labels)

    def test_deletion_is_one_undo_edit_and_redo_removes_row_again(self):
        w = self.workspace
        original = deepcopy(w.project)
        self.assertTrue(w._delete_tracks('V', [1]))
        self.assertNotIn(1, w._timeline_track_bounds)
        self.assertIn(1, w._timeline_audio_track_bounds)
        self.assertEqual(len(w.undo_stack), 1)
        w.undo()
        self.assertEqual(w.project, original)
        w.redo()
        self.assertNotIn(1, w._timeline_track_bounds)
        self.assertEqual(self.errors, [])

    def test_empty_cleanup_is_one_edit_and_preserves_other_type(self):
        w = self.workspace
        w.insert_video_track()
        original = deepcopy(w.project)
        count = len(w.undo_stack)
        self.assertTrue(w._delete_tracks('A', w._empty_timeline_tracks('A')))
        self.assertEqual(len(w.undo_stack), count+1)
        self.assertIn(3, w._timeline_track_bounds)
        self.assertNotIn(3, w._timeline_audio_track_bounds)
        w.undo()
        self.assertEqual(w.project, original)

    def test_insert_after_removal_creates_visible_new_track(self):
        w = self.workspace
        removed = w.insert_video_track()
        w.remove_empty_video_track(removed)
        new = w.insert_video_track()
        self.assertNotEqual(new, removed)
        self.assertIn(new, w._timeline_track_bounds)

    def test_layer_moves_skip_removed_picture_rows_with_surviving_audio(self):
        w = self.workspace
        w.insert_video_track()
        w._delete_tracks('V', [1])
        self.assertEqual(w._timeline_camera_order(), [3, 2])
        self.assertTrue(w.move_video_layer(2, -1))
        self.assertEqual(w._timeline_camera_order(), [2, 3])
