"""Page switches keep live editor containers, window handles and navigation."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from . import test_delivery_ui as fixture
from multicam_core import CameraClip, Cut, MulticamProject
from creative_core import ColorGrade


class WorkspacePageStateTests(unittest.TestCase):
    setUp = fixture.DeliveryTkTests.setUp
    tearDown = fixture.DeliveryTkTests.tearDown
    _create_app = fixture.DeliveryTkTests._create_app
    _destroy_app = fixture.DeliveryTkTests._destroy_app

    def _settle(self):
        self.app.geometry('1500x1000+10000+10000')
        self.app.attributes('-alpha', 0)
        fixture.DeliveryTkTests._settle(self)

    def edit(self):
        self.app._select_page('multicam')
        ws = self.workspace
        ws.project = MulticamProject(clips=[
            CameraClip(str(i), i, f'camera-{i}.mp4', 0., 3600.) for i in range(1, 7)],
            cuts=[Cut(0., 1), Cut(1800., 2)])
        ws._refresh(reconfigure=False)
        self._settle()
        return ws

    def roundtrip(self):
        for page in ('color', 'multicam'):
            self.app.page_buttons[page].invoke()
            self._settle()

    def test_custom_groups_tabs_sashes_and_camera_navigation_stay_alive(self):
        ws = self.edit(); manager = ws.dock_manager
        manager.dock('source', 'program', side='center')
        manager.dock('library', 'timeline', side='right')
        manager.focus('source')
        ws.set_camera_grid(2, 2, persist=False)
        ws._change_camera_page(1)
        manager.focus('cuts')
        ws.zoom = 12.
        ws.draw_timeline()
        self._settle()
        ws.timeline.xview_moveto(.3)
        ws.body.sashpos(0, 275)
        ws.workspace_split.sashpos(0, 485)
        for _, pane in manager._sashes:
            pane.sash_place(0, 600, 0)
        self._settle()
        before = copy.deepcopy(manager.serialize())
        containers = tuple(manager._containers)
        handles = {key: widget.winfo_id() for key, widget in ws._dock_widgets.items()}
        camera_handles = {key: widget.winfo_id() for key, widget in ws.camera_surfaces.items()}
        tabs = {key: str(nb.select()) for key, nb in manager._bound.items()}
        xview = ws.timeline.xview()[0]
        sashes = ws.body.sashpos(0), ws.workspace_split.sashpos(0)
        unmapped = []
        ws.bind('<Unmap>', lambda event: unmapped.append(event.widget) if event.widget is ws else None, add='+')
        with patch.object(manager, '_render', wraps=manager._render) as rebuild, \
                patch.object(manager, 'restore', wraps=manager.restore) as restore:
            for _ in range(3):self.roundtrip()
            rebuild.assert_not_called(); restore.assert_not_called()
        self.assertEqual(tuple(manager._containers), containers)
        self.assertTrue(all(widget.winfo_exists() for widget in containers))
        self.assertEqual(manager.serialize(), before)
        self.assertEqual({key: str(nb.select()) for key, nb in manager._bound.items()}, tabs)
        self.assertEqual({key: widget.winfo_id() for key, widget in ws._dock_widgets.items()}, handles)
        self.assertEqual({key: widget.winfo_id() for key, widget in ws.camera_surfaces.items()}, camera_handles)
        self.assertEqual(ws.camera_page, 1)
        self.assertTrue(any(str(nb.select()) == str(ws.cut_tab) for nb in manager._bound.values()))
        self.assertEqual(ws.zoom, 12.)
        self.assertAlmostEqual(ws.timeline.xview()[0], xview, places=3)
        self.assertEqual((ws.body.sashpos(0), ws.workspace_split.sashpos(0)), sashes)
        self.assertEqual(unmapped, [])

    def test_floating_group_and_single_window_return_in_the_same_shells(self):
        ws = self.edit(); manager = ws.dock_manager
        manager.dock('source', 'program', side='center')
        manager.float_group('program', geometry='650x430+10000+10000')
        manager.float_panel('library', geometry='460x360+10000+10000')
        self._settle()
        shell = manager.floating_group_window('program')
        shell.attributes('-alpha', 0)
        ws.library_tab.tk.call('wm', 'attributes', ws.library_tab, '-alpha', 0)
        group = manager._floating_group('program')
        host = manager._float_hosts[group['id']]
        manager.focus('source'); self._settle()
        windows = shell.winfo_id(), ws.library_tab.winfo_id()
        members = dict(host.members); pages = dict(host.pages)
        before = copy.deepcopy(manager.serialize())
        for _ in range(2):self.roundtrip()
        self.assertIs(manager.floating_group_window('program'), shell)
        self.assertEqual((shell.winfo_id(), ws.library_tab.winfo_id()), windows)
        self.assertEqual(host.members, members)
        self.assertEqual(host.pages, pages)
        self.assertTrue(all(item.attached for item in host.members.values()))
        self.assertEqual(host.group['selected'], 'source')
        self.assertEqual(manager.serialize(), before)

    def test_hidden_panels_and_saved_editing_layout_survive_color_page(self):
        ws = self.edit(); manager = ws.dock_manager
        manager.hide('program'); manager.hide('delivery')
        self._settle()
        before = copy.deepcopy(manager.serialize())
        self.app._select_page('color'); self._settle()
        self.assertTrue(ws.program_panel.winfo_ismapped())
        self.assertTrue(ws.save_workspace_layout())
        saved = json.loads(Path(ws._layout_file()).read_text('utf-8'))
        saved.pop('workspace_chrome')
        self.assertEqual(saved, before)
        self.app._select_page('multicam'); self._settle()
        self.assertFalse(ws.program_panel.winfo_ismapped())
        self.assertEqual(manager.serialize(), before)

    def test_single_floating_program_and_borrowed_sibling_tabs_keep_their_positions(self):
        ws = self.edit(); manager = ws.dock_manager
        manager.float_panel('program', geometry='680x460+10000+10000')
        self._settle()
        window = ws.program_panel
        window.tk.call('wm','attributes',window,'-alpha',0)
        handle = window.winfo_id()
        before = copy.deepcopy(manager.serialize())
        self.roundtrip()
        self.assertEqual(window.winfo_id(), handle)
        self.assertEqual(window.winfo_manager(), 'wm')
        self.assertEqual(manager.serialize(), before)
        manager.dock('program', 'timeline', side='center')
        manager.dock('delivery', 'timeline', side='center')
        manager.focus('program'); self._settle()
        before = copy.deepcopy(manager.serialize())
        group_notebook = next(nb for nb in manager._bound.values()
                              if str(window) in nb.tabs())
        tabs = group_notebook.tabs()
        self.roundtrip()
        self.assertTrue(group_notebook.winfo_exists())
        self.assertEqual(group_notebook.tabs(), tabs)
        self.assertEqual(group_notebook.select(), str(window))
        self.assertEqual(manager.serialize(), before)

    def test_color_page_keeps_manual_split_sizes_and_updates_actual_grade(self):
        ws = self.edit(); finish = ws.finishing_workspace
        original_clips = copy.deepcopy(ws.project.clips)
        original_cuts = copy.deepcopy(ws.project.cuts)
        self.app._select_page('color'); self._settle()
        finish.upper.sashpos(0, 900); finish.split.sashpos(0, 300)
        self._settle()
        sizes = finish.upper.sashpos(0), finish.split.sashpos(0)
        self.assertTrue(ws.apply_selected_grade(ColorGrade(exposure=.25)))
        self.roundtrip()
        self.app._select_page('color'); self._settle()
        self.assertEqual((finish.upper.sashpos(0), finish.split.sashpos(0)), sizes)
        self.assertEqual(ws.project.clips, original_clips)
        self.assertEqual(ws.project.cuts, original_cuts)
        self.assertAlmostEqual(ws.get_selected_grade().exposure, .25)

    def test_editor_and_transcode_still_hide_the_shared_project_host(self):
        ws = self.edit()
        for other in ('editor', 'basic'):
            self.app._select_page(other); self._settle()
            self.assertFalse(ws.winfo_ismapped())
            self.app._select_page('multicam'); self._settle()
            self.assertTrue(ws.winfo_ismapped())


if __name__ == '__main__':unittest.main()
