from __future__ import annotations
import copy
from unittest.mock import patch
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_delivery_ui as fixture
from multicam_core import MulticamProject,CameraClip,Cut

class AudioWorkspaceTests(unittest.TestCase):
    setUp=fixture.DeliveryTkTests.setUp
    tearDown=fixture.DeliveryTkTests.tearDown
    _create_app=fixture.DeliveryTkTests._create_app
    _destroy_app=fixture.DeliveryTkTests._destroy_app
    _settle=fixture.DeliveryTkTests._settle
    _assert_inside=fixture.DeliveryTkTests._assert_inside

    def test_audio_page_settings_undo_plugin_discovery_and_layout_restore(self):
        ws=self.workspace
        ws.project=MulticamProject(clips=[CameraClip('one',1,'one.mp4',0,6,has_audio=True)],cuts=[Cut(0,1)])
        ws._refresh(reconfigure=False)
        self.app._select_page('multicam');self._settle()
        layout=copy.deepcopy(ws.dock_manager.serialize());windows=tuple(w.winfo_id() for w in ws.program_views)
        self.app.page_buttons['audio'].invoke();self._settle()
        panel=ws.audio_workspace
        self.assertEqual(ws.finishing_workspace.mode,'audio')
        self._assert_inside(panel);self._assert_inside(ws.timeline)
        for page in panel.tabs.tabs():
            panel.tabs.select(page);self._settle()
            self.assertTrue(panel.tabs.nametowidget(page).winfo_ismapped())
        panel.tabs.select(panel.pages['FabFilter / VST3']);self._settle()
        self._assert_inside(panel.add_plugin_button,parent=panel)
        self._assert_inside(panel.plugin_choice,parent=panel)
        panel.enabled_var.set(True);panel.vars['gain_db'].set(-6.);panel.apply()
        self.assertEqual(ws.project.audio_mixer['strips']['master']['gain_db'],-6.)
        ws.undo();self._settle();self.assertEqual(ws.project.audio_mixer,{})
        ws.redo();self._settle();self.assertEqual(ws.project.audio_mixer['strips']['master']['gain_db'],-6.)
        with patch('audio_workspace.discover_plugins', return_value=[r'C:\test\FabFilter Pro-Q.vst3']):
            panel.scan()
        self.assertTrue(any('Pro-Q' in str(path) for path in panel.catalog))
        for mode in ('color','delivery','audio','multicam'):
            self.app._select_page(mode);self._settle()
            self.assertEqual(tuple(w.winfo_id() for w in ws.program_views),windows)
        self.assertEqual(ws.dock_manager.serialize(),layout)
        self.assertFalse(self.callback_errors)

if __name__=='__main__':unittest.main()
