"""Exercise live sash events while the event queue never becomes idle."""
from copy import deepcopy
import ctypes as C
from ctypes import wintypes as W
import _tkinter
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from tk_native_calls import TkNativeCalls

from . import test_timeline_drag_merge as fixture


class EffectsLiveResizeTests(unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)
        self.workspace.show_effects_library()
        self.root.update()
        self.browser = self.workspace.effects_browser
        self.browser.list.selection_set('clean')
        self.root.update()
        self.before = deepcopy(self.workspace.project)
        self.handles = (self.workspace.library_tab.winfo_id(), self.browser.winfo_id(),
                        self.browser.list.winfo_id(), self.workspace.program_surface.winfo_id())

    tearDown = fixture.TimelineDropTests.tearDown

    def pump_without_idle(self, seconds=.035):
        deadline = time.monotonic() + seconds
        flags = _tkinter.WINDOW_EVENTS | _tkinter.TIMER_EVENTS | _tkinter.DONT_WAIT
        while time.monotonic() < deadline:
            if not self.root.tk.dooneevent(flags):
                time.sleep(.001)

    def assert_content_follows(self):
        page = self.workspace.library_tab
        book = next(book for book in self.workspace.dock_manager._bound.values()
                    if str(page) in book.tabs())
        self.assertAlmostEqual(page.winfo_rootx(), book.winfo_rootx(), delta=3)
        self.assertAlmostEqual(page.winfo_width(), book.winfo_width(), delta=5)
        self.assertEqual(self.browser.winfo_width(), page.winfo_width())
        self.assertEqual(self.browser.list.selection(), ('clean',))
        self.assertEqual(self.workspace.project, self.before)
        self.assertEqual(self.handles, (page.winfo_id(), self.browser.winfo_id(),
                         self.browser.list.winfo_id(), self.workspace.program_surface.winfo_id()))
        self.assertEqual(self.errors, [])

    def test_inspector_sash_follows_each_motion_without_waiting_for_idle(self):
        pane = self.workspace.body
        index = len(pane.panes()) - 2
        y = pane.winfo_height() // 2
        pane.event_generate('<ButtonPress-1>', x=pane.sashpos(index), y=y, when='now')
        for x in (300, 650, 400, 750, 500):
            pane.event_generate('<B1-Motion>', x=x, y=y, when='now')
            self.pump_without_idle()
            first = pane.nametowidget(pane.panes()[0])
            self.assertAlmostEqual(first.winfo_width(), pane.sashpos(0), delta=5)
            self.assert_content_follows()
        pane.event_generate('<ButtonRelease-1>', x=x, y=y, when='now')
        self.assert_content_follows()

    def test_vertical_sash_updates_the_card_viewport_during_the_drag(self):
        pane = self.workspace.workspace_split
        x = pane.winfo_width() // 2
        pane.event_generate('<ButtonPress-1>', x=x, y=pane.sashpos(0), when='now')
        previous = self.browser.list.winfo_height()
        for y in (490, 350, 520):
            pane.event_generate('<B1-Motion>', x=x, y=y, when='now')
            self.pump_without_idle()
            first = pane.nametowidget(pane.panes()[0])
            self.assertAlmostEqual(first.winfo_height(), pane.sashpos(0), delta=5)
            self.assert_content_follows()
        self.assertNotEqual(self.browser.list.winfo_height(), previous)
        pane.event_generate('<ButtonRelease-1>', x=x, y=y, when='now')

    def test_resize_requests_coalesce_and_close_cancels_the_pending_refresh(self):
        manager = self.workspace.dock_manager
        with patch.object(self.workspace, 'update_idletasks', wraps=self.workspace.update_idletasks) as flush:
            for _ in range(100):
                manager._queue_live_resize()
            self.pump_without_idle()
            self.assertEqual(flush.call_count, 1)
        manager._queue_live_resize()
        self.assertIsNotNone(manager._resize_after)
        manager.close()
        self.assertIsNone(manager._resize_after)
        self.pump_without_idle()
        self.assertEqual(self.errors, [])

    def test_new_docking_split_also_refreshes_while_dragging(self):
        manager = self.workspace.dock_manager
        manager.dock('library', 'program', side='right')
        self.root.update()
        pane = next(pane for node, pane in manager._sashes if node['orient'] == 'horizontal')
        y = pane.winfo_height() // 2
        pane.event_generate('<ButtonPress-1>', x=pane.sash_coord(0)[0], y=y, when='now')
        for fraction in (.4, .6, .45):
            x = round(pane.winfo_width() * fraction)
            pane.event_generate('<B1-Motion>', x=x, y=y, when='now')
            self.pump_without_idle()
            first = pane.nametowidget(pane.panes()[0])
            self.assertAlmostEqual(first.winfo_width(), pane.sash_coord(0)[0], delta=5)
            self.assert_content_follows()
        pane.event_generate('<ButtonRelease-1>', x=x, y=y, when='now')

    def test_cards_repaint_even_when_idle_callbacks_cannot_run(self):
        tiles = self.browser.list
        with patch.object(tiles, '_draw', wraps=tiles._draw) as draw:
            for _ in range(100):
                tiles._schedule_draw()
            self.pump_without_idle()
            self.assertEqual(draw.call_count, 1)
            self.assertIsNone(tiles._draw_after)
            self.assertIsNone(tiles._draw_timer)

    def test_floating_group_resize_updates_native_content_without_idle(self):
        manager = self.workspace.dock_manager
        with patch.object(manager, '_screen_bounds', return_value=(-20000, -20000, 40000, 40000)):
            manager.float_group('library', geometry='620x500+10000+10000')
        window = manager.floating_group_window('library')
        window.attributes('-alpha', 0)
        manager.focus('library')
        self.root.update()
        shell = manager._float_hosts[manager._floating_group('library')['id']]
        host = shell.members['library']
        api = host._native.api
        api.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        wrapper = api.GetParent(window.winfo_id())
        rectangle = W.RECT()
        self.assertTrue(api.GetWindowRect(wrapper, C.byref(rectangle)))
        border_width = rectangle.right - rectangle.left - window.winfo_width()
        border_height = rectangle.bottom - rectangle.top - window.winfo_height()
        calls = TkNativeCalls.for_widget(window)
        for width in (900, 480, 750):
            # Match a real native window resize. wm geometry itself is queued
            # at idle and would not exercise a busy native sizing event loop.
            self.assertTrue(calls.call(api.SetWindowPos, wrapper, None, 0, 0,
                                      width + border_width, 500 + border_height, 0x0016))
            self.pump_without_idle(.065)
            self.assertAlmostEqual(window.winfo_width(), width, delta=2)
            self.assertEqual(host.widget.winfo_width(), shell.pages['library'].winfo_width())
            self.assertEqual(host.widget.winfo_height(), shell.pages['library'].winfo_height())
            self.assertEqual(self.browser.list.selection(), ('clean',))
            self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
