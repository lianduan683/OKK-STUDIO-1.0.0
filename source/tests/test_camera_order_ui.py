"""Camera-header drag ordering through real Tk bindings and project saves."""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from multicam_core import CameraClip, MulticamProject
from project_file import build_project_payload, read_project_file, write_project_file
from studio_state import restore_okk
from tests import test_timeline_drag_merge as fixture
from tests.test_transport_controls import RecordingPlayer


class CameraOrderUITests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown

    def prepare(self, *, collapsed=False):
        self.root.geometry('1280x1200+10000+10000')
        ws = self.workspace
        ws.project.clips.append(CameraClip('three', 3, 'three.mp4', 2., 8.))
        ws.project.camera_names = {1: '主机位', 2: '侧面', 3: '全景'}
        ws.project.alignment_reference_camera = 2
        ws._refresh()
        for parent in ws.media_tree.get_children():
            ws.media_tree.item(parent, open=not collapsed)
        self.root.update()

    def point(self, row, *, after=False):
        tree = self.workspace.media_tree
        if not tree.bbox(row): tree.see(row)
        self.root.update_idletasks()
        x, y, width, height = tree.bbox(row)
        return dict(x=x+min(width-5, 70), y=y+height*(.8 if after else .2))

    def drag(self, source, target, *, after=False, release=True):
        tree = self.workspace.media_tree
        start, end = self.point(source), self.point(target, after=after)
        self.press(start)
        tree.event_generate('<B1-Motion>', **end)
        if release: tree.event_generate('<ButtonRelease-1>', **end)
        self.root.update_idletasks()

    def press(self, point):
        # Distinct gestures must not be classified by Tk as double-clicks.
        self._event_time = getattr(self, '_event_time', 10000)+1000
        self.workspace.media_tree.event_generate('<ButtonPress-1>', time=self._event_time, **point)

    def assert_order(self, order):
        ws = self.workspace
        self.assertEqual(ws._timeline_camera_order(), order)
        self.assertEqual(ws.media_tree.get_children(), tuple(f'camera_{c}' for c in order))
        self.assertEqual(sorted(ws._timeline_track_bounds, key=lambda c: ws._timeline_track_bounds[c][0]), order)
        self.assertEqual(ws._timeline_audio_tracks(order), order)
        capacity = ws.camera_rows*ws.camera_columns
        visible = tuple(order[ws.camera_page*capacity:(ws.camera_page+1)*capacity])
        self.assertEqual(ws._visible_camera_ids, visible)
        self.assertEqual(tuple(ws.camera_frames), visible)
        for index, camera in enumerate(visible):
            cell = ws.camera_frames[camera].grid_info()
            self.assertEqual((cell['row'], cell['column']), divmod(index, ws.camera_columns))
        self.assertEqual(self.errors, [])

    def test_grid_drag_preserves_native_video_handles_and_selects_correct_camera(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        before = {camera: surface.winfo_id() for camera, surface in ws.camera_surfaces.items()}
        ws.player = RecordingPlayer()
        self.drag('camera_3', 'camera_1')
        self.assert_order([3, 1, 2])
        self.assertEqual({camera: surface.winfo_id() for camera, surface in ws.camera_surfaces.items()}, before)
        self.assertFalse(any(action == 'camera_windows' for action, _ in ws.player.commands))
        with patch.object(ws, 'switch') as switch:
            ws._key(SimpleNamespace(widget=ws.timeline, state=0, keysym='1', char='1'))
            switch.assert_called_once_with(3)
        with patch.object(ws, '_monitor_camera_click') as clicked:
            ws.camera_buttons[3].invoke()
            clicked.assert_called_once_with(3)
        self.assertIn('全景', ws.camera_buttons[3].cget('text'))
        ws.undo()
        self.assert_order([1, 2, 3])
        ws.redo()
        self.assert_order([3, 1, 2])

    def test_grid_pages_and_resize_anchor_follow_saved_order(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        for camera in range(4, 13):
            ws.project.clips.append(CameraClip(str(camera), camera, f'{camera}.mp4', 0., 10.))
        order = list(range(12, 0, -1))
        ws.project.video_stack_order = order
        ws._refresh()
        self.assert_order(order)
        ws._displayed_camera = 12
        ws.set_camera_grid(2, 2, persist=False)
        self.assertEqual(ws.camera_page, 0)
        self.assert_order(order)
        self.assertEqual(ws._visible_camera_ids, (12, 11, 10, 9))
        ws._change_camera_page(1)
        self.assertEqual(ws._visible_camera_ids, (8, 7, 6, 5))
        with patch.object(ws, 'switch') as switch:
            ws._key(SimpleNamespace(widget=ws.timeline, state=0, keysym='1', char='1'))
            switch.assert_called_once_with(8)
        ws._displayed_camera = 7
        ws.set_camera_grid(1, 3, persist=False)
        self.assertEqual(ws.camera_page, 1)
        self.assertEqual(ws._visible_camera_ids, (9, 8, 7))
        self.assert_order(order)

    def test_monitor_order_omits_empty_rows_and_retains_hidden_camera_monitor(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        ws.project.timeline_empty_tracks = [4]
        ws.project.video_stack_order = [4, 3, 1, 2]
        ws.project.video_stack_hidden = [3]
        ws._refresh()
        self.assertEqual(ws._timeline_camera_order(), [4, 3, 1, 2])
        self.assertEqual(ws._visible_camera_ids, (3, 1, 2))
        self.assertEqual(set(ws.camera_surfaces), {1, 2, 3})
        self.assertEqual(self.errors, [])

    def test_collapsed_camera_drag_up_is_one_undo_and_preserves_edit(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        original = deepcopy(ws.project)
        self.drag('camera_3', 'camera_1')
        self.assert_order([3, 1, 2])
        self.assertTrue(ws.dirty)
        self.assertEqual(len(ws.undo_stack), 1)
        expected = deepcopy(original)
        expected.video_stack_order = [3, 1, 2]
        self.assertEqual(ws.project, expected)
        self.assertEqual(ws.media_tree.selection(), ('camera_3',))
        self.assertTrue(all(not ws.media_tree.item(row, 'open') for row in ws.media_tree.get_children()))
        ws.undo()
        self.assertEqual(ws.project, original)
        self.assert_order([1, 2, 3])
        ws.redo()
        self.assert_order([3, 1, 2])

    def test_expanded_camera_drag_down_after_child_moves_whole_group(self):
        self.prepare()
        ws = self.workspace
        self.drag('camera_1', 'three')
        self.assert_order([2, 3, 1])
        self.assertEqual(ws.media_tree.get_children('camera_1'), ('one',))
        self.assertTrue(ws.media_tree.item('camera_1', 'open'))
        self.assertFalse(ws.project.video_stack_enabled)

    def test_before_and_after_header_are_distinct_insertions(self):
        self.prepare(collapsed=True)
        self.drag('camera_1', 'camera_3')
        self.assert_order([2, 1, 3])
        self.workspace.undo()
        self.drag('camera_1', 'camera_3', after=True)
        self.assert_order([2, 3, 1])

    def test_preview_is_view_only_and_escape_clears_marker_and_timer(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        original = deepcopy(ws.project)
        self.drag('camera_3', 'camera_1', release=False)
        self.assertEqual(ws.project, original)
        self.assertEqual(ws.undo_stack, [])
        self.assertTrue(ws._camera_drag_marker.place_info())
        self.assertIsNotNone(ws._camera_drag_scroll_after)
        ws.media_tree.focus_force()
        self.root.update()
        ws.media_tree.event_generate('<Escape>')
        self.assertIsNone(ws._media_drag)
        self.assertIsNone(ws._camera_drag_scroll_after)
        self.assertFalse(ws._camera_drag_marker.place_info())
        self.assertEqual(ws.project, original)

    def test_click_threshold_self_and_adjacent_drop_are_noops(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        original = deepcopy(ws.project)
        tree = ws.media_tree
        point = self.point('camera_1')
        self.press(point)
        tree.event_generate('<B1-Motion>', x=point['x'], y=point['y']+2)
        tree.event_generate('<ButtonRelease-1>', **point)
        self.drag('camera_1', 'camera_1', after=True)
        self.drag('camera_1', 'camera_2')
        self.assertEqual(ws.project, original)
        self.assertEqual(ws.undo_stack, [])

    def test_outside_or_heading_drop_cancels(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        original = deepcopy(ws.project)
        for x, y in ((-10, 50), (50, -10), (50, 5)):
            self.drag('camera_3', 'camera_1', release=False)
            ws.media_tree.event_generate('<ButtonRelease-1>', x=x, y=y)
            self.assertIsNone(ws._media_drag)
            self.assertEqual(ws.project, original)
            self.assertEqual(ws.undo_stack, [])

    def test_stale_project_order_or_busy_state_blocks_commit(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        for change in ('project', 'order', 'busy'):
            self.drag('camera_3', 'camera_1', release=False)
            if change == 'project': ws.project = deepcopy(ws.project)
            elif change == 'order': ws.project.video_stack_order = [2, 1, 3]
            else: ws.busy = True
            expected = deepcopy(ws.project)
            ws.media_tree.event_generate('<ButtonRelease-1>', **self.point('camera_1'))
            self.assertEqual(ws.project, expected)
            self.assertEqual(ws.undo_stack, [])
            ws.busy = False
            ws.project.video_stack_order = []

    def test_existing_timeline_order_hidden_empty_tracks_and_fold_state_survive(self):
        self.prepare()
        ws = self.workspace
        ws.project.timeline_empty_tracks = [4]
        ws.project.video_stack_order = [4, 3, 2, 1]
        ws.project.video_stack_hidden = [3]
        ws.project.video_stack_enabled = True
        ws._refresh()
        ws.media_tree.item('camera_2', open=False)
        self.root.update()
        self.assertEqual(ws.media_tree.get_children(), ('camera_3', 'camera_2', 'camera_1'))
        self.drag('camera_1', 'camera_3')
        self.assertEqual(ws._timeline_camera_order(), [4, 1, 3, 2])
        self.assertEqual(ws.project.video_stack_hidden, [3])
        self.assertTrue(ws.project.video_stack_enabled)
        self.assertEqual(ws.project.timeline_empty_tracks, [4])
        self.assertFalse(ws.media_tree.item('camera_2', 'open'))

    def test_both_project_formats_roundtrip_and_refresh_follow_order(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        self.drag('camera_3', 'camera_1')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'机位调序.multicam.json'
            ws.project.save(path)
            restored = MulticamProject.load(path)
            self.assertEqual(restored.video_stack_order, [3, 1, 2])
            self.assertFalse(restored.video_stack_enabled)
            self.root.multicam_workspace = ws
            path = Path(folder)/'机位调序.rtxproj'
            write_project_file(path, build_project_payload(self.root, project_path='', app_version='1.0.14'))
            ws.project = MulticamProject()
            restore_okk(self.root, read_project_file(path)['state']['okk_multicam'])
            self.assert_order([3, 1, 2])
            ws._refresh_camera_labels()
            ws._refresh(reconfigure=False)
            self.assert_order([3, 1, 2])

    def test_timeline_header_reorder_also_updates_camera_list(self):
        self.prepare()
        self.assertTrue(self.workspace.move_video_layer(3, 'top'))
        self.assert_order([3, 1, 2])

    def test_autoscroll_and_drop_below_last_row(self):
        self.prepare(collapsed=True)
        ws, tree = self.workspace, self.workspace.media_tree
        for camera in range(4, 35):
            ws.project.clips.append(CameraClip(str(camera), camera, f'{camera}.mp4', 0., 10.))
        ws._refresh()
        for parent in tree.get_children(): tree.item(parent, open=False)
        self.root.update()
        start = self.point('camera_1')
        self.press(start)
        tree.event_generate('<B1-Motion>', x=start['x'], y=tree.winfo_height()-5)
        before = tree.yview()[0]
        for _ in range(8):
            ws.after_cancel(ws._camera_drag_scroll_after)
            ws._camera_drag_autoscroll()
            self.root.update_idletasks()
        self.assertGreater(tree.yview()[0], before)
        ws._media_drag_cancel()
        # Blank space below all groups is a valid end insertion.
        ws.project.clips = ws.project.clips[:3]
        ws._refresh()
        self.root.update()
        start = self.point('camera_1')
        self.press(start)
        tree.event_generate('<B1-Motion>', x=start['x'], y=tree.winfo_height()-10)
        tree.event_generate('<ButtonRelease-1>', x=start['x'], y=tree.winfo_height()-10)
        self.assert_order([2, 3, 1])


if __name__ == '__main__': unittest.main()
