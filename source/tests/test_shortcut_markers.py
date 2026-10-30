"""Physical-key dispatch, reversible local IME state, and undoable markers."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multicam_core import CameraClip, Cut, MulticamProject
from shortcut_keys import canonical_key, EditorIMEGuard
from timeline_markers_ui import TimelineMarkersMixin


class PhysicalShortcutTests(unittest.TestCase):
    def test_chinese_or_empty_ime_text_does_not_replace_windows_letter_key(self):
        for code, expected in ((0x43, 'c'), (0x4D, 'm'), (0x5A, 'z'),
                               (0x59, 'y'), (0x53, 's'), (0x41, 'a')):
            for char in ('', '中'):
                event = SimpleNamespace(keycode=code, keysym='??', char=char)
                self.assertEqual(canonical_key(event, windows=True), expected)

    def test_number_row_and_numpad_keep_camera_shortcuts_under_ime(self):
        for number in range(1, 10):
            for code in (0x30+number, 0x60+number):
                self.assertEqual(canonical_key(SimpleNamespace(keycode=code, keysym='??', char=''),
                                               windows=True), str(number))

    def test_navigation_keys_are_available_without_char(self):
        for code, expected in ((0x20, 'space'), (0x25, 'Left'), (0x27, 'Right'),
                               (0x2E, 'Delete'), (0x1B, 'Escape')):
            self.assertEqual(canonical_key(SimpleNamespace(keycode=code, keysym='', char=''),
                                           windows=True), expected)

    def test_non_windows_keycodes_and_legacy_test_events_fall_back_to_symbols(self):
        self.assertEqual(canonical_key(SimpleNamespace(keycode=38, keysym='C', char='C'), windows=False), 'c')
        for symbol in ('space', 'Left', 'Right', 'Delete', 'Escape'):
            self.assertEqual(canonical_key(SimpleNamespace(keysym=symbol, char='')), symbol)
        self.assertEqual(canonical_key(SimpleNamespace(keysym='KP_7', char='')), '7')
        self.assertEqual(canonical_key(SimpleNamespace(keysym='', char='m')), 'm')


class IMEGuardTests(unittest.TestCase):
    def setUp(self):
        self.associate = Mock(return_value=1234)
        self.valid = Mock(return_value=True)
        self.guard = EditorIMEGuard(associate=self.associate, is_window=self.valid)
        self.widget = SimpleNamespace(winfo_id=lambda: 9876)

    def test_association_is_only_local_and_original_context_is_restored(self):
        self.guard.suspend(self.widget)
        self.guard.suspend(self.widget)
        self.associate.assert_called_once_with(9876, None)
        self.guard.restore_all()
        self.assertEqual(self.associate.call_args_list[-1].args, (9876, 1234))
        self.guard.restore_all()
        self.assertEqual(self.associate.call_count, 2)

    def test_destroyed_window_is_never_reassociated(self):
        self.guard.suspend(self.widget)
        self.valid.return_value = False
        self.guard.restore_all()
        self.associate.assert_called_once_with(9876, None)

    def test_window_without_an_ime_context_has_nothing_to_restore(self):
        self.associate.return_value = None
        self.guard.suspend(self.widget)
        self.guard.restore_all()
        self.associate.assert_called_once_with(9876, None)


class MarkerHarness(TimelineMarkersMixin):
    def __init__(self, fps=30.):
        self.project = MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', 0., 12.)],
                                       cuts=[Cut(0., 1)], fps=fps)
        self.current = 3.456
        self.busy = False
        self.status = Mock()
        self.draw_timeline = Mock()
        self.undo_stack = []
        self.redo_stack = []
        self._init_timeline_markers()

    def _available(self):
        return not self.busy

    def _remember(self):
        self.undo_stack.append(copy.deepcopy(self.project))
        self.redo_stack.clear()

    def undo(self):
        self.redo_stack.append(copy.deepcopy(self.project))
        self.project = self.undo_stack.pop()

    def redo(self):
        self.undo_stack.append(copy.deepcopy(self.project))
        self.project = self.redo_stack.pop()


class TimelineMarkerTests(unittest.TestCase):
    def test_marker_is_frame_aligned_and_does_not_cut_or_move_sources(self):
        workspace = MarkerHarness()
        before = copy.deepcopy(workspace.project)
        self.assertTrue(workspace.add_timeline_marker())
        self.assertEqual(workspace.project.timeline_markers, [104/30])
        self.assertEqual(workspace.project.clips, before.clips)
        self.assertEqual(workspace.project.cuts, before.cuts)
        self.assertEqual(workspace.project.edit_points, before.edit_points)
        self.assertEqual(len(workspace.undo_stack), 1)

    def test_fractional_rate_marker_rounds_on_the_same_frame_grid(self):
        workspace = MarkerHarness(30000/1001)
        workspace.current = 120/(30000/1001) + .00001
        workspace.add_timeline_marker()
        self.assertAlmostEqual(workspace.project.timeline_markers[0], 120/(30000/1001), places=12)

    def test_duplicate_frame_and_busy_edits_do_not_create_undo_history(self):
        workspace = MarkerHarness()
        workspace.add_timeline_marker(3.)
        self.assertFalse(workspace.add_timeline_marker(3.00001))
        self.assertEqual(len(workspace.undo_stack), 1)
        workspace.busy = True
        self.assertFalse(workspace.add_timeline_marker(5.))
        self.assertFalse(workspace.remove_timeline_marker(3.))
        self.assertEqual(workspace.project.timeline_markers, [3.])

    def test_add_and_remove_markers_both_support_undo_and_redo(self):
        workspace = MarkerHarness()
        workspace.add_timeline_marker(4.)
        workspace.undo()
        self.assertEqual(workspace.project.timeline_markers, [])
        workspace.redo()
        self.assertEqual(workspace.project.timeline_markers, [4.])
        workspace.remove_timeline_marker(4.)
        self.assertEqual(workspace.project.timeline_markers, [])
        workspace.undo()
        self.assertEqual(workspace.project.timeline_markers, [4.])
        workspace.redo()
        self.assertEqual(workspace.project.timeline_markers, [])

    def test_held_m_adds_one_marker_until_release(self):
        workspace = MarkerHarness()
        self.assertEqual(workspace._marker_key_press(), 'break')
        workspace.current = 7.
        workspace._marker_key_press()
        self.assertEqual(len(workspace.project.timeline_markers), 1)
        workspace._marker_key_release()
        workspace._marker_key_press()
        self.assertEqual(len(workspace.project.timeline_markers), 2)

    def test_markers_roundtrip_in_project_and_survive_shortening_source(self):
        workspace = MarkerHarness()
        workspace.add_timeline_marker(8.)
        workspace.project.clips[0].duration = 4.
        with tempfile.TemporaryDirectory(prefix='okk_marker_test_') as directory:
            path = Path(directory)/'marker.multicam.json'
            workspace.project.save(path)
            restored = MulticamProject.load(path)
        self.assertEqual(restored.timeline_markers, [8.])
        self.assertEqual(restored.duration, 4.)
        self.assertEqual(workspace._timeline_marker_extent(), 8.)

    def test_invalid_marker_times_and_empty_project_are_noops(self):
        workspace = MarkerHarness()
        for value in (float('nan'), float('inf'), 'oops'):
            self.assertFalse(workspace.add_timeline_marker(value))
        workspace.project = MulticamProject()
        self.assertFalse(workspace.add_timeline_marker())
        self.assertEqual(workspace.undo_stack, [])


class ShortcutMarkerWorkspaceTests(unittest.TestCase):
    def setUp(self):
        try:
            from . import test_program_edit_ui as fixture
        except ImportError:
            import test_program_edit_ui as fixture
        self.fixture = fixture
        fixture.ProgramEditWorkspaceTests.setUp(self)

    def tearDown(self):
        self.fixture.ProgramEditWorkspaceTests.tearDown(self)

    def event(self, code, *, widget=None, state=0):
        return SimpleNamespace(keycode=code, keysym='??', char='中', state=state,
                               widget=widget or self.workspace.timeline)

    def test_ime_m_creates_visible_marker_once_and_keyrelease_allows_next(self):
        workspace = self.workspace
        workspace.current = 4.
        self.assertEqual(workspace._key(self.event(0x4D)), 'break')
        self.assertEqual(workspace.project.timeline_markers, [4.])
        self.assertTrue(workspace.timeline.find_withtag('timeline-marker'))
        workspace.current = 7.
        workspace._key(self.event(0x4D))
        self.assertEqual(workspace.project.timeline_markers, [4.])
        workspace._transport_key_release(self.event(0x4D))
        workspace._key(self.event(0x4D))
        self.assertEqual(workspace.project.timeline_markers, [4., 7.])
        self.assertEqual(self.callback_errors, [])

    def test_ime_ctrl_undo_redo_and_c_use_virtual_keys(self):
        workspace = self.workspace
        workspace.current = 5.
        workspace._key(self.event(0x43))
        self.assertEqual(workspace.project.edit_points, [5.])
        workspace._key(self.event(0x5A, state=0x4))
        self.assertEqual(workspace.project.edit_points, [])
        workspace._key(self.event(0x59, state=0x4))
        self.assertEqual(workspace.project.edit_points, [5.])
        self.assertEqual(self.callback_errors, [])

    def test_text_fields_keep_ime_typing_and_do_not_get_suspended(self):
        import tkinter as tk
        from tkinter import ttk
        workspace = self.workspace
        guard = Mock()
        workspace._editor_ime_guard = guard
        for widget in (tk.Entry(workspace), ttk.Entry(workspace), tk.Text(workspace),
                       ttk.Combobox(workspace)):
            with self.subTest(widget=widget.winfo_class()):
                for code in (0x4D, 0x43, 0x31):
                    self.assertIsNone(workspace._key(self.event(code, widget=widget)))
                workspace._transport_editor_focus_in(SimpleNamespace(widget=widget))
                widget.destroy()
        guard.suspend.assert_not_called()
        self.assertEqual(workspace.project.timeline_markers, [])
        workspace._transport_editor_focus_in(SimpleNamespace(widget=workspace.timeline))
        guard.suspend.assert_called_once_with(workspace.timeline)
        workspace._transport_editor_focus_out()
        guard.restore_all.assert_called_once()

    def test_marker_ruler_menu_deletes_and_undo_restores(self):
        workspace = self.workspace
        workspace.add_timeline_marker(3.)
        canvas = workspace.timeline
        item = next(item for item in workspace.timeline_marker_items
                    if canvas.type(item) == 'polygon')
        points = canvas.coords(item)
        x, y = points[0] + workspace._px(5), points[1] + workspace._px(3)
        event = SimpleNamespace(x=x-canvas.canvasx(0), y=y-canvas.canvasy(0),
                                x_root=10000+x, y_root=10000+y)
        with patch('timeline_markers_ui.tk.Menu.tk_popup') as popup:
            self.assertTrue(workspace._timeline_marker_context(event))
        popup.assert_called_once()
        workspace.timeline_marker_menu.invoke(0)
        self.assertEqual(workspace.project.timeline_markers, [])
        workspace.undo()
        self.assertEqual(workspace.project.timeline_markers, [3.])
        self.assertEqual(self.callback_errors, [])

    def test_marker_guides_do_not_capture_source_clip_clicks(self):
        workspace = self.workspace
        workspace.add_timeline_marker(4.)
        y = sum(workspace._timeline_track_bounds[1])/2
        event = SimpleNamespace(x=workspace._timeline_x(4.)-workspace.timeline.canvasx(0),
                                y=y-workspace.timeline.canvasy(0))
        self.assertIsNone(workspace._timeline_marker_at(event))
        self.assertFalse(workspace._timeline_marker_press(event))

    def test_focus_out_releases_ime_context_and_held_marker_key(self):
        workspace = self.workspace
        guard = workspace._editor_ime_guard = Mock()
        workspace._marker_key_press()
        self.assertTrue(workspace._timeline_marker_key_down)
        workspace._transport_focus_out()
        self.assertFalse(workspace._timeline_marker_key_down)
        guard.restore_all.assert_called_once()


if __name__ == '__main__':
    unittest.main()
