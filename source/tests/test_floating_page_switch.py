"""Isolate native crashes while moving floating panels between real app pages."""
from __future__ import annotations

import copy
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


def layout_without_ids(value):
    if isinstance(value, dict):
        return {key: layout_without_ids(item) for key, item in value.items() if key != 'id'}
    if isinstance(value, list):
        return [layout_without_ids(item) for item in value]
    return value


def page_switch_worker(scenario):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import test_finishing_workspace as fixture
    ctypes.windll.kernel32.SetErrorMode(3)
    case = fixture.FinishingPageTests()
    case.setUp()
    try:
        case.app.attributes('-alpha', 0)
        case.seed_project()
        case.app.page_buttons['multicam'].invoke()
        case._settle()
        ws, manager = case.workspace, case.workspace.dock_manager
        with patch.object(manager, '_screen_bounds', return_value=(-20000, -20000, 40000, 40000)):
            if scenario == 'single':
                for key in tuple(manager.panels):
                    ws._float_dock_panel(key)
                    window = manager.panels[key].widget
                    window.tk.call('wm', 'attributes', window, '-alpha', 0)
                    window.tk.call('wm', 'geometry', window, '480x340+10000+10000')
                    case._settle()
            else:
                if scenario == 'mixed':
                    manager.dock('source', 'program')
                    ws._float_dock_panel('timeline')
                    anchors = ('media', 'program', 'clip', 'grading')
                else:
                    anchors = ('media', 'source', 'program', 'timeline', 'clip', 'grading')
                for key in anchors:
                    ws._float_dock_group(key)
                    manager.floating_group_window(key).geometry('480x340+10000+10000')
                    manager.floating_group_window(key).attributes('-alpha', 0)
                    case._settle()
            before = layout_without_ids(manager.serialize())
            project = ws.project
            content = copy.deepcopy(project)
            frames = [*ws._dock_widgets.values(), ws.program_surface, *ws.program_views,
                      *ws.comparison_views, *ws.camera_surfaces.values()]
            handles = tuple(frame.winfo_id() for frame in frames)
            for cycle in range(3):
                for page in ('color', 'delivery', 'multicam'):
                    print('PAGE', scenario, cycle, page, flush=True)
                    case.app.page_buttons[page].invoke()
                    case._settle()
                    case.assertIs(ws.project, project)
                    case.assertEqual(ws.project, content)
                    case.assertEqual(tuple(frame.winfo_id() for frame in frames), handles)
                    case.assertEqual(case.app.active_page, page)
                    if page == 'multicam':
                        case.assertEqual(layout_without_ids(manager.serialize()), before)
                    else:
                        case.assertTrue(ws.program_surface.winfo_viewable())
                        if page == 'color':
                            case.assertTrue(ws.color_workspace.wheels['lift'].canvas.winfo_viewable())
                        else:
                            case.assertTrue(ws.delivery_export_button.winfo_viewable())
            print('PAGE_SWITCH_OK', scenario, flush=True)
    finally:
        case.tearDown()
        case.doCleanups()


@unittest.skipUnless(os.name == 'nt', 'Windows native floating panels')
class FloatingPageSwitchTests(unittest.TestCase):
    def check_scenario(self, scenario):
        # A native GIL failure aborts Python, so run the real button route in a
        # child and assert successful process exit as well as layout assertions.
        result = subprocess.run(
            [sys.executable, '-X', 'faulthandler', str(Path(__file__).resolve()),
             '--page-switch-worker', scenario],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            encoding='utf-8', errors='replace', timeout=60, creationflags=0x08000000)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PAGE_SWITCH_OK ' + scenario, result.stdout)

    def test_all_single_floating_panels_survive_color_deliver_and_return(self):
        self.check_scenario('single')

    def test_multiple_floating_groups_survive_color_deliver_and_return(self):
        self.check_scenario('group')

    def test_mixed_floats_and_merged_monitor_tabs_restore_original_layout(self):
        self.check_scenario('mixed')


if __name__ == '__main__':
    if '--page-switch-worker' in sys.argv:
        page_switch_worker(sys.argv[-1])
    else:
        unittest.main()
