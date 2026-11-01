"""Exact timeline selection, anchored audio and the user-facing sync picker."""
import copy
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock

from editor_core import AlignmentResult
from multicam_core import CameraClip, MulticamProject
from reference_sync import align_camera_tracks
from timeline_audio import MasterAudioClip
from timeline_sync import selected_video_clips, reference_audio_clips
from tests import test_sync_timeline_ui as fixture
from tests import test_reference_sync as media_fixture


class SelectedSyncTests(unittest.TestCase):
    setUp = fixture.SyncTimelineTkTests.setUp
    tearDown = fixture.SyncTimelineTkTests.tearDown
    _set_working = fixture.SyncTimelineTkTests._set_working
    _starts = fixture.SyncTimelineTkTests._starts
    _terminal_jobs = fixture.SyncTimelineTkTests._terminal_jobs
    _consume_jobs = fixture.SyncTimelineTkTests._consume_jobs

    def run_alignment(self, **options):
        def analyze(ffmpeg, clips, master, **kwargs):
            return [AlignmentResult(-(10+index), .95, 50, 'selected-test')
                    for index, _ in enumerate(clips)]
        with mock.patch('multicam_ui.align_camera_tracks', side_effect=analyze) as backend:
            self.workspace.align(True, **options)
            self.assertTrue(self.workspace.sync_running)
            jobs = self._terminal_jobs()
            self.assertEqual(jobs[-1][0], 'aligned')
            self._consume_jobs(jobs)
        return backend.call_args

    def test_only_exact_selected_clips_move_including_two_on_same_track(self):
        w = self.workspace
        w._set_timeline_selection({('clip', 'a'), ('clip', 'b')})
        call = self.run_alignment(use_master=True)
        self.assertEqual([clip.id for clip in call.args[1]], ['a', 'b'])
        self.assertEqual(self._starts(), dict(a=10, b=11, c=6, d=20))
        self.assertEqual(w.project.master_audio, 'continuous-master.wav')
        self.assertEqual(len(w.undo_stack), 1)
        w.undo()
        self.assertEqual(self._starts(), self.original_starts)
        w.redo()
        self.assertEqual(self._starts(), dict(a=10, b=11, c=6, d=20))

    def test_same_camera_unselected_parts_are_excluded(self):
        self.workspace._set_timeline_selection({('clip', 'a'), ('clip', 'd')})
        call = self.run_alignment(use_master=True)
        self.assertEqual([clip.id for clip in call.args[1]], ['a', 'd'])
        self.assertEqual(self._starts()['b'], 2)
        self.assertEqual(self._starts()['c'], 6)

    def test_tree_multiple_clips_are_exact_but_explicit_header_expands(self):
        p = self.workspace.project
        self.assertEqual([c.id for c in selected_video_clips(p, set(), ('a', 'b'))], ['a', 'b'])
        self.assertEqual([c.id for c in selected_video_clips(p, set(), ('camera_1',))], ['a', 'd'])
        self.assertEqual([c.id for c in selected_video_clips(p, {('clip', 'c')}, ('a', 'b'))], ['c'])

    def test_bound_reference_pair_is_fixed_but_same_track_other_part_can_move(self):
        w = self.workspace
        w.project.timeline_link_enabled = True
        w._set_timeline_selection({('clip', 'a'), ('clip', 'b'), ('clip', 'd')})
        before = copy.deepcopy(w.project.clips[0])
        call = self.run_alignment(reference_audio_keys={('camera_audio', 'a')})
        self.assertEqual([clip.id for clip in call.args[1]], ['b', 'd'])
        self.assertEqual(call.kwargs['reference_clips'][0]['path'], 'camera-one.mp4')
        self.assertEqual(w.project.clips[0], before)
        self.assertEqual(self._starts(), dict(a=0, b=10, c=6, d=11))

    def test_detached_audio_can_anchor_its_own_picture_and_remains_fixed(self):
        w = self.workspace
        w.project.timeline_link_enabled = False
        reference = CameraClip('a', 1, 'camera-one.mp4', 25, 5, source_in=3)
        w.project.audio_overrides['a'] = copy.deepcopy(reference)
        w._set_timeline_selection({('clip', 'a'), ('clip', 'b'), ('camera_audio', 'a')})
        call = self.run_alignment(reference_audio_keys={('camera_audio', 'a')})
        self.assertEqual([clip.id for clip in call.args[1]], ['a', 'b'])
        self.assertEqual(w.project.audio_overrides['a'], reference)
        self.assertEqual(call.kwargs['reference_clips'][0],
            dict(id='audio:camera:a', camera=1, path=reference.path, start=25, duration=5, source_in=3))

    def test_multiple_audio_references_include_orphan_audio_and_master_fragment(self):
        w = self.workspace
        w.project.audio_overrides['orphan'] = CameraClip('orphan', 4, 'audio.wav', 16, 8, source_in=2)
        w.project.master_audio_clips = [MasterAudioClip('m', 8, 10, 4)]
        w._set_timeline_selection({('clip', 'a'), ('clip', 'b'), ('audio', 'm'), ('camera_audio', 'orphan')})
        call = self.run_alignment(reference_audio_keys={('audio', 'm'), ('camera_audio', 'orphan')})
        refs = call.kwargs['reference_clips']
        self.assertEqual([(r['start'], r['source_in']) for r in refs], [(8, 4), (16, 2)])
        self.assertEqual(w.project.master_audio_clips[0], MasterAudioClip('m', 8, 10, 4))
        self.assertEqual(w.project.audio_overrides['orphan'].start, 16)

    def test_edited_master_uses_current_timeline_anchor(self):
        w = self.workspace
        w.project.master_audio_clips = [MasterAudioClip('m', 20, 10, 4)]
        w._set_timeline_selection({('clip', 'b')})
        call = self.run_alignment(use_master=True)
        self.assertEqual(call.kwargs['reference_clips'], [dict(id='audio:master:m', camera=0,
            path='continuous-master.wav', start=20, duration=10, source_in=4)])

    def test_invalid_reference_or_reference_only_selection_never_starts_worker(self):
        w = self.workspace
        w.project.timeline_link_enabled = True
        w._set_timeline_selection({('clip', 'a')})
        with mock.patch('multicam_ui.align_camera_tracks') as backend, mock.patch('multicam_ui.messagebox.showinfo'):
            w.align(True, reference_audio_keys={('camera_audio', 'missing')})
            w.align(True, reference_audio_keys=set())
            w.align(True, reference_audio_keys={('camera_audio', 'a')})
        backend.assert_not_called()
        self.assertFalse(w.busy)
        self.assertEqual(w.undo_stack, [])
        self.assertEqual(self._starts(), self.original_starts)

    def test_audio_only_selection_does_not_fall_back_to_sync_all(self):
        w = self.workspace
        w._set_timeline_selection({('audio', 'master')})
        with mock.patch.object(w, 'align') as backend, mock.patch('timeline_sync.messagebox.showinfo'):
            w.auto_align()
        backend.assert_not_called()

    def test_cancelled_selected_job_keeps_every_clip_and_audio_unchanged(self):
        w = self.workspace
        w._set_timeline_selection({('clip', 'a'), ('clip', 'b')})
        before = copy.deepcopy(w.project)
        def cancel(*args, **kwargs):
            self.root.cancel_event.set()
            return [AlignmentResult(-12, .9, 50), AlignmentResult(-15, .9, 50)]
        with mock.patch('multicam_ui.align_camera_tracks', side_effect=cancel):
            w.align(True, use_master=True)
            jobs = self._terminal_jobs()
            self.assertEqual(jobs[-1], ('align_cancelled', None))
            self._consume_jobs(jobs)
        self.assertEqual(w.project, before)
        self.assertEqual(w.undo_stack, [])

    def test_selected_master_does_not_request_full_recording_for_partial_reference(self):
        w = self.workspace
        w.project.master_audio = 'camera-one.mp4'
        w.project.master_duration = 12
        w._set_timeline_selection({('clip', 'b')})
        with mock.patch('multicam_ui.messagebox.askyesnocancel') as prompt:
            self.run_alignment(use_master=True)
        prompt.assert_not_called()

    def test_picker_keeps_configured_camera_reference_available(self):
        w = self.workspace
        w.project.alignment_reference_camera = 1
        w._set_timeline_selection({('clip', 'b')})
        with mock.patch.object(w, 'align') as backend:
            w.align_selected()
            next(widget for widget in self.descendants(w.alignment_dialog)
                 if isinstance(widget, ttk.Button) and widget.cget('text') == '开始对齐').invoke()
        backend.assert_called_once_with(True, reference_audio_keys=None, use_master=False)

    def descendants(self, widget):
        return [child for direct in widget.winfo_children() for child in [direct, *self.descendants(direct)]]

    def test_picker_can_choose_master_or_audio_and_cancel_without_editing(self):
        w = self.workspace
        w._set_timeline_selection({('clip', 'a'), ('clip', 'b')})
        for mode, expected in [('master', dict(reference_audio_keys=None, use_master=True)),
                               ('audio', dict(reference_audio_keys={('camera_audio', 'a')}, use_master=False))]:
            with mock.patch.object(w, 'align') as backend:
                w.align_selected(mode)
                self.root.update_idletasks()
                dialog = w.alignment_dialog
                widgets = self.descendants(dialog)
                start = next(widget for widget in widgets if isinstance(widget, ttk.Button) and widget.cget('text') == '开始对齐')
                start.invoke()
                backend.assert_called_once_with(True, **expected)
        w.align_selected()
        next(widget for widget in self.descendants(w.alignment_dialog)
             if isinstance(widget, ttk.Button) and widget.cget('text') == '取消').invoke()
        self.assertEqual(self._starts(), self.original_starts)
        self.assertEqual(w.undo_stack, [])

    def test_ctrl_click_video_selection_reaches_auto_align_picker(self):
        w = self.workspace
        for identifier, state in [('a', 0), ('b', 4)]:
            clip = next(c for c in w.project.clips if c.id == identifier)
            top, bottom = w._timeline_track_bounds[clip.camera]
            event = SimpleNamespace(x=w._timeline_x(clip.start+1)-w.timeline.canvasx(0),
                y=(top+bottom)/2-w.timeline.canvasy(0), state=state)
            w.timeline_press(event)
            w.timeline_release(event)
        self.assertEqual(w._timeline_selection, {('clip', 'a'), ('clip', 'b')})
        with mock.patch.object(w, 'align_selected') as picker:
            w.auto_align()
        picker.assert_called_once_with()


class SelectedSyncMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        media_fixture.ReferenceSyncMediaTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_waveforms_account_for_both_video_and_reference_trims(self):
        project = MulticamProject(clips=[
            CameraClip('target', 1, str(self.paths['targetA']), 0, 10, source_in=3),
            CameraClip('untouched', 1, str(self.paths['targetB']), 30, 15)],
            master_audio=str(self.paths['refA']), master_duration=35,
            master_audio_clips=[MasterAudioClip('trim', 50, 20, 5)])
        before = copy.deepcopy(project)
        clips = selected_video_clips(project, {('clip', 'target')})
        refs = reference_audio_clips(project, {('audio', 'trim')})
        results = align_camera_tracks(self.ffmpeg, clips, reference_clips=refs,
            ffprobe_path=self.ffprobe, cache_dir=self.folder/'selected-cache')
        self.assertEqual(len(results), 1)
        self.assertGreaterEqual(results[0].confidence, .2)
        self.assertAlmostEqual(-results[0].offset_seconds, 58, delta=.06)
        self.assertEqual(project, before)


if __name__ == '__main__':
    unittest.main()
