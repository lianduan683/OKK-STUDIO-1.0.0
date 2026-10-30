"""Exercise preset changes through real Tk controls and editing gestures."""
from pathlib import Path
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from types import MethodType, SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from keyboard_profiles import PROFILE_LABELS, load_profile, save_profile, shortcut_action
from keyboard_profile_ui import KeyboardProfileSettingsMixin
from tests import test_program_edit_ui as fixture


class PreferenceTests(unittest.TestCase):
    def test_roundtrip_and_invalid_preferences_fall_back_to_original(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'keys.json'
            self.assertEqual(load_profile(path), 'current')
            for profile in PROFILE_LABELS:
                save_profile(profile, path)
                self.assertEqual(load_profile(path), profile)
            for payload in ('bad json', '[]', '{"profile": "unknown"}', '{"profile": []}'):
                path.write_text(payload, encoding='utf-8')
                self.assertEqual(load_profile(path), 'current')

    def test_unmapped_modifier_combinations_do_not_trigger_edits(self):
        for profile in PROFILE_LABELS:
            for state in (0x20000, 8, 0x20005):
                event = SimpleNamespace(keysym='c', char='c', state=state)
                self.assertIsNone(shortcut_action(profile, event))


class KeyboardPresetWorkspaceTests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def key(self, key, state=0, code=None, widget=None):
        values = dict(keysym=key, char='', state=state, widget=widget or self.workspace.timeline)
        if code is not None: values['keycode'] = code
        return self.workspace._key(SimpleNamespace(**values))

    def click_program(self, position):
        ws = self.workspace
        top, bottom = ws._timeline_program_bounds
        x = round(ws._timeline_x(position)-ws.timeline.canvasx(0))
        y = round((top+bottom)/2-ws.timeline.canvasy(0))
        ws.timeline.event_generate('<ButtonPress-1>', x=x, y=y)
        ws.timeline.event_generate('<ButtonRelease-1>', x=x, y=y)

    def test_original_c_still_splits_immediately(self):
        self.root.keyboard_profile = 'current'
        ws = self.workspace
        ws.seek(3.)
        self.key('c')
        self.assertIn(3., ws.project.edit_points)
        self.assertEqual(ws._keyboard_tool, 'select')

    def test_resolve_blade_selection_split_undo_and_shift_redo(self):
        self.root.keyboard_profile = 'resolve'
        ws = self.workspace
        ws.seek(3.)
        self.key('b')
        self.assertEqual(ws.project.edit_points, [])
        self.assertEqual(ws._keyboard_tool, 'razor')
        self.click_program(3.)
        self.assertTrue(any(abs(point-3.) < .04 for point in ws.project.edit_points))
        self.key('a'); self.assertEqual(ws._keyboard_tool, 'select')
        ws.seek(6.); self.key('b', state=4)
        self.assertIn(6., ws.project.edit_points)
        self.key('z', state=4); self.assertNotIn(6., ws.project.edit_points)
        self.key('z', state=5); self.assertIn(6., ws.project.edit_points)
        before = list(ws.project.edit_points)
        self.key('c')
        self.assertEqual(ws.project.edit_points, before)

    def test_premiere_c_only_selects_razor_and_ctrl_k_splits_once_in_ime(self):
        self.root.keyboard_profile = 'premiere'
        ws = self.workspace
        ws.seek(4.)
        self.key('c', code=0x43)
        self.assertEqual(ws.project.edit_points, [])
        self.assertEqual(ws._keyboard_tool, 'razor')
        self.key('v'); self.assertEqual(ws._keyboard_tool, 'select')
        self.key('Process', state=4, code=0x4B)
        self.assertEqual(ws.project.edit_points, [4.])
        self.assertEqual(len(ws.undo_stack), 1)
        self.key('z', state=4)
        self.assertEqual(ws.project.edit_points, [])
        self.key('z', state=5)
        self.assertEqual(ws.project.edit_points, [4.])

    def test_range_keys_include_displayed_out_frame_and_are_undoable(self):
        self.root.keyboard_profile = 'premiere'
        ws = self.workspace
        ws.seek(2.); self.key('i')
        ws.seek(5.); self.key('o')
        self.assertEqual(ws.project.range_start, 2.)
        self.assertAlmostEqual(ws.project.range_end, 5.+1/30)
        self.key('i', state=1); self.assertEqual(ws.current, 2.)
        self.key('o', state=1); self.assertEqual(ws.current, 5.)
        self.key('o', state=5); self.assertIsNone(ws.project.range_end)
        self.key('z', state=4); self.assertAlmostEqual(ws.project.range_end, 5.+1/30)
        self.key('x', state=5)
        self.assertEqual((ws.project.range_start, ws.project.range_end), (0., None))

    def test_navigation_and_jkl_use_existing_transport(self):
        self.root.keyboard_profile = 'resolve'
        ws = self.workspace
        ws.seek(3.); self.key('b', state=4)
        ws.seek(7.); self.key('b', state=4)
        ws.seek(5.); self.key('Up'); self.assertEqual(ws.current, 3.)
        self.key('Down'); self.assertEqual(ws.current, 7.)
        self.key('Home'); self.assertEqual(ws.current, 0.)
        self.key('End'); self.assertAlmostEqual(ws.current, 12.-1/30)
        ws.player = Mock()
        ws._can_start_transport_monitors = Mock(return_value=True)
        self.key('j')
        self.assertEqual(ws._transport_held_direction, -1)
        self.key('k')
        self.assertEqual(ws._transport_held_direction, 0)
        ws.player.submit.assert_called_with('pause')
        self.key('l')
        ws.player.submit.assert_called_with('play')

    def test_inputs_keep_typing_and_modified_keys_dont_use_old_shortcuts(self):
        ws = self.workspace
        entry = tk.Entry(ws)
        for profile in ('resolve', 'premiere'):
            self.root.keyboard_profile = profile
            for key in ('b', 'c', 'i', 'o', 'm', 'j', 'l'):
                self.assertIsNone(self.key(key, widget=entry))
            self.assertIsNone(self.key('y', state=4))
        self.assertEqual(ws.project.edit_points, [])
        self.assertEqual(ws.project.timeline_markers, [])
        entry.destroy()

    def test_native_ctrl_shift_o_clears_out_without_bubbling_to_open_project(self):
        ws = self.workspace
        self.root.keyboard_profile = 'premiere'
        ws.project.range_end = 6.
        ws._restore_timeline_range_fields()
        opened = Mock()
        self.root.bind('<Control-Shift-O>', opened)
        ws.timeline.focus_force()
        ws.timeline.event_generate('<KeyPress-O>', state=5)
        self.assertIsNone(ws.project.range_end)
        opened.assert_not_called()

    def test_settings_switch_persists_and_resets_tool_without_dirtying(self):
        ws = self.workspace
        self.root.multicam_workspace = ws
        for name in ('_keyboard_profile_selected', '_show_keyboard_reference', '_apply_keyboard_settings'):
            setattr(self.root, name, MethodType(getattr(KeyboardProfileSettingsMixin, name), self.root))
        with patch('keyboard_profile_ui.load_profile', return_value='current'):
            KeyboardProfileSettingsMixin._init_keyboard_profile(self.root)
        before = ws.project.to_payload('.')
        dirty, history = ws.dirty, len(ws.undo_stack)
        ws._transport_request_id = 37
        with patch('keyboard_profile_ui.save_profile') as save:
            self.root._apply_keyboard_settings('premiere')
            save.assert_called_once_with('premiere')
        self.assertEqual(self.root.keyboard_profile, 'premiere')
        self.assertGreaterEqual(ws._transport_request_id, 37)
        self.assertIn('Ctrl+K', ws._keyboard_hint_var.get())
        self.key('c'); self.assertEqual(ws._keyboard_tool, 'razor')
        with patch('keyboard_profile_ui.save_profile'):
            self.root._apply_keyboard_settings('resolve')
        self.assertEqual(ws._keyboard_tool, 'select')
        self.assertEqual(ws.project.to_payload('.'), before)
        self.assertEqual((ws.dirty, len(ws.undo_stack)), (dirty, history))


class MainSettingsPresetTests(unittest.TestCase):
    def test_real_application_settings_contain_presets_and_autosave_and_remember_selection(self):
        import rtx5090_exporter as module
        original_init, original_top = tk.Tk.__init__, tk.Toplevel
        def hidden_init(root, *args, **kwargs):
            original_init(root, *args, **kwargs)
            root.withdraw(); root.attributes('-alpha', 0)
            root.geometry('1280x820+10000+10000')
        def hidden_top(*args, **kwargs):
            window = original_top(*args, **kwargs)
            window.withdraw(); window.geometry('920x650+10000+10000')
            return window
        with TemporaryDirectory() as directory, ExitStack() as stack:
            folder = Path(directory)
            pref = folder/'keyboard.json'
            save_profile('resolve', pref)
            for item in (patch('keyboard_profiles.preferences_path', return_value=pref),
                         patch('autosave_settings.preferences_path', return_value=folder/'autosave.json'),
                         patch('studio_project.app_directory', return_value=folder),
                         patch.object(module, 'initialize_cache_policy', return_value=folder),
                         patch.object(module.ExporterApp, '_detect_async'),
                         patch.object(module.ExporterApp, '_offer_session_recovery'),
                         patch.object(module.ExporterApp, 'state'),
                         patch.object(tk.Tk, '__init__', hidden_init),
                         patch.object(tk, 'Toplevel', hidden_top)):
                stack.enter_context(item)
            app = module.ExporterApp()
            try:
                errors = []
                app.report_callback_exception = lambda *args: errors.append(args)
                app.geometry('1280x820+10000+10000'); app.deiconify(); app.update()
                self.assertEqual(app.keyboard_profile, 'resolve')
                self.assertEqual(app.keyboard_profile_var.get(), 'DaVinci Resolve')
                layout = app.multicam_workspace.workspace_layout_menu
                self.assertFalse(any(layout.type(i) == 'cascade' and layout.entrycget(i, 'label') == '键位选择'
                                     for i in range(layout.index('end')+1)))
                self.assertTrue(app.multicam_workspace.workspace_layout_button.winfo_ismapped())
                ws = app.multicam_workspace
                original = ws.project.to_payload('.')
                ws.show_project_settings()
                ws._project_settings_select('keyboard')
                variables = ws._project_settings_vars
                self.assertEqual(variables['keyboard_profile'].get(), 'DaVinci Resolve')
                variables['keyboard_profile'].set('Premiere Pro')
                variables['autosave_enabled'].set(False)
                ws._project_settings_window.destroy()
                self.assertEqual(app.keyboard_profile, 'resolve')
                self.assertTrue(app.autosave_enabled)
                ws.show_project_settings()
                variables = ws._project_settings_vars
                variables['keyboard_profile'].set('Premiere Pro')
                variables['autosave_interval'].set('nan')
                ws._project_settings_apply()
                self.assertTrue(ws._project_settings_window.winfo_exists())
                self.assertEqual(app.keyboard_profile, 'resolve')
                variables['autosave_interval'].set('137')
                ws._project_settings_apply()
                self.assertFalse(ws._project_settings_window.winfo_exists())
                self.assertEqual(load_profile(pref), 'premiere')
                self.assertEqual(app.autosave_interval_seconds, 137)
                self.assertEqual(ws.project.to_payload('.'), original)
                self.assertEqual(ws.undo_stack, [])
                self.assertIn('Ctrl+K', app.multicam_workspace._keyboard_hint_var.get())
                app._show_keyboard_reference()
                self.assertTrue(app._keyboard_reference_window.winfo_exists())
                self.assertEqual(errors, [])
            finally:
                app._closing = True
                app.lossless_workspace.close()
                app.media_pool_workspace.close()
                app.multicam_workspace.close()
                for pending in app.tk.call('after', 'info'): app.tk.call('after', 'cancel', pending)
                app.destroy()


if __name__ == '__main__': unittest.main()
