"""Actual Tk gestures, target validation and undo/save after library drops."""
import copy
from pathlib import Path
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from effects_browser import PRESETS, PRESET_BY_KEY, EffectPreset, show_effects_browser
from effects_drag import effect_drop_target
from effects_library import OverlayAsset, TransitionMask
from multicam_core import Cut, MulticamProject
from program_edits import grade_at
import test_program_edit_ui as fixture


class EffectsDragTests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def browser(self):
        ws = self.workspace
        ws.project.cuts = [Cut(0, 1), Cut(4, 2), Cut(8, 1)]
        ws._refresh()
        with patch('effects_browser.library_presets', return_value=PRESETS):
            browser = show_effects_browser(ws)
        self.root.update()
        return browser

    def point(self, time, *, y=None):
        ws = self.workspace
        canvas = ws.timeline
        cy = sum(ws._timeline_program_bounds)/2 if y is None else y
        return (round(canvas.winfo_rootx()+ws._timeline_x(time)-canvas.canvasx(0)),
                round(canvas.winfo_rooty()+cy-canvas.canvasy(0)))

    def press(self, browser, key):
        browser.list.see(key)
        self.root.update()
        x, y, width, height = browser.list.bbox(key)
        x, y = x+25, y+height//2
        browser.list.event_generate('<ButtonPress-1>', x=x, y=y,
            rootx=browser.list.winfo_rootx()+x, rooty=browser.list.winfo_rooty()+y)

    def drag(self, browser, key, point, *, release=True):
        self.press(browser, key)
        x, y = point
        browser.list.event_generate('<B1-Motion>', x=x-browser.list.winfo_rootx(),
            y=y-browser.list.winfo_rooty(), rootx=x, rooty=y)
        if release:
            browser.list.event_generate('<ButtonRelease-1>', x=x-browser.list.winfo_rootx(),
                y=y-browser.list.winfo_rooty(), rootx=x, rooty=y)
        self.root.update_idletasks()

    def test_drag_applies_to_hovered_shot_instead_of_old_selection_and_roundtrips(self):
        browser = self.browser()
        ws = self.workspace
        ws.selected_program_range = (0, 4)
        before = copy.deepcopy(ws.project)
        self.drag(browser, 'mono', self.point(6))
        self.assertEqual(ws.selected_program_range, (4, 8))
        self.assertEqual(grade_at(ws.project, 6).saturation, 0)
        self.assertIsNone(grade_at(ws.project, 2))
        self.assertEqual(len(ws.undo_stack), 1)
        path = self.folder/'drag.multicam.json'
        ws.project.save(path)
        self.assertEqual(grade_at(MulticamProject.load(path), 6).saturation, 0)
        ws.undo(); self.assertEqual(ws.project, before)
        ws.redo(); self.assertEqual(grade_at(ws.project, 6).saturation, 0)
        self.assertEqual(ws.timeline.find_withtag('effect-drop'), ())
        self.assertEqual(self.callback_errors, [])

    def test_transition_snaps_to_seam_and_honors_duration_with_one_undo(self):
        browser = self.browser()
        browser.duration.set('0.8')
        x, y = self.point(4)
        self.drag(browser, 'transition_wipeleft', (x+4, y), release=False)
        self.assertTrue(self.workspace.timeline.find_withtag('effect-drop'))
        self.assertEqual(self.workspace.project.transitions, [])
        browser.list.event_generate('<ButtonRelease-1>', rootx=x+4, rooty=y)
        transition, = self.workspace.project.transitions
        self.assertEqual((transition.time, transition.kind, transition.duration), (4, 'wipeleft', .8))
        self.assertEqual(len(self.workspace.undo_stack), 1)
        self.workspace.undo(); self.assertEqual(self.workspace.project.transitions, [])
        self.assertEqual(self.callback_errors, [])

    def test_invalid_targets_endpoints_busy_escape_and_click_do_not_edit(self):
        browser = self.browser()
        ws = self.workspace
        before = copy.deepcopy(ws.project)
        self.press(browser, 'mono')
        browser.list.event_generate('<ButtonRelease-1>')
        self.assertEqual(ws.project, before)
        for position in (0, 2, 12):
            self.drag(browser, 'transition_fade', self.point(position))
            self.assertEqual(ws.project, before)
        x, y = self.point(6)
        self.drag(browser, 'mono', (x, ws.timeline.winfo_rooty()+3))
        self.assertEqual(ws.project, before)
        self.drag(browser, 'mono', (0, 0))
        self.drag(browser, 'mono', (x, y), release=False)
        browser.list.focus_force()
        browser.list.event_generate('<KeyPress-Escape>')
        self.assertIsNone(browser._effect_drag)
        browser.list.event_generate('<ButtonRelease-1>', rootx=x, rooty=y)
        ws.busy = True
        self.drag(browser, 'mono', (x, y))
        ws.busy = False
        self.assertEqual(ws.project, before)
        self.assertEqual(ws.undo_stack, [])
        self.assertEqual(self.callback_errors, [])

    def test_scrolled_zoomed_timeline_resolves_coordinates_once(self):
        browser = self.browser()
        ws = self.workspace
        ws.zoom = 4
        ws.draw_timeline()
        ws.timeline.xview_moveto(.28)
        self.root.update()
        point = self.point(6)
        target = effect_drop_target(ws, PRESET_BY_KEY['mono'], *point)
        self.assertEqual((target.start, target.end), (4, 8))
        self.drag(browser, 'mono', point)
        self.assertEqual(grade_at(ws.project, 6).saturation, 0)
        self.assertIsNone(grade_at(ws.project, 2))

    def test_asset_drop_forwards_overlay_opacity_and_mask_at_actual_boundary(self):
        browser = self.browser()
        ws = self.workspace
        overlay = OverlayAsset('glow', '光斑', 'screen', '光效', 'glow.mov', 1920, 1080, 30, 60, 2, 'test')
        mask = TransitionMask('m', '画笔', '画笔', 'mask.png')
        presets = (EffectPreset('overlay:test', '光斑', '叠加', '', kind='overlay', asset=overlay),
                   EffectPreset('mask:test', '画笔', '转场', '', kind='mask', asset=mask))
        browser._catalog = presets
        browser._catalog_by_key = {item.key: item for item in presets}
        browser._filter()
        browser.strength.set(42)
        browser.duration.set('.9')
        with patch.object(ws, 'apply_library_overlay', return_value=True) as apply:
            self.drag(browser, 'overlay:test', self.point(6))
            apply.assert_called_once_with(overlay, opacity=.42)
            self.assertEqual(ws.selected_program_range, (4, 8))
        with patch.object(ws, 'apply_library_mask', return_value=True) as apply:
            self.drag(browser, 'mask:test', self.point(8))
            apply.assert_called_once_with(mask, duration=.9)
            self.assertEqual(ws.selected_program_range, (4, 8))

    def test_unmap_cancels_drag_and_clears_highlight(self):
        browser = self.browser()
        self.drag(browser, 'mono', self.point(6), release=False)
        browser._unmap(SimpleNamespace(widget=browser))
        self.assertIsNone(browser._effect_drag)
        self.assertIsNone(browser._effect_drag_after)
        self.assertEqual(self.workspace.timeline.find_withtag('effect-drop'), ())

    def test_floating_library_uses_screen_coordinates_for_main_timeline(self):
        browser = self.browser()
        self.workspace.dock_manager.float_panel('library', geometry='420x580+10000+10000')
        self.root.update()
        self.drag(browser, 'mono', self.point(6))
        self.assertEqual(grade_at(self.workspace.project, 6).saturation, 0)
        self.assertIsNone(grade_at(self.workspace.project, 2))
        self.assertEqual(self.callback_errors, [])


if __name__ == '__main__': unittest.main()
