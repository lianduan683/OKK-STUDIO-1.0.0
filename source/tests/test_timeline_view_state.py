"""Timeline navigation survives standalone, session, and autosave round trips."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from editor_core import format_time
from multicam_core import CameraClip, Cut, MulticamProject
from project_file import read_project_file, state_digest
from studio_state import collect_okk, restore_okk
from timeline_view import validate_timeline_view
from . import test_timeline_drag_merge as fixture
from . import test_studio_session as session_fixture


class TimelineViewModelTests(unittest.TestCase):
    def test_optional_state_roundtrips_and_old_versions_keep_defaults(self):
        project = MulticamProject()
        payload = project.to_payload('.')
        for version in range(1, 19):
            old = dict(payload, version=version)
            self.assertEqual(MulticamProject.from_payload(old, '.').timeline_view, {})
        project.timeline_view = dict(position=15., zoom=16., x_fraction=.4, y_fraction=.2)
        payload = project.to_payload('.')
        original = deepcopy(payload)
        self.assertEqual(payload['version'], 19)
        self.assertEqual(MulticamProject.from_payload(payload, '.').timeline_view, project.timeline_view)
        self.assertEqual(payload, original)

    def test_view_does_not_change_edit_equality_or_content_digest(self):
        first = MulticamProject()
        second = deepcopy(first)
        second.timeline_view = dict(position=0., zoom=32., x_fraction=.5)
        self.assertEqual(first, second)
        before = {'okk_multicam': dict(project=first.to_payload('.'))}
        after = {'okk_multicam': dict(project=second.to_payload('.'))}
        snapshot = deepcopy(after)
        self.assertEqual(state_digest(before), state_digest(after))
        self.assertNotEqual(state_digest(before, include_view=True), state_digest(after, include_view=True))
        self.assertEqual(after, snapshot)
        after['okk_multicam']['project']['fps'] = 60
        self.assertNotEqual(state_digest(before), state_digest(after))

    def test_bad_navigation_metadata_is_rejected(self):
        for value in (None, [], {'unknown': 1}, {'position': -1}, {'position': '3'},
                      {'position': float('nan')}, {'zoom': float('inf')}, {'zoom': True},
                      {'zoom': 0}, {'zoom': 10001}, {'x_fraction': -.1}, {'y_fraction': 1.1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_timeline_view(value)


class TimelineViewWorkspaceTests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown

    def long_timeline(self):
        ws = self.workspace
        ws.project = MulticamProject(
            clips=[CameraClip(str(camera), camera, str(Path(f'camera-{camera}.mp4').resolve()), 0., 3600.)
                   for camera in range(1, 13)], cuts=[Cut(0., 1)])
        ws.zoom = 16.
        ws.current = 1200.
        ws._refresh()
        self.root.update()
        ws.timeline.xview_moveto(.3)
        ws.timeline.yview_moveto(.2)
        self.root.update()
        return ws

    def assert_view(self, expected):
        ws = self.workspace
        self.root.update()
        self.assertEqual(ws.current, expected['position'])
        self.assertEqual(ws.zoom, expected['zoom'])
        self.assertEqual(ws.seek_var.get(), format_time(ws.current))
        self.assertAlmostEqual(ws.timeline.xview()[0], expected['x_fraction'], delta=.001)
        self.assertAlmostEqual(ws.timeline.yview()[0], expected['y_fraction'], delta=.01)
        self.assertFalse(ws.playing)
        self.assertEqual(self.errors, [])

    def test_standalone_save_and_open_restore_cursor_zoom_and_both_scrollbars(self):
        ws = self.long_timeline()
        expected = ws._capture_timeline_view()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'navigation.multicam.json'
            ws.project_path = path
            self.assertTrue(ws.save())
            self.assertEqual(MulticamProject.load(path).timeline_view, expected)
            ws.new()
            ws.current = 1.
            ws.zoom = 1.
            ws.timeline.xview_moveto(0.)
            with patch('multicam_ui.messagebox.showwarning'):
                ws.load_project(path)
            self.assert_view(expected)

    def test_switching_away_remembers_navigation_without_saving_other_edits(self):
        ws = self.long_timeline()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'latest.multicam.json'
            ws.project_path = path
            self.assertTrue(ws.save())
            saved = MulticamProject.load(path)
            ws.current = 1500.
            ws.change_zoom(2)
            ws.project.clips[0].start = 1.
            ws.dirty = True
            with patch('multicam_ui.messagebox.askyesnocancel', return_value=False):
                ws.new()
            restored = MulticamProject.load(path)
            self.assertEqual(restored.clips, saved.clips)
            self.assertEqual(restored.timeline_view['position'], 1500.)
            self.assertEqual(restored.timeline_view['zoom'], 32.)
            with patch('multicam_ui.messagebox.showwarning'):
                ws.load_project(path)
            self.root.update()
            self.assertEqual((ws.current, ws.zoom), (1500., 32.))

    def test_embedded_state_restores_navigation_without_changing_edits(self):
        ws = self.long_timeline()
        self.root.multicam_workspace = ws
        snapshot = collect_okk(self.root)
        expected = snapshot['project']['timeline_view']
        original = deepcopy(ws.project)
        ws.current = 0.
        ws.zoom = 1.
        restore_okk(self.root, snapshot)
        self.assert_view(expected)
        self.assertEqual(ws.project, original)
        self.assertEqual(ws.undo_stack, [])

    def test_cancelling_a_project_switch_does_not_write_navigation(self):
        ws = self.workspace
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cancel.multicam.json'
            ws.project_path = path
            self.assertTrue(ws.save())
            before = path.read_bytes()
            ws.current, ws.zoom, ws.dirty = 5., 8., True
            with patch('multicam_ui.messagebox.askyesnocancel', return_value=None):
                self.assertFalse(ws._confirm_discard())
            self.assertEqual(path.read_bytes(), before)

    def test_locked_view_update_keeps_the_saved_project_intact(self):
        ws = self.workspace
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'locked.multicam.json'
            ws.project_path = path
            self.assertTrue(ws.save())
            before = path.read_bytes()
            ws.current, ws.zoom = 5., 8.
            with patch('project_file.os.replace', side_effect=PermissionError('locked')):
                ws._persist_timeline_view()
            self.assertEqual(path.read_bytes(), before)

    def test_old_project_and_new_project_reset_previous_projects_navigation(self):
        ws = self.long_timeline()
        ws.project.timeline_view = {}
        ws._restore_timeline_view()
        self.root.update()
        self.assertEqual((ws.current, ws.zoom), (0., 1.))
        self.assertEqual(ws.timeline.xview()[0], 0.)
        ws.current, ws.zoom = 2., 8.
        ws.new()
        self.root.update()
        self.assertEqual((ws.current, ws.zoom), (0., 1.))

    def test_saved_position_is_clamped_and_aligned_to_project_frames(self):
        ws = self.workspace
        ws.project.timeline_view = dict(position=30., zoom=8.)
        ws._restore_timeline_view()
        self.assertEqual(ws.current, ws.project.duration)
        ws.project.timeline_view['position'] = 1.234
        ws._restore_timeline_view()
        self.assertAlmostEqual(ws.current, 37 / ws.project.fps)

    def test_switching_projects_cancels_a_deferred_viewport_restore(self):
        ws = self.long_timeline()
        ws.project.timeline_view = dict(position=1200., zoom=16., x_fraction=.6)
        ws._restore_timeline_view()
        ws.project = MulticamProject()
        ws._restore_timeline_view()
        self.root.update()
        self.assertEqual((ws.current, ws.zoom), (0., 1.))
        self.assertEqual(ws.timeline.xview()[0], 0.)
        self.assertIsNone(ws._pending_timeline_view)
        self.assertEqual(self.errors, [])

    def test_hidden_canvas_retains_saved_scroll_until_it_is_mapped(self):
        ws = self.long_timeline()
        expected = ws._capture_timeline_view()
        ws.project.timeline_view = expected
        self.root.withdraw()
        ws._restore_timeline_view()
        self.root.update()
        self.assertEqual(ws._capture_timeline_view(), expected)
        self.assertIsNotNone(ws._pending_timeline_view)
        self.root.deiconify()
        self.assert_view(expected)

    def test_close_cancels_the_pending_restore(self):
        ws = self.workspace
        ws._restore_timeline_view()
        self.assertIsNotNone(ws._timeline_view_after)
        ws.close()
        self.assertIsNone(ws._timeline_view_after)
        self.assertIsNone(ws._pending_timeline_view)


class TimelineViewSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_fixture.StudioSessionTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        session_fixture.StudioSessionTests.tearDownClass.__func__(cls)

    setUp = session_fixture.StudioSessionTests.setUp
    tearDown = session_fixture.StudioSessionTests.tearDown
    populate = session_fixture.StudioSessionTests.populate

    def test_rtxproj_open_restores_last_saved_navigation(self):
        ws = self.populate()
        ws.current, ws.zoom = 5.2, 8.
        path = self.directory / 'navigation.rtxproj'
        self.assertTrue(self.app._project_write(path))
        self.app._project_new()
        ws.current, ws.zoom = 0., 1.
        self.assertFalse(self.app._project_is_dirty())
        self.app._project_open(path)
        self.assertEqual((ws.current, ws.zoom), (5.2, 8.))
        self.assertEqual(ws.seek_var.get(), '00:00:05.200')
        self.assertFalse(self.app._project_is_dirty())
        self.assertEqual(self.errors, [])

    def test_project_switch_remembers_latest_view_when_autosave_is_disabled(self):
        ws = self.populate()
        path = self.directory / 'last-used.rtxproj'
        self.assertTrue(self.app._project_write(path))
        self.app.autosave_enabled = False
        ws.current, ws.zoom = 4.4, 32.
        self.assertFalse(self.app._project_is_dirty())
        self.app._project_new()
        view = read_project_file(path)['state']['okk_multicam']['project']['timeline_view']
        self.assertEqual((view['position'], view['zoom']), (4.4, 32.))
        self.app._project_open(path)
        self.assertEqual((ws.current, ws.zoom), (4.4, 32.))
        self.assertEqual(self.errors, [])

    def test_view_only_changes_are_autosaved_without_marking_the_edits_dirty(self):
        ws = self.populate()
        path = self.directory / 'autosaved.rtxproj'
        self.assertTrue(self.app._project_write(path))
        ws.current, ws.zoom = 3.3, 16.
        self.assertFalse(self.app._project_is_dirty())
        self.app._project_autosave_tick()
        view = read_project_file(path)['state']['okk_multicam']['project']['timeline_view']
        self.assertEqual((view['position'], view['zoom']), (3.3, 16.))
        self.assertFalse(self.app._project_is_dirty())
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
