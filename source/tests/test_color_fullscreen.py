"""Native colour-page floating/fullscreen without replacing video surfaces."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import test_delivery_ui as fixture
from .test_transport_controls import RecordingPlayer
from .test_monitor_fullscreen import native_rectangle, monitor_rectangles
from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject


class ColorFullscreenTests(unittest.TestCase):
    tearDown = fixture.DeliveryTkTests.tearDown
    _create_app = fixture.DeliveryTkTests._create_app
    _destroy_app = fixture.DeliveryTkTests._destroy_app

    def _settle(self):
        self.app.attributes('-alpha', 0)
        self.app.geometry('1500x1000+10000+10000')
        fixture.DeliveryTkTests._settle(self)

    def setUp(self):
        fixture.DeliveryTkTests.setUp(self)
        self.app._select_page('multicam')
        self.ws = self.workspace
        self.manager = self.ws.dock_manager
        self.ws.project = MulticamProject([
            CameraClip('one', 1, 'original-1.mp4', 0., 12.),
            CameraClip('two', 2, 'original-2.mp4', 0., 12.)], [Cut(0., 1), Cut(6., 2)])
        self.ws._refresh(reconfigure=False)
        self.ws.player = RecordingPlayer()
        self._settle()
        self.handles = self.video_handles()
        original = self.manager._manage_float
        def manage(*args, **kwargs):
            original(*args, **kwargs)
            widget = self.manager.panels[args[0]].widget
            widget.tk.call('wm', 'attributes', widget, '-alpha', 0)
        self.manage_patch = patch.object(self.manager, '_manage_float', side_effect=manage)
        self.manage_patch.start()
        self.addCleanup(self.manage_patch.stop)

    def video_handles(self):
        return tuple(widget.winfo_id() for widget in (
            self.ws.program_surface, self.ws.audio_surface, *self.ws.program_views,
            *self.ws.comparison_views, *self.ws.camera_surfaces.values()))

    def key(self, keysym, widget=None):
        return self.ws._key(SimpleNamespace(keysym=keysym, widget=widget or self.ws.program_surface))

    def assert_fullscreen(self, window):
        self._settle()
        self.assertTrue(window.tk.getboolean(window.tk.call('wm', 'attributes', window, '-fullscreen')))
        self.assertTrue(self.ws.program_surface.winfo_viewable())
        self.assertFalse(self.ws.program_title.winfo_ismapped())
        self.assertEqual(self.video_handles(), self.handles)
        self.assertLessEqual(abs(self.ws.program_viewport.winfo_width()-window.winfo_width()), 2)
        self.assertLessEqual(abs(self.ws.program_viewport.winfo_height()-window.winfo_height()), 2)

    def test_colour_button_and_f11_work_from_docked_single_and_grouped_edit_layouts(self):
        for layout in ('docked', 'single', 'group'):
            with self.subTest(layout=layout):
                self.manager.restore_defaults()
                if layout == 'single':self.manager.float_panel('program', '680x450+10000+10000')
                if layout == 'group':
                    self.manager.dock('source', 'program', side='center')
                    self.manager.float_group('program', '700x460+10000+10000')
                    self.manager.floating_group_window('program').attributes('-alpha', 0)
                self._settle()
                before = copy.deepcopy(self.manager.serialize())
                containers = tuple(self.manager._containers)
                self.app._select_page('color')
                self._settle()
                self.ws.program_fullscreen_button.invoke()
                self.assert_fullscreen(self.ws.program_panel)
                self.assertEqual(self.manager.serialize(), before)
                self.assertEqual(tuple(self.manager._containers), containers)
                self.key('Escape')
                self._settle()
                self.assertIsNone(self.manager.fullscreen.active)
                self.assertTrue(self.ws.program_title.winfo_ismapped())
                self.key('F11', self.app)
                self.assert_fullscreen(self.ws.program_panel)
                self.app._select_page('multicam')
                self._settle()
                self.assertIsNone(self.manager.fullscreen.active)
                self.assertEqual(self.manager.serialize(), before)
                self.assertEqual(self.video_handles(), self.handles)

    def test_editing_fullscreen_returns_on_same_window_screen_and_normal_geometry_after_grading(self):
        for grouped in (False, True):
            with self.subTest(grouped=grouped):
                self.manager.restore_defaults()
                if grouped:
                    self.manager.dock('source', 'program', side='center')
                    self.manager.float_group('program', '700x460+10000+10000')
                    window = self.manager.floating_group_window('program')
                    window.attributes('-alpha', 0)
                else:
                    self.manager.float_panel('program', '700x460+10000+10000')
                    window = self.ws.program_panel
                self._settle()
                geometry = self.manager.fullscreen.geometry(window)
                self.key('F11')
                self.assert_fullscreen(window)
                bounds = native_rectangle(window)
                before = copy.deepcopy(self.manager.serialize())
                self.app._select_page('color')
                self._settle()
                self.assertIsNone(self.manager.fullscreen.active)
                self.assertEqual(self.ws.program_panel.winfo_manager(), 'grid')
                self.assertTrue(self.ws.apply_selected_grade(ColorGrade(exposure=.3)))
                self.ws.finishing_workspace.float_preview_button.invoke()
                self.key('F11')
                self.assert_fullscreen(self.ws.program_panel)
                self.app._select_page('multicam')
                self.assert_fullscreen(window)
                self.assertIs(self.manager.fullscreen.active['window'], window)
                self.assertEqual(native_rectangle(window), bounds)
                self.assertEqual(self.manager.fullscreen.geometry(window), geometry)
                self.assertEqual(self.manager.serialize(), before)
                self.assertAlmostEqual(self.ws.get_selected_grade().exposure, .3)
                self.key('Escape')
                self._settle()
                self.assertEqual(self.manager.fullscreen.geometry(window), geometry)

    def test_drag_out_back_and_window_close_restore_colour_host_without_changing_edit_layout(self):
        self.app._select_page('color')
        self._settle()
        before = copy.deepcopy(self.manager.serialize())
        panel = self.ws.program_panel
        host = self.ws.finishing_workspace.viewer_host
        x, y = host.winfo_rootx()+10, host.winfo_rooty()+10
        self.manager._press(SimpleNamespace(x_root=x, y_root=y, widget=self.ws.program_title), 'program')
        outside = SimpleNamespace(x_root=x+host.winfo_width()+100, y_root=y+200)
        self.manager._motion(outside)
        self.manager._release(outside)
        self._settle()
        self.assertEqual(panel.winfo_manager(), 'wm')
        self.assertEqual(self.manager.serialize(), before)
        self.manager._drag = dict(key='program', widget=self.ws.program_title, started=True,
            native=True, whole_group=False)
        self.manager._release(SimpleNamespace(x_root=x+40, y_root=y+40))
        self._settle()
        self.assertEqual(panel.winfo_manager(), 'grid')
        self.assertIs(panel.grid_info()['in'], host)
        self.ws.finishing_workspace.float_preview_button.invoke()
        protocol = panel.tk.call('wm', 'protocol', panel, 'WM_DELETE_WINDOW')
        panel.tk.eval(protocol)
        self._settle()
        self.assertEqual(panel.winfo_manager(), 'grid')
        self.assertEqual(self.manager.serialize(), before)
        self.assertEqual(self.video_handles(), self.handles)

    def test_colour_float_fullscreen_and_escape_follow_each_physical_monitor(self):
        self.app._select_page('color')
        self._settle()
        before = copy.deepcopy(self.manager.serialize())
        for bounds in monitor_rectangles():
            with self.subTest(monitor=bounds):
                self.ws.finishing_workspace.float_preview_button.invoke()
                self._settle()
                window = self.ws.program_panel
                left, top, _, _ = bounds
                native_rectangle(window, (left+100, top+100, left+740, top+560))
                normal = native_rectangle(window)
                self.key('F11', self.app)
                self.assert_fullscreen(window)
                self.assertEqual(native_rectangle(window), bounds)
                self.key('Escape')
                self._settle()
                self.assertEqual(native_rectangle(window), normal)
                self.manager.return_borrowed_panel('program')
                self._settle()
                self.assertEqual(self.ws.program_panel.winfo_manager(), 'grid')
                self.assertEqual(self.manager.serialize(), before)

    def test_maximized_fullscreen_roundtrip_keeps_normal_restore_bounds(self):
        self.manager.dock('source', 'program', side='center')
        self.manager.float_group('program', '700x460+10000+10000')
        window = self.manager.floating_group_window('program')
        window.attributes('-alpha', 0)
        self._settle()
        left, top, _, _ = monitor_rectangles()[0]
        native_rectangle(window, (left+100, top+100, left+800, top+560))
        normal = native_rectangle(window)
        window.state('zoomed')
        self._settle()
        self.key('F11')
        self.assert_fullscreen(window)
        self.app._select_page('color')
        self._settle()
        self.app._select_page('multicam')
        self.assert_fullscreen(window)
        self.key('Escape')
        self._settle()
        self.assertEqual(window.state(), 'zoomed')
        window.state('normal')
        self._settle()
        self.assertEqual(native_rectangle(window), normal)


if __name__ == '__main__':
    unittest.main()
