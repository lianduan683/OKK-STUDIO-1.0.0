"""Project settings, persistent monitor grid, measured FPS and real exports."""
import copy
import json
from pathlib import Path
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from multicam_core import MulticamProject, CameraClip
from playback_metrics import FrameRate, PlaybackMetricsPlayerMixin
from project_settings import DEFAULT_SETTINGS
from studio_state import collect_okk, restore_okk
from delivery_panel import make_delivery_settings
from tests import test_program_edit_ui as fixture


class ProjectFormatTests(unittest.TestCase):
    def test_new_format_roundtrip_and_old_format_defaults(self):
        project = MulticamProject(project_settings={**DEFAULT_SETTINGS, 'sample_rate': 192000, 'bit_depth': '32bit 浮点'},
                                  monitor_layout=dict(rows=2, columns=4, page=3))
        payload = project.to_payload('.')
        self.assertEqual(payload['version'], 14)
        loaded = MulticamProject.from_payload(payload, '.')
        self.assertEqual(loaded.project_settings, project.project_settings)
        self.assertEqual(loaded.monitor_layout, project.monitor_layout)
        old = MulticamProject().to_payload('.')
        self.assertNotIn('project_settings', old)
        self.assertEqual(MulticamProject.from_payload(old, '.').project_settings, {})

    def test_invalid_settings_and_layout_rejected(self):
        for value in ({**DEFAULT_SETTINGS, 'width': 1921}, {**DEFAULT_SETTINGS, 'sample_rate': True},
                      {**DEFAULT_SETTINGS, 'bit_depth': '64bit'}, {'width': 1920}, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MulticamProject(project_settings=value).validate()
        for value in (dict(rows=0), dict(page=-1), dict(rows=True), dict(untrusted='value')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MulticamProject(monitor_layout=value).validate()


class ProjectSettingsUITests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def test_cancel_and_invalid_apply_do_not_mutate_project(self):
        workspace = self.workspace
        original = copy.deepcopy(workspace.project)
        workspace.show_project_settings()
        workspace._project_settings_vars['fps'].set('nan')
        workspace._project_settings_apply()
        self.assertTrue(workspace._project_settings_window.winfo_exists())
        self.assertEqual(workspace.project, original)
        workspace._project_settings_window.destroy()
        self.assertEqual(workspace.project, original)

    def test_apply_updates_model_delivery_and_undo_frame_rate(self):
        workspace = self.workspace
        old_fps = workspace.project.fps
        workspace.show_project_settings()
        variables = workspace._project_settings_vars
        variables['width'].set('1080'); variables['height'].set('1920')
        variables['fps'].set('59.94'); variables['sample_label'].set('192 kHz')
        variables['bit_depth'].set('32bit 浮点')
        workspace._project_settings_apply()
        self.assertAlmostEqual(workspace.project.fps, 60000/1001)
        self.root.update_idletasks()
        self.assertAlmostEqual(workspace.program_surface.winfo_width()/workspace.program_surface.winfo_height(),
                               1080/1920, delta=.01)
        settings, suffix = make_delivery_settings(workspace)
        self.assertEqual((settings.resolution, settings.audio, settings.audio_sample_rate, suffix),
                         ('timeline', 'pcm_f32le', 192000, '.mov'))
        workspace.undo()
        self.assertEqual(workspace.project.project_settings, {})
        self.assertEqual(float(workspace.fps_var.get()), old_fps)
        self.assertEqual(self.callback_errors, [])

    def test_standalone_and_full_session_restore_grid_page_and_settings(self):
        workspace = self.workspace
        self.root.multicam_workspace = workspace
        workspace.project.clips = [CameraClip(str(i), i, str(self.folder/'a.mp4'), 0, 10) for i in range(1, 31)]
        workspace.project.project_settings = dict(DEFAULT_SETTINGS)
        workspace._refresh()
        workspace.set_camera_grid(2, 4)
        workspace._change_camera_page(2)
        self.assertEqual(workspace.camera_page, 2)
        path = self.folder/'布局.multicam.json'
        workspace.project_path = str(path)
        self.assertTrue(workspace.save())
        workspace.set_camera_grid(3, 3)
        workspace.load_project(str(path))
        self.assertEqual((workspace.camera_rows, workspace.camera_columns, workspace.camera_page), (2, 4, 2))
        self.assertEqual(workspace._visible_camera_ids, tuple(range(17, 25)))
        payload = collect_okk(self.root)
        workspace.set_camera_grid(1, 1)
        restore_okk(self.root, payload)
        self.assertEqual((workspace.camera_rows, workspace.camera_columns, workspace.camera_page), (2, 4, 2))
        self.assertEqual(workspace.project.project_settings, DEFAULT_SETTINGS)
        self.assertEqual(self.callback_errors, [])

    def test_full_session_keeps_explicit_delivery_override(self):
        workspace = self.workspace
        self.root.multicam_workspace = workspace
        workspace.project.project_settings = dict(DEFAULT_SETTINGS)
        workspace.app.resolution_var.set('4K')
        workspace.delivery_audio_format_var.set('AAC')
        workspace.delivery_audio_sample_rate_var.set('48000')
        workspace.delivery_container_var.set('MP4')
        value = collect_okk(self.root)
        restore_okk(self.root, value)
        self.assertEqual(workspace.app.resolution_var.get(), '4K')
        self.assertEqual(workspace.delivery_audio_format_var.get(), 'AAC')
        self.assertEqual(workspace.delivery_container_var.get(), 'MP4')


class PlaybackRateTests(unittest.TestCase):
    def test_gpu_controller_exposes_actual_backend_frame_counter(self):
        from gpu_live_controller import GpuLiveSession
        session = GpuLiveSession(0)
        self.assertIsNone(session.presented_video_frames)
        session._backend = SimpleNamespace(presented_video_frames=42)
        self.assertEqual(session.presented_video_frames, 42)

    def test_rate_uses_frames_and_real_elapsed_time_and_stalls_decay_to_zero(self):
        meter = FrameRate()
        self.assertIsNone(meter.sample(0, 0))
        self.assertAlmostEqual(meter.sample(.5, 15), 30)
        self.assertAlmostEqual(meter.sample(1, 30), 30)
        for timestamp in (1.5, 2, 2.5, 3): result = meter.sample(timestamp, 30)
        self.assertEqual(result, 0)
        self.assertIsNone(meter.sample(3.5, 0))

    def test_source_lowest_rate_and_program_independent_drops_and_pause(self):
        class Monitor(PlaybackMetricsPlayerMixin):
            def _emit(self, kind, payload): self.latest = payload
        class Controller:
            frame, drop = 0, 0
            def command(self, command, timeout):
                return json.dumps(dict(frame=self.frame, drop=self.drop, decoder_drop=0, seeking=False))
        fast, slow, program = Controller(), Controller(), Controller()
        def slot(controller): return SimpleNamespace(active=True, controller=controller, key='media')
        monitor = Monitor()
        monitor._slots = {1: slot(fast), 2: slot(slow)}
        monitor._program = slot(program)
        monitor._playing = True
        with patch('playback_metrics.time.monotonic', return_value=0): monitor._poll_playback_metrics()
        fast.frame, slow.frame, program.frame, program.drop = 30, 15, 30, 5
        with patch('playback_metrics.time.monotonic', return_value=1): monitor._poll_playback_metrics()
        self.assertEqual(monitor.latest['source'], 15)
        self.assertEqual(monitor.latest['program'], 25)
        monitor._playing = False
        with patch('playback_metrics.time.monotonic', return_value=2): monitor._poll_playback_metrics()
        self.assertEqual(monitor.latest, dict(source=None, program=None, playing=False))
        self.assertFalse(monitor._metrics_meters)


if __name__ == '__main__': unittest.main()
