"""Real Tk regressions for detached program monitor fullscreen and transport."""
from __future__ import annotations

from types import SimpleNamespace
import ctypes as C
from ctypes import wintypes as W
import os
import unittest

import test_workspace_docking as fixture
from tk_native_calls import TkNativeCalls


def monitor_rectangles():
    rectangles = []
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC, C.POINTER(W.RECT), W.LPARAM)

    @callback_type
    def collect(_monitor, _dc, rectangle, _data):
        rect = rectangle.contents
        rectangles.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    C.windll.user32.EnumDisplayMonitors(None, None, collect, 0)
    return rectangles


def native_rectangle(window, position=None):
    api = C.WinDLL('user32', use_last_error=True)
    api.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
    api.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
    hwnd = int(window.tk.call('wm', 'frame', window), 0)
    if position is not None:
        left, top, right, bottom = position
        if not TkNativeCalls.for_widget(window).call(
                api.SetWindowPos, hwnd, None, left, top, right - left, bottom - top, 0x0014):
            raise C.WinError(C.get_last_error())
        window.update_idletasks()
    rect = W.RECT()
    if not api.GetWindowRect(hwnd, C.byref(rect)):
        raise C.WinError(C.get_last_error())
    return rect.left, rect.top, rect.right, rect.bottom


