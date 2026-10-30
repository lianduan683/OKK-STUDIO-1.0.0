"""Failed sync results stay visible and camera folding remains view-only."""
from copy import deepcopy
import unittest

from editor_core import AlignmentResult
from tests import test_program_edit_ui as fixture


class CameraListUITests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def align_partial(self):
        ws = self.workspace
        ws.project.clips[1].start = 6.
        ws._finish_alignment([('a', AlignmentResult(-2., .9, 200, '可靠匹配')),
                              ('b', AlignmentResult(-8., .1, 200, '匹配可信度不足'))])
        self.root.update()
        return ws

    def test_fold_button_compacts_to_camera_headers_and_does_not_edit_project(self):
        ws = self.workspace
        before = deepcopy(ws.project)
        dirty, history = ws.dirty, len(ws.undo_stack)
        ws.media_tree.selection_set('b')
        ws.camera_fold_button.invoke()
        self.assertTrue(all(not ws.media_tree.item(parent, 'open') for parent in ws.media_tree.get_children()))
        self.assertEqual(ws.media_tree.selection(), ('camera_2',))
        self.assertEqual(ws.camera_fold_button.cget('text'), '全部展开')
        ws._refresh(reconfigure=False)
        self.assertTrue(all(not ws.media_tree.item(parent, 'open') for parent in ws.media_tree.get_children()))
        ws.camera_fold_button.invoke()
        self.assertTrue(all(ws.media_tree.item(parent, 'open') for parent in ws.media_tree.get_children()))
        self.assertEqual(ws.project, before)
        self.assertEqual((ws.dirty, len(ws.undo_stack)), (dirty, history))

    def test_refresh_and_camera_metadata_keep_individual_fold_states(self):
        ws = self.workspace
        ws.media_tree.item('camera_1', open=False)
        ws.media_tree.item('camera_2', open=True)
        ws._refresh(reconfigure=False)
        ws._refresh_camera_labels()
        self.assertFalse(ws.media_tree.item('camera_1', 'open'))
        self.assertTrue(ws.media_tree.item('camera_2', 'open'))

    def test_partial_failure_opens_full_list_and_marks_camera_even_when_collapsed(self):
        ws = self.align_partial()
        self.assertEqual(ws.project.clips[1].start, 6.)
        self.assertTrue(ws._sync_review_window.winfo_exists())
        self.assertEqual(ws._sync_review_tree.get_children(), ('b',))
        values = ws._sync_review_tree.item('b', 'values')
        self.assertIn('机位 2', values[0])
        self.assertEqual(values[1], 'b.mp4')
        self.assertIn('可信度不足', values[3])
        self.assertTrue(ws.media_tree.item('b', 'text').startswith('[未对齐]'))
        self.assertIn('1 段待检查', ws.media_tree.item('camera_2', 'text'))
        ws.camera_fold_button.invoke()
        self.assertIn('review', ws.media_tree.item('camera_2', 'tags'))
        self.assertEqual(ws.sync_review_summary.get(), '待检查 1 段')
        self.assertEqual(self.callback_errors, [])

    def test_report_locates_exact_clip_and_opens_its_camera(self):
        ws = self.align_partial()
        ws.camera_fold_button.invoke()
        ws._sync_review_tree.selection_set('b')
        ws._locate_sync_review()
        self.assertEqual(ws.media_tree.selection(), ('b',))
        self.assertTrue(ws.media_tree.item('camera_2', 'open'))
        self.assertFalse(ws.media_tree.item('camera_1', 'open'))
        self.assertEqual(ws.current, 6.)
        self.assertIn('b.mp4', ws.status.get())

    def test_reopen_report_and_successful_resync_remove_old_pending_rows(self):
        ws = self.align_partial()
        ws._close_sync_review()
        ws.sync_review_button.invoke()
        self.assertEqual(ws._sync_review_tree.get_children(), ('b',))
        window = ws._sync_review_window
        ws._finish_alignment([('b', AlignmentResult(-6., .95, 200, '可靠匹配'))])
        self.assertFalse(window.winfo_exists())
        self.assertNotIn('待检查', ws.media_tree.item('camera_2', 'text'))
        self.assertNotIn('[未对齐]', ws.media_tree.item('b', 'text'))
        self.assertEqual(ws.sync_review_summary.get(), '已对齐 2 段')

    def test_unattempted_clips_are_not_listed_as_failed(self):
        ws = self.workspace
        ws._finish_alignment([('a', AlignmentResult(0., .05, 200, '声音不足'))])
        self.assertEqual(ws._sync_review_tree.get_children(), ('a',))
        self.assertNotIn('未对齐', ws.media_tree.item('b', 'text'))
        self.assertNotIn('待检查', ws.media_tree.item('camera_2', 'text'))

    def test_nonfinite_result_is_not_marked_synced_or_applied(self):
        ws = self.workspace
        before = ws.project.clips[1].start
        ws._finish_alignment([('b', AlignmentResult(float('nan'), .99, 200, 'invalid'))])
        self.assertEqual(ws.project.clips[1].start, before)
        self.assertEqual(ws._sync_clip_state('b'), 'review')
        self.assertIn('同步结果无效', ws._sync_review_tree.item('b', 'values')[3])
        self.assertEqual(self.callback_errors, [])

    def test_undo_clears_pending_report_along_with_alignment_results(self):
        ws = self.align_partial()
        window = ws._sync_review_window
        ws.undo()
        self.assertEqual(ws.sync_results, {})
        self.assertFalse(window.winfo_exists())
        self.assertEqual(ws.sync_review_summary.get(), '')

    def test_full_worker_failure_lists_attempted_clips_without_changing_project(self):
        ws = self.workspace
        before = deepcopy(ws.project)
        ws._alignment_attempt_ids = ('b',)
        ws._record_alignment_failure('素材没有可用音频')
        self.assertEqual(ws._sync_review_tree.get_children(), ('b',))
        self.assertIn('素材没有可用音频', ws._sync_review_tree.item('b', 'values')[3])
        self.assertEqual(ws.project, before)
        self.assertFalse(ws.dirty)
        self.assertEqual(ws.undo_stack, [])

    def test_cancel_does_not_create_a_failure_report(self):
        ws = self.workspace
        self.root.cancel_event.set()
        ws._finish_alignment([('b', AlignmentResult(0., .1, 200, 'cancelled'))])
        self.assertEqual(ws.sync_results, {})
        self.assertIsNone(getattr(ws, '_sync_review_window', None))
        self.assertEqual(ws.undo_stack, [])


if __name__ == '__main__': unittest.main()
