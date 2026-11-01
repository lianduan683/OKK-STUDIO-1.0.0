"""Later imports stay outside an existing multicamera compound."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from multicam_core import MulticamProject
from multicam_outer import compound_duration, has_compound, outer_cameras
from tests import test_timeline_drag_merge as fixture


class CompoundImportTests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown

    def create_compound(self, expanded=False):
        w = self.workspace
        w.set_multicam_timeline_view(True)
        if expanded: w.set_multicam_timeline_view(False)
        self.root.update_idletasks()
        return w

    def info(self, path='later.mp4', duration=4, audio=True, alpha=False):
        return SimpleNamespace(path=path, duration=duration, fps=30, has_audio=audio, has_alpha=alpha)

    def import_camera(self, groups):
        w = self.workspace
        w._handle_camera_import_job('camera_import_ready', (w.project, groups))
        self.assertEqual(self.errors, [])

    def test_added_camera_is_outer_and_original_compound_is_unchanged(self):
        w = self.create_compound()
        before = deepcopy(w.project)
        self.import_camera([(3, [self.info()])])
        self.assertEqual(w.project.multicam_outer_order, [3, 0])
        self.assertEqual(w.project.clips[:2], before.clips)
        self.assertEqual(w.project.cuts, before.cuts)
        self.assertEqual(w.project.master_audio, before.master_audio)
        self.assertEqual(w.project.alignment_reference_camera, before.alignment_reference_camera)
        self.assertEqual(compound_duration(w.project), 10)
        self.assertEqual(w._timeline_camera_order(), [3, 0])
        w.project.validate()

    def test_multiple_folders_and_parts_become_stable_outer_tracks(self):
        w = self.create_compound()
        self.import_camera([(3, [self.info('a.mp4', 3), self.info('b.mp4', 5)]),
                            (4, [self.info('c.mp4', 20)])])
        self.assertEqual(w.project.multicam_outer_order, [3, 4, 0])
        self.assertEqual([(c.camera, c.start) for c in w.project.clips[2:]], [(3, 0), (3, 3), (4, 0)])
        self.assertEqual(compound_duration(w.project), 10)
        self.assertEqual(w.project.duration, 20)
        self.assertIsNone(w.project.alignment_reference_camera)

    def test_expanded_compound_keeps_new_camera_outside_and_survives_roundtrip(self):
        w = self.create_compound(expanded=True)
        self.assertTrue(has_compound(w.project))
        payload = w.project.to_payload('.')
        self.assertEqual(payload['version'], 21)
        w.project = MulticamProject.from_payload(payload, '.')
        w._refresh()
        self.import_camera([(3, [self.info()])])
        self.assertEqual(outer_cameras(w.project), {3})
        w.set_multicam_timeline_view(True)
        self.assertEqual(w._timeline_camera_order(), [3, 0])

    def test_camera_import_is_one_undoable_change(self):
        w = self.create_compound()
        before = deepcopy(w.project)
        count = len(w.undo_stack)
        self.import_camera([(3, [self.info()]), (4, [self.info('two.mp4')])])
        after = deepcopy(w.project)
        self.assertEqual(len(w.undo_stack), count+1)
        w.undo()
        self.assertEqual(w.project, before)
        w.redo()
        self.assertEqual(w.project, after)

    def test_explicit_append_to_existing_internal_camera_remains_supported(self):
        w = self.create_compound()
        self.import_camera([(1, [self.info()])])
        self.assertEqual(outer_cameras(w.project), set())
        self.assertEqual((w.project.clips[-1].camera, w.project.clips[-1].start), (1, 10))

    def test_plain_timeline_import_before_compound_still_creates_internal_cameras(self):
        w = self.workspace
        self.import_camera([(3, [self.info()])])
        self.assertFalse(has_compound(w.project))
        self.assertEqual(outer_cameras(w.project), set())
        self.assertEqual(w.project.master_audio, 'later.mp4')

    def test_timeline_new_files_stay_outer_in_both_views(self):
        w = self.create_compound()
        before = deepcopy(w.project.cuts)
        w._finish_timeline_import((w.project, 2, None, [self.info('collapsed.mp4')]))
        self.assertIn(w.project.clips[-1].camera, outer_cameras(w.project))
        w.set_multicam_timeline_view(False)
        w._finish_timeline_import((w.project, 3, None, [self.info('expanded.mp4')]))
        self.assertIn(w.project.clips[-1].camera, outer_cameras(w.project))
        self.assertEqual(w.project.cuts, before)
        self.assertEqual(len(outer_cameras(w.project)), 2)

    def test_collapsed_drop_never_moves_existing_source_track_outside(self):
        w = self.create_compound()
        before = deepcopy(w.project.clips)
        w._finish_timeline_import((w.project, 2, 1, [self.info()]))
        self.assertNotEqual(w.project.clips[-1].camera, 1)
        self.assertEqual(outer_cameras(w.project), {w.project.clips[-1].camera})
        self.assertEqual(w.project.clips[:2], before)

    def test_inserted_track_in_expanded_compound_is_outer(self):
        w = self.create_compound(expanded=True)
        track = w.insert_video_track(('top', None))
        self.assertIn(track, outer_cameras(w.project))
        w._finish_timeline_import((w.project, 1, track, [self.info()]))
        self.assertEqual(w.project.clips[-1].camera, track)
        self.assertEqual(outer_cameras(w.project), {track})

    def test_deleting_last_outer_track_retains_compound_for_later_import(self):
        w = self.create_compound()
        self.import_camera([(3, [self.info()])])
        self.assertTrue(w._delete_tracks('V', [3]))
        # Picture-only track deletion deliberately preserves its independent audio.
        self.assertTrue(w._delete_tracks('A', [3]))
        self.assertEqual(w.project.multicam_outer_order, [0])
        self.assertFalse(w.project.video_stack_enabled)
        self.import_camera([(4, [self.info('again.mp4')])])
        self.assertEqual(outer_cameras(w.project), {4})

    def test_legacy_collapsed_project_is_recognized_and_cancel_does_not_commit(self):
        w = self.workspace
        w.project.timeline_multicam_collapsed = True
        self.assertEqual(w.project.multicam_outer_order, [])
        before = deepcopy(w.project)
        self.root.cancel_event.set()
        self.import_camera([(3, [self.info()])])
        self.assertEqual(w.project, before)
        self.root.cancel_event.clear()
        self.import_camera([(3, [self.info()])])
        self.assertEqual(outer_cameras(w.project), {3})

    def test_empty_collapsed_view_does_not_put_first_import_outside(self):
        w = self.workspace
        w.project = MulticamProject()
        w.set_multicam_timeline_view(True)
        self.import_camera([(1, [self.info()])])
        self.assertEqual(outer_cameras(w.project), set())
        self.assertEqual(w.project.master_audio, 'later.mp4')

    def test_compound_layer_toggle_retains_inner_layer_state_after_import(self):
        w = self.create_compound()
        w.video_stack_var.set(True)
        w._toggle_video_stack()
        self.assertTrue(w.project.multicam_inner_stack)
        self.assertTrue(w.project.video_stack_enabled)
        self.import_camera([(3, [self.info()])])
        self.assertTrue(w.project.multicam_inner_stack)

    def test_batch_add_button_uses_the_same_outer_import_rules(self):
        w = self.create_compound()
        before = deepcopy(w.project.cuts)
        w.jobs.put(('batch_import_ready', [(3, self.info()), (4, self.info('two.mp4'))]))
        w._poll()
        self.assertEqual(outer_cameras(w.project), {3, 4})
        self.assertEqual(w.project.cuts, before)
        self.assertEqual(w.project.master_audio, '')


if __name__ == '__main__':
    unittest.main()
