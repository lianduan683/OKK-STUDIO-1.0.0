"""Source splits must retain pixels, sound, selected scope and saved clocks."""
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from multicam_core import CameraClip, Cut, MulticamProject
from timeline_audio import audio_sources, master_audio_clips
from timeline_split import split_timeline
from keyboard_profiles import shortcut_action
from tests import test_program_edit_ui as fixture


class SourceSplitTests(unittest.TestCase):
    def project(self, linked=False):
        return MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', -2., 12., has_audio=True),
                                     CameraClip('b', 2, 'b.mp4', 0., 10., has_audio=True)],
            cuts=[Cut(0., 1)], master_audio='master.wav', master_duration=10.,
            timeline_link_enabled=linked, audio_track_enabled={0: True, 1: True, 2: True})

    def test_no_selection_splits_all_tracks_at_one_frame_and_keeps_source_clock(self):
        project = self.project()
        original = copy.deepcopy(project)
        edit, count, selection = split_timeline(project, 3.)
        self.assertEqual(project, original)
        self.assertEqual(count, 6)  # Programme, 2 videos, 2 source audio, master.
        self.assertFalse(selection)
        self.assertEqual(edit.edit_points, [3.])
        self.assertEqual(len(edit.clips), 4)
        self.assertEqual([(c.start, c.duration, c.source_in) for c in edit.clips],
                         [(-2., 5., 0.), (3., 7., 5.), (0., 3., 0.), (3., 7., 3.)])
        self.assertEqual(edit.media_at(1, 4.)[1], project.media_at(1, 4.)[1])
        self.assertEqual([(c.start, c.duration, c.source_in) for c in master_audio_clips(edit)],
                         [(0., 3., 0.), (3., 7., 3.)])
        for time in (0., 2.9, 3., 4., 9.9):
            for track in (0, 1, 2):
                a = next(s for s in audio_sources(project) if s.track == track and s.start <= time < s.end)
                b = next(s for s in audio_sources(edit) if s.track == track and s.start <= time < s.end)
                self.assertAlmostEqual(a.source_start+time-a.start, b.source_start+time-b.start)
        restored = MulticamProject.from_payload(edit.to_payload('.'), '.')
        self.assertEqual(restored.to_payload('.'), edit.to_payload('.'))
        self.assertEqual(edit.to_payload('.')['version'], 15)

    def test_only_selected_video_is_split_and_unlinked_sound_is_unchanged(self):
        project = self.project()
        edit, count, keys = split_timeline(project, 3., {('clip', 'a')})
        self.assertEqual(count, 1)
        self.assertEqual(len(edit.clips), 3)
        self.assertEqual(edit.clips[-1], project.clips[-1])
        self.assertEqual(len([s for s in audio_sources(edit) if s.track == 1]), 1)
        self.assertEqual(edit.edit_points, [])
        self.assertEqual(len(keys), 2)
        self.assertEqual(edit.media_at(1, 4.)[1], 6.)

    def test_selected_audio_only_does_not_cut_picture_or_other_tracks(self):
        project = self.project()
        edit, count, keys = split_timeline(project, 3., {('camera_audio', 'a')})
        self.assertEqual(count, 1)
        self.assertEqual(edit.clips, project.clips)
        self.assertEqual(len([s for s in audio_sources(edit) if s.track == 1]), 2)
        self.assertEqual(edit.edit_points, [])
        self.assertEqual(len(keys), 2)

    def test_linked_selection_cuts_both_sides_and_repeated_split_is_noop(self):
        project = self.project(linked=True)
        edit, count, keys = split_timeline(project, 3., {('clip', 'a')})
        self.assertEqual(count, 2)
        self.assertFalse(edit.audio_overrides)
        repeated, count, _ = split_timeline(edit, 3., keys)
        self.assertEqual(count, 0)
        self.assertEqual(edit, repeated)

    def test_selected_master_segment_never_splits_an_unselected_segment(self):
        edit, _, _ = split_timeline(self.project(), 3., {('audio', 'master')})
        self.assertEqual(len(edit.master_audio_clips), 2)
        again, count, _ = split_timeline(edit, 5., {('audio', 'master')})
        self.assertEqual(count, 0)
        self.assertEqual(again, edit)

    def test_gaps_and_boundaries_skip_only_the_tracks_that_cannot_split(self):
        edit, count, _ = split_timeline(self.project(), 0.)
        self.assertEqual(count, 2)  # Negative-start source picture and audio.
        self.assertEqual(len(edit.clips), 3)
        self.assertEqual(len(edit.master_audio_clips), 0)
        again, count, _ = split_timeline(edit, 10.)
        self.assertEqual(count, 0)
        self.assertEqual(again, edit)

    def test_invalid_source_in_is_rejected_and_old_payload_stays_compatible(self):
        project = self.project()
        self.assertNotIn('source_in', project.to_payload('.')['clips'][0])
        for value in (-1., float('nan'), True):
            project.clips[0].source_in = value
            with self.assertRaises(ValueError): project.validate()

    def test_sync_after_split_keeps_source_in_on_external_and_camera_reference_clocks(self):
        from editor_core import AlignmentResult
        from reference_sync import align_camera_tracks, align_to_camera
        project, _, _ = split_timeline(self.project(), 3., {('clip', 'b')})
        clips = [clip for clip in project.clips if clip.camera == 2]
        results = [AlignmentResult(-1., .99, 50, 'fixture') for _ in clips]
        with patch('reference_sync.align_audio_segments', return_value=results):
            aligned = align_camera_tracks('ffmpeg', clips, 'master.wav', fps=30.)
        self.assertEqual([-result.offset_seconds for result in aligned], [1., 4.])
        reference = CameraClip('ref', 3, 'ref.mov', 10., 10., source_in=3.)
        target = CameraClip('target', 4, 'target.mov', 0., 10., source_in=2.)
        with patch('reference_sync._has_audio', return_value=True), patch(
                'reference_sync.align_audio_segments', return_value=[AlignmentResult(2., .99, 50, 'fixture')]):
            aligned = align_to_camera('ffmpeg', [target], [reference], ffprobe_path='ffprobe')
        self.assertEqual(-aligned[0].offset_seconds, 7.)


