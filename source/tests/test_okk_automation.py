"""Frame statistics, transactional plans, and portable project copies."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject
from okk_automation import EditSession, build_edit, revision, statistics
from program_edits import set_grade_on_range


def project_fixture(folder):
    return MulticamProject(
        clips=[CameraClip('a', 1, str(folder/'a.mp4'), -2., 12.),
               CameraClip('b', 2, str(folder/'b.mp4'), 0., 10.)],
        cuts=[Cut(0., 1), Cut(4., 2), Cut(7., 1)], fps=30.)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='okk_automation_')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.project = project_fixture(self.folder)
        self.session = EditSession(deepcopy(self.project))

    def test_exact_use_cuts_negative_origin_and_source_in(self):
        report = statistics(self.project)
        self.assertEqual(report['program_cut_count'], 2)
        self.assertEqual(report['camera_switch_count'], 2)
        self.assertEqual([c['used_frames'] for c in report['clips']], [210, 90])
        self.assertEqual(report['clips'][0]['used_ranges'][0]['source_start'], 2.)
        self.assertEqual(report['clips'][0]['used_ranges'][1]['source_start'], 9.)
        self.assertIsNone(report['historical_cut_action_count'])

    def test_range_clips_counts_and_grade_boundaries_do_not_add_cuts(self):
        self.project.range_start, self.project.range_end = 4., 7.
        set_grade_on_range(self.project, 5., 6., ColorGrade(exposure=.1))
        # Grade API preserves boundaries as explicit edit points. Inspecting an
        # existing grade alone must not invent additional razor boundaries.
        self.project.edit_points = []
        report = statistics(self.project)
        self.assertEqual(report['program_cut_count'], 0)
        self.assertEqual(report['duration_frames'], 90)
        self.assertEqual([c['used_frames'] for c in report['clips']], [0, 90])

    def test_fractional_edges_follow_render_clock_and_report_gap(self):
        self.project = MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', .011, 1.)], fps=30.)
        report = statistics(self.project)
        self.assertEqual(report['gap_frames'], 0)
        self.assertEqual(report['clips'][0]['used_frames'], 30)
        self.project.clips[0].start = .1
        report = statistics(self.project)
        self.assertEqual(report['gap_frames'], 3)

    def test_overlapping_recordings_choose_latest_and_aggregate_file(self):
        self.project.clips.append(CameraClip('c', 1, self.project.clips[0].path, 2., 2.))
        report = statistics(self.project)
        counts = {c['id']: c['used_frames'] for c in report['clips']}
        self.assertEqual(counts, {'a': 150, 'b': 90, 'c': 60})
        self.assertEqual(next(f['used_frames'] for f in report['files'] if f['path'].endswith('a.mp4')), 210)

    def test_stack_opaque_occlusion_transparency_and_hidden_tracks(self):
        self.project.video_stack_enabled = True
        self.project.video_stack_order = [2, 1]
        report = statistics(self.project)
        self.assertEqual([c['used_frames'] for c in report['clips']], [0, 300])
        self.project.clips[1].has_alpha = True
        report = statistics(self.project)
        self.assertEqual([c['used_frames'] for c in report['clips']], [300, 300])
        self.project.video_stack_hidden = [2]
        report = statistics(self.project)
        self.assertEqual([c['used_frames'] for c in report['clips']], [300, 0])
        self.assertFalse(report['program_cuts_affect_output'])

    def test_preview_does_not_mutate_and_batch_is_one_undo(self):
        before = revision(self.session.project)
        plan = self.session.preview([{'op': 'split_program', 'time': 2.},
                                     {'op': 'set_camera', 'start': 1., 'end': 3., 'camera': 2}], before)
        self.assertEqual(revision(self.session.project), before)
        applied = self.session.apply(plan['plan_id'], before)
        self.assertNotEqual(applied['revision'], before)
        self.assertEqual(len(self.session.undo_stack), 1)
        self.session.history('undo', applied['revision'])
        self.assertEqual(revision(self.session.project), before)
        redone = self.session.history('redo', before)
        self.assertEqual(redone['revision'], applied['revision'])

    def test_failed_batch_is_atomic_and_refuses_unknown_fields(self):
        before = revision(self.project)
        with self.assertRaises(ValueError):
            build_edit(self.project, [{'op': 'split_program', 'time': 2.},
                                      {'op': 'set_camera', 'start': 3., 'end': 5., 'camera': 99}])
        self.assertEqual(revision(self.project), before)
        with self.assertRaises(ValueError):
            build_edit(self.project, [{'op': 'split_program', 'time': 2., 'typo': True}])

    def test_stale_plan_rejected_after_manual_change(self):
        before = revision(self.session.project)
        plan = self.session.preview([{'op': 'add_marker', 'time': 2.}], before)
        self.session.project.set_camera_name(1, 'changed by user')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.session.apply(plan['plan_id'], before)
        self.assertEqual(self.session.project.timeline_markers, [])

    def test_noop_has_no_undo_and_expired_plan_cannot_apply(self):
        before = revision(self.session.project)
        plan = self.session.preview([{'op': 'split_program', 'time': 4.}], before)
        self.assertFalse(plan['changed'])
        self.session.apply(plan['plan_id'], before)
        self.assertEqual(self.session.undo_stack, [])
        plan = self.session.preview([{'op': 'split_program', 'time': 2.}], before)
        old_revision, candidate, created = self.session.plans[plan['plan_id']]
        self.session.plans[plan['plan_id']] = old_revision, candidate, created-1801
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.session.apply(plan['plan_id'], before)
        self.assertEqual(revision(self.session.project), before)

    def test_source_split_preserves_clock_and_reports_current_boundary(self):
        before = revision(self.session.project)
        plan = self.session.preview([{'op': 'split_source', 'time': 2., 'clip_ids': ['a']}], before)
        self.assertEqual(len(plan['new_clip_ids']), 1)
        self.session.apply(plan['plan_id'], before)
        right = next(c for c in self.session.project.clips if c.id in plan['new_clip_ids'])
        self.assertEqual(right.source_in, 4.)
        self.assertEqual(right.source_origin, -2.)
        self.assertEqual(right.duration, 8.)
        report = statistics(self.session.project)
        self.assertEqual(report['source_split_boundary_count'], 1)
        self.assertEqual(report['current_cut_boundary_count'], 3)
        self.assertEqual(sum(c['used_frames'] for c in report['clips']), 300)

    def test_set_camera_refuses_gap_and_stack_ineffective_edit(self):
        self.project.clips[1].start, self.project.clips[1].duration = 2., 8.
        with self.assertRaisesRegex(ValueError, 'gap'):
            build_edit(self.project, [{'op': 'set_camera', 'start': 0., 'end': 4., 'camera': 2}])
        self.project.video_stack_enabled = True
        with self.assertRaisesRegex(ValueError, 'multicam mode'):
            build_edit(self.project, [{'op': 'set_camera', 'start': 2., 'end': 4., 'camera': 2}])

    def test_save_copy_roundtrip_and_explicit_overwrite_backup(self):
        path = self.folder/'副本.multicam.json'
        before = revision(self.session.project)
        self.session.save_copy(path, before)
        original = path.read_bytes()
        loaded = MulticamProject.load(path)
        self.assertEqual(revision(loaded), before)
        with self.assertRaises(FileExistsError):
            self.session.save_copy(path, before)
        result = self.session.save_copy(path, before, overwrite=True)
        self.assertEqual(Path(result['backup_path']).read_bytes(), original)
        self.assertEqual(list(self.folder.glob('.okk_mcp_*')), [])

    def test_full_project_preserves_other_sections_and_rebases_timeline(self):
        path = self.folder/'完整.rtxproj'
        envelope = {'format': 'rtx-studio-project', 'schema': 2, 'unknown_metadata': {'a': 1},
                    'state': {'export': {'codec_var': 'libx264'}, 'okk_multicam': {
                        'base_directory': str(self.folder), 'project': self.project.to_payload(self.folder),
                        'settings': {'proxy_var': True}}}}
        path.write_text(json.dumps(envelope), encoding='utf-8')
        before_bytes = path.read_bytes()
        self.session.open_project(path, revision(self.session.project))
        target = self.folder/'other'/'复制.rtxproj'
        self.session.save_copy(target, revision(self.session.project))
        saved = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(saved['unknown_metadata'], envelope['unknown_metadata'])
        self.assertEqual(saved['state']['export'], envelope['state']['export'])
        self.assertEqual(saved['state']['okk_multicam']['settings'], {'proxy_var': True})
        second = EditSession()
        second.open_project(target, revision(second.project))
        self.assertEqual(revision(second.project), revision(self.project))
        self.assertEqual(path.read_bytes(), before_bytes)

    def test_boolean_nan_and_duplicate_ids_rejected(self):
        for op in [{'op': 'split_program', 'time': True}, {'op': 'set_range', 'start': 0., 'end': float('nan')},
                   {'op': 'split_source', 'time': 2., 'clip_ids': ['a', 'a']},
                   {'op': 'set_audio_enabled', 'track': True, 'enabled': True}]:
            with self.subTest(op=op), self.assertRaises(ValueError):
                build_edit(self.project, [op])


if __name__ == '__main__':
    unittest.main(verbosity=2)
