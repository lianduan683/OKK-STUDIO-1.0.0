"""Native exposure UI, user-owned API configuration, and authenticated live MCP."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_finishing_workspace as fixture
from exposure_check import ExposureService
from exporter_core import CREATE_NO_WINDOW
from multicam_core import CameraClip, Cut, MulticamProject
from okk_mcp_bridge import LocalBridge, LiveClient
from vision_review import PROTOCOLS


class ExposureUITests(unittest.TestCase):
    setUp_base = fixture.FinishingPageTests.setUp
    tearDown = fixture.FinishingPageTests.tearDown
    _create_app = fixture.FinishingPageTests._create_app
    _destroy_app = fixture.FinishingPageTests._destroy_app
    _settle = fixture.FinishingPageTests._settle
    _assert_inside = fixture.FinishingPageTests._assert_inside

    def setUp(self):
        self.setUp_base()
        self.folder = Path(self.session_directory.name)
        ffmpeg = Path(__file__).resolve().parents[1] / 'bin' / 'ffmpeg.exe'
        if not ffmpeg.is_file(): self.skipTest('Bundled FFmpeg unavailable')
        project = MulticamProject(fps=10)
        for camera, color in ((1, 'black'), (2, 'white'), (3, 'gray')):
            path = self.folder / f'{camera}.mp4'
            result = subprocess.run([str(ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                f'color=c={color}:s=96x54:r=10:d=1', '-c:v', 'libx264', '-an', str(path)],
                capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0)
            project.clips.append(CameraClip(str(camera), camera, str(path), 0, 1, has_audio=False))
        project.cuts = [Cut(0, 3)]
        ws = self.workspace
        ws.project = project; ws.fps_var.set('10'); ws.selected_program_range = (0., 1.)
        ws._refresh(reconfigure=False)
        self.app.caps.ffmpeg_path = str(ffmpeg)
        ws._exposure_service = ExposureService(str(ffmpeg), self.folder / 'frames')
        self.app._select_page('color')
        self.panel = ws.color_workspace.exposure_panel
        self.panel.profile_path = self.folder / 'model-profile.json'
        ws.color_workspace.exposure_button.invoke()
        self._settle()

    def wait(self, predicate):
        end = time.monotonic() + 12
        while time.monotonic() < end:
            self.app.update()
            if predicate(): return
            time.sleep(.02)
        self.fail('Exposure UI job did not finish')

    def scan(self):
        self.panel.scope_var.set('所有机位'); self.panel.interval_var.set('0.5')
        self.panel.start()
        self.wait(lambda: self.panel.report and self.panel.report['status'] == 'complete')

    def test_scan_preview_navigation_and_model_review_leave_project_unchanged(self):
        original = copy.deepcopy(self.workspace.project)
        history = len(self.workspace.undo_stack)
        self.scan()
        self.assertEqual(len(self.panel.tree.get_children()), 6)
        self._assert_inside(self.panel.scan_button)
        self._assert_inside(self.panel.tree)
        self.assertFalse(self.workspace.color_workspace.curve_editor.winfo_ismapped())
        self.panel.select_suspects(); self.app.update()
        self.assertEqual(len(self.panel.tree.selection()), 4)
        self.assertTrue(self.panel.preview.cget('image'))
        self._assert_inside(self.panel.details_label)
        self.assertTrue(self.panel.details_var.get())
        self.panel.base_var.set('http://127.0.0.1/v1'); self.panel.model_var.set('fixture')
        self.panel.key_var.set('session-only-key')
        judgment = dict(classification='normal', confidence=.8, reason='舞台背景暗，主体细节仍可辨。', intentional_lighting=True, regions=[])
        with patch('exposure_check.request_review', return_value=judgment) as request:
            self.panel.review()
            self.wait(lambda: self.panel.report['review_status'] == 'complete')
        self.assertEqual(request.call_count, 4)
        self.assertNotIn('session-only-key', json.dumps(self.panel.report))
        self.assertEqual(self.workspace.project, original)
        self.assertEqual(len(self.workspace.undo_stack), history)
        self.panel.locate()
        self.assertAlmostEqual(self.workspace.current, self.panel.report['frames'][0]['timeline_time'])
        self.assertEqual(self.callback_errors, [])

    def test_all_protocol_settings_save_without_a_key(self):
        self.panel.tabs.select(1); self._settle()
        self.panel.key_var.set('session-only-key')
        self.panel.base_var.set('http://127.0.0.1/v1'); self.panel.model_var.set('user-model')
        for protocol, label in PROTOCOLS.items():
            with self.subTest(protocol=protocol):
                self.panel.protocol_var.set(label); self.panel.save_connection()
                value = json.loads(self.panel.profile_path.read_text('utf-8'))
                self.assertEqual(value['protocol'], protocol)
                self.assertNotIn('session-only-key', self.panel.profile_path.read_text('utf-8'))
                self.assertEqual(self.panel.config().protocol, protocol)

    def test_live_bridge_exposure_results_appear_in_the_same_ui(self):
        directory = self.folder / 'bridge'
        with patch('okk_mcp_bridge.discovery_directory', return_value=directory):
            bridge = LocalBridge(self.app)
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    def call(method, **params):
                        future = pool.submit(lambda: LiveClient(bridge.instance).call(method, **params))
                        self.wait(future.done)
                        return future.result()
                    state = call('snapshot')
                    result = call('exposure_start', expected_revision=state['revision'], options={'scope': 'cameras', 'interval': .5})
                    self.wait(lambda: self.panel.report and self.panel.report['scan_id'] == result['scan_id'] and self.panel.report['status'] == 'complete')
                    record = self.panel.report['frames'][0]
                    judgment = dict(classification='uncertain', confidence=.5, reason='需核对拍摄意图。', regions=[])
                    call('exposure_submit', scan_id=result['scan_id'], scan_revision=result['scan_revision'],
                         reviews=[{'frame_id': record['frame_id'], 'result': judgment}], model='fixture-harness')
                    self.wait(lambda: self.panel.report['frames'][0]['review'] is not None)
                    self.assertEqual(self.panel.report['frames'][0]['review']['source'], 'harness')
                    self.assertEqual(call('snapshot')['revision'], state['revision'])
            finally:
                bridge.close()


if __name__ == '__main__': unittest.main()