class MonitorFullscreenTests(unittest.TestCase):
    setUp = fixture.WorkspaceDockingTests.setUp
    tearDown = fixture.WorkspaceDockingTests.tearDown
    video_handles = fixture.WorkspaceDockingTests.video_handles
    float_panel = fixture.WorkspaceDockingTests.float_panel
    float_group = fixture.WorkspaceDockingTests.float_group

    def key(self, keysym, widget=None):
        return self.workspace._key(SimpleNamespace(
            keysym=keysym, widget=widget or self.workspace.program_surface))

    def assert_fullscreen(self, window):
        self.root.update()
        self.assertTrue(window.tk.getboolean(window.tk.call('wm', 'attributes', window, '-fullscreen')))
        self.assertFalse(self.workspace.program_title.winfo_ismapped())
        self.assertTrue(self.workspace.program_surface.winfo_viewable())
        viewport = self.workspace.program_viewport
        self.assertLessEqual(abs(viewport.winfo_width() - window.winfo_width()), 2)
        self.assertLessEqual(abs(viewport.winfo_height() - window.winfo_height()), 2)
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(self.errors, [])

    def test_single_panel_f11_and_escape_preserve_geometry_selection_and_playback(self):
        ws = self.workspace
        window = self.float_panel('program')
        geometry = self.manager._float_geometry('program')
        player = ws.player = fixture.RecordingPlayer()
        ws.selected_program_range = (1., 3.)
        for _ in range(3):
            self.assertEqual(self.key('F11'), 'break')
            self.assert_fullscreen(window)
            self.assertEqual(self.manager.serialize()['floating']['program'], geometry)
            self.assertIsNotNone(self.manager.fullscreen.active)
            self.assertEqual(self.key('Escape'), 'break')
            self.root.update()
            self.assertIsNone(self.manager.fullscreen.active)
            self.assertTrue(ws.program_title.winfo_ismapped())
            self.assertEqual(self.manager._float_geometry('program'), geometry)
        self.assertEqual(ws.selected_program_range, (1., 3.))
        self.assertTrue(all(action == 'refresh_presentation' for action, _values in player.commands))
        self.assertFalse(player.closed)
        self.assertEqual(ws.project, self.project_before)

    def test_group_button_fullscreen_hides_tabs_and_header_and_saves_normal_geometry(self):
        ws = self.workspace
        self.manager.dock('source', 'program')
        window = self.float_group('program')
        self.manager.focus('program')
        self.root.update()
        saved = self.manager.serialize()
        group = self.manager._floating_group('program')
        host = self.manager._float_hosts[group['id']]
        ws.program_fullscreen_button.invoke()
        self.assert_fullscreen(window)
        self.assertFalse(host.header.winfo_ismapped())
        self.assertEqual(self.manager.serialize(), saved)
        self.assertIsNotNone(self.manager.fullscreen.active)
        self.key('F11')
        self.root.update()
        self.assertTrue(host.header.winfo_ismapped())
        self.assertEqual(host.notebook.cget('style'), 'Dock.TNotebook')
        self.assertTrue(ws.program_title.winfo_ismapped())
        self.assertEqual(window.geometry(), saved['floating_groups'][0]['geometry'])
        self.assertEqual(self.video_handles(), self.handles)

    def test_maximized_group_returns_to_maximized_state(self):
        window = self.float_group('program')
        window.state('zoomed')
        self.root.update()
        self.key('F11')
        self.assert_fullscreen(window)
        self.key('Escape')
        self.root.update()
        self.assertEqual(window.state(), 'zoomed')
        self.assertTrue(self.workspace.program_title.winfo_ismapped())

    def test_group_tab_switch_and_layout_reset_restore_chrome_before_detaching(self):
        self.manager.dock('source', 'program')
        window = self.float_group('program')
        self.key('F11')
        self.assert_fullscreen(window)
        self.manager.focus('source')
        self.root.update()
        self.assertIsNone(self.manager.fullscreen.active)
        self.key('F11')
        self.assert_fullscreen(window)
        self.manager.restore_defaults()
        self.root.update()
        self.assertIsNone(self.manager.fullscreen.active)
        self.assertTrue(self.workspace.program_title.winfo_ismapped())
        self.assertEqual(self.workspace.program_panel.winfo_manager(), 'grid')
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(self.errors, [])

    def test_escape_from_floating_group_shell_exits_fullscreen(self):
        window = self.float_group('program')
        self.key('F11')
        self.assert_fullscreen(window)
        # A shell event has no editor transport bindtag. Its docking Escape
        # binding must still restore the monitor before cancelling any drag.
        self.manager._cancel_drag(SimpleNamespace(widget=window))
        self.root.update()
        self.assertIsNone(self.manager.fullscreen.active)
        self.assertTrue(self.workspace.program_title.winfo_ismapped())
        self.assertEqual(self.errors, [])

    @unittest.skipUnless(os.name == 'nt', 'Windows monitor placement')
    def test_fullscreen_and_restore_on_every_display_for_panel_and_group(self):
        monitors = monitor_rectangles()
        if len(monitors) < 2:
            self.skipTest('Multiple displays required')
        for grouped in (False, True):
            window = self.float_group('program') if grouped else self.float_panel('program')
            for bounds in monitors:
                with self.subTest(grouped=grouped, monitor=bounds):
                    left, top, _, _ = bounds
                    native_rectangle(window, (left + 120, top + 100, left + 760, top + 580))
                    before = native_rectangle(window)
                    geometry = self.manager.fullscreen.geometry(window)
                    for _ in range(2):
                        # Global F11 must follow the program panel even when
                        # keyboard focus belongs to the main editor window.
                        self.key('F11', self.root)
                        self.assert_fullscreen(window)
                        self.assertEqual(native_rectangle(window), bounds)
                        self.assertEqual(self.manager.fullscreen.geometry(window), geometry)
                        self.key('Escape')
                        self.root.update()
                        self.assertEqual(native_rectangle(window), before)
                        self.assertEqual(self.manager.fullscreen.geometry(window), geometry)
            self.manager.restore_defaults()

    @unittest.skipUnless(os.name == 'nt', 'Windows monitor placement')
    def test_docked_program_uses_its_display_before_becoming_a_window(self):
        for bounds in monitor_rectangles():
            with self.subTest(monitor=bounds):
                left, top, _, _ = bounds
                native_rectangle(self.root, (left + 100, top + 100, left + 1380, top + 920))
                self.root.update()
                self.assertTrue(self.manager.is_home('program'))
                self.key('F11')
                window = self.workspace.program_panel
                self.assert_fullscreen(window)
                self.assertEqual(native_rectangle(window), bounds)
                self.key('Escape')
                self.manager.restore_defaults()
                self.root.update()

    @unittest.skipUnless(os.name == 'nt', 'Windows monitor placement')
    def test_maximized_group_restores_on_each_display_and_keeps_normal_bounds(self):
        window = self.float_group('program')
        for bounds in monitor_rectangles():
            with self.subTest(monitor=bounds):
                window.state('normal')
                self.root.update()
                left, top, _, _ = bounds
                native_rectangle(window, (left + 120, top + 100, left + 760, top + 580))
                normal = native_rectangle(window)
                window.state('zoomed')
                self.root.update()
                maximized = native_rectangle(window)
                self.key('F11')
                self.assert_fullscreen(window)
                self.assertEqual(native_rectangle(window), bounds)
                self.key('Escape')
                self.root.update()
                self.assertEqual(window.state(), 'zoomed')
                self.assertEqual(native_rectangle(window), maximized)
                window.state('normal')
                self.root.update()
                self.assertEqual(native_rectangle(window), normal)


if __name__ == '__main__':
    unittest.main()
