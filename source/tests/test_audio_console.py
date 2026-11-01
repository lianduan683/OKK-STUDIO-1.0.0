import copy
import unittest
from types import SimpleNamespace
import test_audio_workspace as fixture
from audio_mixer import strip
from studio_project import APP_VERSION
from multicam_core import MulticamProject,CameraClip,Cut
from rtx5090_exporter import enable_dpi_awareness

enable_dpi_awareness()

class ConsoleTests(unittest.TestCase):
    setUp=fixture.AudioWorkspaceTests.setUp
    tearDown=fixture.AudioWorkspaceTests.tearDown
    _create_app=fixture.AudioWorkspaceTests._create_app
    _destroy_app=fixture.AudioWorkspaceTests._destroy_app
    _settle=fixture.AudioWorkspaceTests._settle
    _assert_inside=fixture.AudioWorkspaceTests._assert_inside

    def seed(self):
        ws=self.workspace
        ws.project=MulticamProject(clips=[CameraClip('a',1,'a.mp4',0,6,has_audio=True),
            CameraClip('b',2,'b.mp4',0,6,has_audio=True)],cuts=[Cut(0,1)],audio_track_enabled={1:True,2:True})
        ws._refresh(reconfigure=False)
        self.app._select_page('audio');self._settle()
        return ws.audio_workspace

    def test_fader_group_selection_single_undo_and_independent_channels(self):
        panel=self.seed();ws=self.workspace
        channel=panel.console.channels['2']
        channel.press(SimpleNamespace(y=100))
        channel.release()
        self.assertEqual(ws.project.audio_mixer,{})
        channel.press(SimpleNamespace(y=100))
        channel.motion(SimpleNamespace(y=130,state=0))
        self.assertEqual(ws.project.audio_mixer,{})
        channel.release()
        gain=strip(ws.project,'2')['gain_db']
        self.assertLess(gain,0)
        self.assertEqual(strip(ws.project,'1')['gain_db'],0)
        self.assertEqual(panel.target,'2')
        ws.undo();self._settle();self.assertEqual(ws.project.audio_mixer,{})
        ws.redo();self._settle();self.assertEqual(strip(ws.project,'2')['gain_db'],gain)
        panel.console.channels['1'].flag('solo')
        self.assertTrue(strip(ws.project,'1')['solo'])
        self.assertEqual(strip(ws.project,'2')['gain_db'],gain)
        panel.console.master_strip.commit(-3.)
        self.assertEqual(strip(ws.project,'master')['gain_db'],-3.)
        self.assertEqual(strip(ws.project,'2')['gain_db'],gain)
        self.assertIn(APP_VERSION,self.app.title())

    def test_eq_drag_cancelled_when_project_changes_and_ui_routes_preserved(self):
        panel=self.seed();ws=self.workspace
        project_before=copy.deepcopy(ws.project)
        panel.select_target('1');panel.tabs.select(panel.pages['均衡器']);self._settle()
        x,y=panel._eq_points[2]
        panel._eq_press(SimpleNamespace(x=x,y=y))
        panel._eq_motion(SimpleNamespace(x=x,y=y-20))
        self.assertEqual(ws.project.audio_mixer,{})
        panel._eq_release()
        self.assertTrue(strip(ws.project,'1')['eq_enabled'])
        self.assertGreater(strip(ws.project,'1')['eq'][2],0)
        ws.undo();self._settle()
        self.assertEqual(ws.project.audio_mixer,{})
        panel._eq_press(SimpleNamespace(x=x,y=y-30))
        ws.project=copy.deepcopy(project_before)
        panel._eq_release()
        self.assertEqual(ws.project.audio_mixer,{})
        windows=tuple(v.winfo_id() for v in ws.program_views)
        for page in ('color','audio','delivery','multicam'):
            self.app._select_page(page);self._settle()
            self.assertEqual(tuple(v.winfo_id() for v in ws.program_views),windows)
        self.assertFalse(self.callback_errors)

if __name__=='__main__':unittest.main()
