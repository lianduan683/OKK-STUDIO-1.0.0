"""Autosave settings, timer lifecycle and real atomic project writes."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from autosave_settings import (AutosaveSettingsMixin, load_preferences,
                               save_preferences, validate_interval)
from multicam_core import MulticamProject
from project_file import state_digest
from studio_project import StudioProjectMixin


class AutosavePreferencesTests(unittest.TestCase):
    def test_roundtrip_defaults_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            self.assertEqual(load_preferences(path), (True, 30))
            for enabled, seconds in ((False, 137), (True, 1), (True, 86400)):
                save_preferences(enabled, seconds, path)
                self.assertEqual(load_preferences(path), (enabled, seconds))
            for value in (0, -1, 86401, 1.5, True, 'nan', 'inf', '', None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    validate_interval(value)
            for value in ('[]', '{"enabled": true}', '{"enabled": 1, "interval_seconds": 30}', 'broken'):
                path.write_text(value, encoding='utf-8')
                self.assertEqual(load_preferences(path), (True, 30))


class FakeApp(AutosaveSettingsMixin, StudioProjectMixin):
    pass


class AutosaveLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.app = FakeApp()
        self.app.autosave_enabled, self.app.autosave_interval_seconds = True, 137
        self.app._project_autosave_after = None
        self.app._project_autosave_digest = 'old'
        self.app._project_saved_digest = 'original'
        self.app._closing = self.app.working = False
        self.app.project_path = None
        self.app.after = Mock(side_effect=lambda delay, callback: f'timer-{delay}')
        self.app.after_cancel = Mock()
        self.app._project_update_title = Mock()
        self.app._log = Mock()
        self.payload = {'state': {'revision': 2}, 'project_path': ''}
        self.app._project_current_digest = lambda: state_digest(self.payload['state'])
        self.app.multicam_workspace = SimpleNamespace(
            project=MulticamProject(), project_path='', busy=False, dirty=True,
            project_label=Mock(), _read_range=lambda: (0, None, 30),
            _capture_monitor_layout=lambda: dict(rows=2, columns=4, page=0))
        self.patches = [patch('studio_project.app_directory', return_value=self.folder),
                        patch('studio_project.build_project_payload', return_value=self.payload),
                        patch('autosave_settings.preferences_path', return_value=self.folder/'settings.json')]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temporary.cleanup()

    def test_reschedule_cancel_disable_and_restart(self):
        self.app._schedule_project_autosave()
        self.app.after.assert_called_with(137000, self.app._project_autosave_tick)
        self.app._apply_autosave_settings(True, 5)
        self.app.after_cancel.assert_called_with('timer-137000')
        self.app.after.assert_called_with(5000, self.app._project_autosave_tick)
        self.app._apply_autosave_settings(False, 5)
        self.app.after_cancel.assert_called_with('timer-5000')
        self.assertIsNone(self.app._project_autosave_after)
        self.app._project_autosave_tick()
        self.assertFalse((self.folder/'projects/autosave.rtxproj').exists())
        self.assertFalse(self.app._write_project_autosave())
        self.app._apply_autosave_settings(True, 7)
        self.app.after.assert_called_with(7000, self.app._project_autosave_tick)
        self.assertEqual(load_preferences(self.folder/'settings.json'), (True, 7))

    def test_unnamed_project_recovery_and_unchanged_state_skips_writes(self):
        self.app._project_autosave_tick()
        path = self.folder/'projects/autosave.rtxproj'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['state'], self.payload['state'])
        self.assertEqual(self.app._project_saved_digest, 'original')
        with patch('studio_project.write_project_file') as write:
            self.app._project_autosave_tick()
            write.assert_not_called()

    def test_named_full_project_written_and_marked_saved(self):
        self.app.project_path = self.folder/'named.rtxproj'
        self.app._project_autosave_tick()
        self.assertEqual(json.loads(self.app.project_path.read_text(encoding='utf-8'))['state'], self.payload['state'])
        self.assertEqual(self.app._project_saved_digest, state_digest(self.payload['state']))
        self.assertFalse(self.app.multicam_workspace.dirty)

    def test_named_standalone_project_writes_current_format_and_layout(self):
        ws = self.app.multicam_workspace
        ws.project_path = str(self.folder/'named.multicam.json')
        self.app._project_autosave_tick()
        saved = MulticamProject.load(ws.project_path)
        self.assertEqual(saved.monitor_layout, dict(rows=2, columns=4, page=0))
        self.assertEqual(saved.fps, 30)
        self.assertFalse(ws.dirty)

    def test_busy_and_edit_gestures_defer_without_saving_half_edit(self):
        for key in ('busy', '_program_boundary_drag', '_selection_gesture', '_timeline_scrubbing'):
            with self.subTest(key=key):
                setattr(self.app.multicam_workspace, key, True)
                with patch('studio_project.write_project_file') as write:
                    self.app._project_autosave_tick()
                    write.assert_not_called()
                self.app.after.assert_called_with(1000, self.app._project_autosave_tick)
                setattr(self.app.multicam_workspace, key, False)

    def test_write_failure_keeps_dirty_state_and_retries(self):
        self.app.project_path = self.folder/'named.rtxproj'
        self.app.project_path.write_text('original', encoding='utf-8')
        with patch('studio_project.write_project_file', side_effect=OSError('disk full')):
            self.app._project_autosave_tick()
        self.assertEqual(self.app._project_autosave_digest, 'old')
        self.assertEqual(self.app._project_saved_digest, 'original')
        self.assertTrue(self.app.multicam_workspace.dirty)
        self.assertEqual(self.app.project_path.read_text(encoding='utf-8'), 'original')
        self.assertEqual(self.app._autosave_error, 'disk full')
        self.app._project_autosave_tick()
        self.assertEqual(self.app._autosave_error, '')


if __name__ == '__main__': unittest.main()
