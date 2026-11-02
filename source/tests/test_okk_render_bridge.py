"""UI-thread bridge controls use native jobs without dialogs or subprocesses."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exporter_core import Capabilities, ExportSettings
from multicam_core import CameraClip, Cut, MulticamProject
from okk_mcp_bridge import WorkspaceSession


class Variable:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class RenderBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='okk-render-bridge-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.settings = ExportSettings(codec='hevc_nvenc', video_bitrate='80M',
                                       decode_mode='cuda', resolution='4k', auto_parallel=True)
        self.project = MulticamProject(fps=30., clips=[CameraClip('a', 1, 'original.mov', 0., 2.)],
                                       cuts=[Cut(0., 1)], range_end=2.)
        self.finishing = SimpleNamespace(jobs=[], serial=0, active_job=None, running=False,
            _add_card=Mock(), selected_job=Variable(), queue_status=Variable())
        self.ws = SimpleNamespace(project=self.project, project_path='', busy=False, current=0.,
            finishing_workspace=self.finishing, _read_range=lambda: (0., 2., 30.),
            output_var=Variable(str(self.folder/'delivery.mov')), status=Variable(),
            _export_last_snapshot={}, dirty=False)
        self.ws.prepare_delivery_export = Mock(side_effect=lambda:
            (deepcopy(self.project), deepcopy(self.settings), self.ws.output_var.get()))
        self.app = SimpleNamespace(multicam_workspace=self.ws, working=False, project_path=None,
            caps=Capabilities(ffmpeg_path='ffmpeg', ffprobe_path='ffprobe', nvenc_codecs={'hevc_nvenc'}),
            _project_update_title=Mock(), _project_is_dirty=Mock(return_value=False),
            _close=Mock(), after=Mock(), _log=Mock())
        self.finishing.start_queue = Mock(side_effect=self.start_queue)
        self.finishing.stop_queue = Mock(side_effect=self.stop_queue)
        self.session = WorkspaceSession(self.app)
        self.delivery = patch('delivery_panel.make_delivery_settings',
                              side_effect=lambda ws: (deepcopy(self.settings), '.mov'))
        self.delivery.start()
        self.addCleanup(self.delivery.stop)
        self.hardware = patch('hardware_encoders.available_codecs', return_value={'hevc_nvenc'})
        self.hardware.start()
        self.addCleanup(self.hardware.stop)

    def start_queue(self):
        self.finishing.running = True
        self.finishing.active_job = self.finishing.jobs[0]
        self.finishing.active_job.state = '正在渲染'
        self.app.working = self.ws.busy = True

    def stop_queue(self):
        self.finishing.running = False

    def enqueue(self):
        status = self.session.render_status()
        return self.session.render_enqueue_current(status['revision'], status['queue_revision'])

    def test_status_and_enqueue_preserve_delivered_settings_and_avoid_duplicates(self):
        result = self.enqueue()
        self.assertTrue(result['created'])
        self.assertEqual(len(self.finishing.jobs), 1)
        self.assertEqual(result['jobs'][0]['settings']['video_bitrate'], '80M')
        self.assertTrue(result['jobs'][0]['settings']['auto_parallel'])
        again = self.enqueue()
        self.assertFalse(again['created'])
        self.assertEqual(again['job_id'], result['job_id'])
        self.assertEqual(len(self.finishing.jobs), 1)
        self.finishing._add_card.assert_called_once()
        self.assertIsNot(self.finishing.jobs[0].project, self.project)

    def test_target_bitrate_is_set_only_with_fresh_idle_queue(self):
        self.ws.bitrate_var = Variable('80M')
        status = self.session.render_status()
        self.session.render_set_bitrate('20M',status['revision'],status['queue_revision'])
        self.assertEqual(self.ws.bitrate_var.get(),'20M')
        self.app.working = True
        with self.assertRaises(ValueError):
            self.session.render_set_bitrate('10M',status['revision'],status['queue_revision'])

    def test_revision_guards_reject_changed_project_delivery_and_conflicting_job(self):
        status = self.session.render_status()
        self.settings.video_bitrate = '40M'
        with self.assertRaisesRegex(ValueError, 'settings changed'):
            self.session.render_enqueue_current(status['revision'], status['queue_revision'])
        self.enqueue()
        self.settings.video_bitrate = '80M'
        with self.assertRaisesRegex(ValueError, 'different pending job'):
            self.enqueue()
        with self.assertRaisesRegex(ValueError, 'Project changed'):
            self.session.render_enqueue_current('stale', self.session.render_status()['queue_revision'])

    def test_metrics_do_not_invalidate_queue_identity_and_start_uses_existing_job(self):
        initial = self.enqueue()
        self.finishing.jobs[0].progress = 7.
        self.finishing.jobs[0].detail = 'sampled metric'
        status = self.session.render_status()
        self.assertEqual(status['queue_revision'], initial['queue_revision'])
        started = self.session.render_start_queue(status['revision'], status['queue_revision'])
        self.assertTrue(started['working'])
        self.assertEqual(started['active_job_id'], initial['job_id'])
        self.finishing.start_queue.assert_called_once()

    def test_remote_start_never_overwrites_existing_output(self):
        self.enqueue()
        path = Path(self.ws.output_var.get())
        path.write_bytes(b'existing completed delivery')
        status = self.session.render_status()
        with self.assertRaisesRegex(ValueError, 'will not overwrite'):
            self.session.render_start_queue(status['revision'], status['queue_revision'])
        self.finishing.start_queue.assert_not_called()
        self.assertEqual(path.read_bytes(), b'existing completed delivery')

    def test_cancel_only_targets_the_observed_active_render(self):
        status = self.enqueue()
        self.session.render_start_queue(status['revision'], status['queue_revision'])
        with self.assertRaisesRegex(ValueError, 'Active render job changed'):
            self.session.render_cancel(status['revision'], 'different-job')
        self.finishing.stop_queue.assert_not_called()
        result = self.session.render_cancel(status['revision'], status['job_id'])
        self.assertTrue(result['cancellation_requested'])
        self.finishing.stop_queue.assert_called_once()

    def test_save_while_rendering_writes_existing_project_with_backup(self):
        self.app.project_path = self.folder/'project.rtxproj'
        self.app.project_path.write_bytes(b'old saved project')
        self.app.working = self.ws.busy = True
        payload = {'state': {'fixture': 'saved settings'}}
        with patch('project_file.build_project_payload', return_value=payload), \
             patch('project_file.state_digest', return_value='saved digest'), \
             patch('project_file.write_project_file', side_effect=lambda path, data:
                   Path(path).write_text(json.dumps(data), 'utf-8')):
            result = self.session.project_save(self.session.render_status()['revision'])
        self.assertEqual(Path(result['backup_path']).read_bytes(), b'old saved project')
        self.assertEqual(json.loads(self.app.project_path.read_text('utf-8')), payload)
        self.assertEqual(self.app._project_saved_digest, 'saved digest')
        self.assertTrue(self.app.working)

    def test_close_requires_saved_idle_state_and_rechecks_before_deferred_close(self):
        revision = self.session.render_status()['revision']
        self.app._project_is_dirty.return_value = True
        with self.assertRaisesRegex(ValueError, 'Save the current project'):
            self.session.app_close(revision)
        self.app._project_is_dirty.return_value = False
        self.assertTrue(self.session.app_close(revision)['close_scheduled'])
        self.app._close.assert_not_called()
        callback = self.app.after.call_args.args[1]
        self.app.working = True
        callback()
        self.app._close.assert_not_called()
        self.app.working = False
        self.session.app_close(revision)
        self.app.after.call_args.args[1]()
        self.app._close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