class TimelineSplitWorkspaceTests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def event_for(self, mapping, identifier):
        ws = self.workspace
        item = next(item for item, value in mapping.items() if value == identifier
                    and ws.timeline.type(item) == 'rectangle')
        left, top, right, bottom = ws.timeline.coords(item)
        return SimpleNamespace(x=(left+right)/2-ws.timeline.canvasx(0),
                               y=(top+bottom)/2-ws.timeline.canvasy(0), state=0)

    def test_clicking_and_dragging_source_never_moves_playhead_but_ruler_does(self):
        ws = self.workspace
        ws.seek(2.)
        event = self.event_for(ws.timeline_clip_items, 'a')
        ws.timeline_press(event)
        self.assertEqual(ws.current, 2.)
        ws.timeline_motion(SimpleNamespace(x=event.x+ws._timeline_scale(), y=event.y))
        ws.timeline_release(event)
        self.assertEqual(ws.current, 2.)
        event = SimpleNamespace(x=ws._timeline_x(5.)-ws.timeline.canvasx(0), y=ws._px(16), state=0)
        ws.timeline_press(event); ws.timeline_release(event)
        self.assertEqual(ws.current, 5.)

    def test_program_click_keeps_playhead_and_ctrl_b_targets_selection(self):
        ws = self.workspace
        ws.seek(4.)
        event = self.event_for(ws.timeline_program_items, (0., 12.))
        ws.timeline_press(event); ws.timeline_release(event)
        self.assertEqual(ws.current, 4.)
        ws._set_timeline_selection({('clip', 'a')})
        ws.project.timeline_link_enabled = False
        before = copy.deepcopy(ws.project)
        ws._key(SimpleNamespace(keysym='b', state=4, widget=ws.timeline))
        self.assertEqual(len(ws.project.clips), 3)
        self.assertEqual(ws.project.edit_points, [])
        self.assertEqual(ws.current, 4.)
        ws.undo(); self.assertEqual(ws.project, before)
        ws.redo(); self.assertEqual(len(ws.project.clips), 3)

    def test_shift_nine_opens_settings_for_every_preset_without_selecting_camera(self):
        ws = self.workspace
        ws.show_project_settings = Mock()
        ws.switch = Mock()
        for profile in ('current', 'resolve', 'premiere'):
            self.root.keyboard_profile = profile
            for event in (SimpleNamespace(keysym='(', keycode=57, state=1, widget=ws.timeline),
                          SimpleNamespace(keysym='(', state=1, widget=ws.timeline)):
                self.assertEqual(shortcut_action(profile, event), 'project_settings')
                self.assertEqual(ws._key(event), 'break')
        self.assertEqual(ws.show_project_settings.call_count, 6)
        ws.switch.assert_not_called()

    def test_master_segments_draw_select_delete_and_undo_individually(self):
        ws = self.workspace
        ws.project.master_audio = str(self.folder/'master.wav')
        ws.project.master_duration = 12.
        ws.seek(4.)
        ws._set_timeline_selection({('audio', 'master')})
        self.assertTrue(ws.split_timeline_at_playhead())
        self.assertEqual(len(ws.project.master_audio_clips), 2)
        right = ws.project.master_audio_clips[1].id
        self.assertIn(right, ws.timeline_master_audio_items.values())
        ws._set_timeline_selection({('audio', right)})
        ws._delete_timeline_selection()
        self.assertEqual(len(ws.project.master_audio_clips), 1)
        self.assertEqual(ws.project.master_audio_clips[0].end, 4.)
        ws.undo()
        self.assertEqual(len(ws.project.master_audio_clips), 2)


class SplitMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import test_video_stack_render as media
        media.StackMediaTests.setUpClass()
        cls.media_class = media.StackMediaTests
        cls.media = media.StackMediaTests()

    @classmethod
    def tearDownClass(cls): cls.media_class.tearDownClass()

    def test_split_retains_real_export_pixels_and_pcm_audio_in_both_modes(self):
        import subprocess
        from exporter_core import CREATE_NO_WINDOW
        fixture = self.media
        for stack in (False, True):
            project = fixture.project()
            project.video_stack_enabled = stack
            project.clips[0].path = str(fixture.dynamic25)
            project.clips[0].start = -.017
            project.fps = 60.
            project.cuts[0].camera = 1
            project.range_end = 1.5
            before_path, before = fixture.render(project, f'split-before-{stack}', audio='pcm_s16le')
            edit, count, _ = split_timeline(project, .7)
            self.assertGreater(count, 0)
            after_path, after = fixture.render(edit, f'split-after-{stack}', audio='pcm_s16le')
            self.assertEqual(before, after)
            audio = []
            for path in (before_path, after_path):
                result = subprocess.run([fixture.ffmpeg, '-v', 'error', '-i', str(path),
                    '-map', '0:a:0', '-f', 's16le', 'pipe:1'], capture_output=True, timeout=30,
                    creationflags=CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr)
                audio.append(result.stdout)
            self.assertEqual(audio[0], audio[1])


if __name__ == '__main__': unittest.main()
