"""Clip disable must agree across keyboard, preview, render and saved projects."""
import copy
from types import SimpleNamespace
import tkinter as tk
import unittest

from keyboard_profiles import PROFILE_LABELS, shortcut_action, shortcut_label
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_player import MulticamPlayer
from timeline_audio import audio_sources, master_audio_clips
from timeline_enable import toggle_timeline_enabled
from timeline_split import split_timeline
from video_stack_render import plan_stack_segments
from tests import test_program_edit_ui as fixture
from tests import test_multicam as media_fixture


class ClipEnableTests(unittest.TestCase):
    def project(self, linked=True):
        return MulticamProject(
            clips=[CameraClip('a', 1, 'a.mp4', 0., 12., has_audio=True),
                   CameraClip('b', 2, 'b.mp4', 0., 12., has_audio=True)],
            cuts=[Cut(0., 1)], master_audio='master.wav', master_duration=12.,
            timeline_link_enabled=linked, audio_track_enabled={0: True, 1: True, 2: True})

    def test_linked_video_and_sound_disable_and_restore_without_removing_media(self):
        project = self.project()
        before = copy.deepcopy(project)
        edit, count, enabled, keys = toggle_timeline_enabled(project, {('clip', 'a')})
        self.assertEqual(project, before)
        self.assertEqual(count, 2)
        self.assertFalse(enabled)
        self.assertIsNone(edit.media_at(1, 4.))
        self.assertEqual({s.track for s in audio_sources(edit)}, {0, 2})
        self.assertTrue(all(segment.path is None for segment in edit.segments()))
        edit.video_stack_enabled = True
        self.assertEqual([layer.camera for layer in plan_stack_segments(edit)[0].layers], [2])
        self.assertEqual(edit.duration, 12.)
        self.assertEqual(len(edit.clips), 2)
        edit.video_stack_enabled = False
        restored, _, enabled, _ = toggle_timeline_enabled(edit, keys)
        self.assertTrue(enabled)
        self.assertEqual(restored, before)

    def test_unlinked_video_and_audio_can_be_toggled_independently(self):
        project = self.project(linked=False)
        picture, _, _, _ = toggle_timeline_enabled(project, {('clip', 'a')})
        self.assertIsNone(picture.media_at(1, 4.))
        self.assertIn(1, {s.track for s in audio_sources(picture)})
        sound, _, _, _ = toggle_timeline_enabled(project, {('camera_audio', 'a')})
        self.assertIsNotNone(sound.media_at(1, 4.))
        self.assertNotIn(1, {s.track for s in audio_sources(sound)})
        # Exercise the same source resolvers the live player uses.
        player = object.__new__(MulticamPlayer)
        player._project = picture
        self.assertIsNotNone(player._camera_audio_spec(1, 4.))
        player._project = sound
        self.assertIsNone(player._camera_audio_spec(1, 4.))

    def test_master_audio_and_split_disabled_source_keep_state_and_clocks(self):
        edit, _, _, _ = toggle_timeline_enabled(self.project(), {('audio', 'master')})
        self.assertFalse(master_audio_clips(edit)[0].enabled)
        self.assertNotIn(0, {s.track for s in audio_sources(edit)})
        player = object.__new__(MulticamPlayer)
        player._project = edit
        self.assertIsNone(player._audio_spec(3.))
        edit, _, _, _ = toggle_timeline_enabled(edit, {('clip', 'a')})
        split, _, _ = split_timeline(edit, 3., {('clip', 'a')})
        halves = [clip for clip in split.clips if clip.camera == 1]
        self.assertEqual(len(halves), 2)
        self.assertTrue(all(not clip.enabled for clip in halves))
        self.assertEqual(halves[1].source_in, 3.)

    def test_mixed_selection_disables_all_then_enables_all_and_empty_is_noop(self):
        project = self.project()
        project.clips[0].enabled = False
        keys = {('clip', 'a'), ('clip', 'b')}
        edit, _, enabled, _ = toggle_timeline_enabled(project, keys)
        self.assertFalse(enabled)
        self.assertFalse(any(clip.enabled for clip in edit.clips))
        edit, _, enabled, _ = toggle_timeline_enabled(edit, keys)
        self.assertTrue(enabled)
        self.assertTrue(all(clip.enabled for clip in edit.clips))
        for selection in (set(), {('subtitle', 'missing')}, {('clip', 'missing')}):
            unchanged, count, _, _ = toggle_timeline_enabled(project, selection)
            self.assertIs(unchanged, project)
            self.assertEqual(count, 0)

    def test_programme_selection_only_disables_its_range_and_preserves_source_time(self):
        project = self.project()
        project.edit_points = [3., 7.]
        keys = {('program', (3., 7.))}
        edit, _, enabled, selection = toggle_timeline_enabled(project, keys)
        self.assertFalse(enabled)
        self.assertEqual(selection, keys)
        self.assertIsNotNone(edit.media_at(1, 2.))
        self.assertIsNone(edit.media_at(1, 4.))
        self.assertEqual(edit.media_at(1, 8.)[1], 8.)
        self.assertFalse(any(s.track == 1 and s.start <= 4. < s.end for s in audio_sources(edit)))
        restored, _, enabled, _ = toggle_timeline_enabled(edit, keys)
        self.assertTrue(enabled)
        self.assertEqual(restored.media_at(1, 4.)[1], 4.)
        self.assertEqual(restored.edit_points, project.edit_points)

    def test_roundtrip_defaults_version_and_invalid_flags(self):
        project = self.project()
        payload = project.to_payload('.')
        self.assertNotIn('enabled', payload['clips'][0])
        self.assertLess(payload['version'], 17)
        edit, _, _, _ = toggle_timeline_enabled(project, {('clip', 'a'), ('audio', 'master')})
        payload = edit.to_payload('.')
        self.assertEqual(payload['version'], 17)
        restored = MulticamProject.from_payload(payload, '.')
        self.assertEqual(restored.to_payload('.'), payload)
        for bad in (0, 'false', None):
            invalid = copy.deepcopy(payload)
            invalid['clips'][0]['enabled'] = bad
            with self.assertRaises(ValueError): MulticamProject.from_payload(invalid, '.')
            invalid = copy.deepcopy(payload)
            invalid['master_audio_clips'][0]['enabled'] = bad
            with self.assertRaises(ValueError): MulticamProject.from_payload(invalid, '.')

    def test_programme_multi_selection_does_not_split_selected_subtitles(self):
        from subtitle_core import SubtitleCue
        project = self.project()
        project.edit_points = [3., 7.]
        project.subtitles.cues = [SubtitleCue('cue', 1., 9., '字幕')]
        selected = {('program', (3., 7.)), ('subtitle', 'cue')}
        edit, _, _, selection = toggle_timeline_enabled(project, selected)
        self.assertEqual(edit.subtitles, project.subtitles)
        self.assertEqual(selection, selected)

    def test_d_is_available_in_every_preset_and_works_with_ime(self):
        for profile in PROFILE_LABELS:
            event = SimpleNamespace(keysym='Process', char='的', keycode=0x44, state=0)
            self.assertEqual(shortcut_action(profile, event), 'toggle_clip_enabled')
            self.assertEqual(shortcut_label(profile, 'toggle_clip_enabled'), 'D')
            event.state = 4
            self.assertIsNone(shortcut_action(profile, event))


class ClipEnableWorkspaceTests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def press(self, widget=None):
        ws = self.workspace
        return ws._key(SimpleNamespace(keysym='Process', char='的', keycode=0x44,
                                       state=0, widget=widget or ws.timeline))

    def release(self):
        self.workspace._transport_key_release(SimpleNamespace(keysym='d', keycode=0x44))

    def test_native_keypress_toggles_grey_selection_and_repeat_is_suppressed(self):
        ws = self.workspace
        ws._set_timeline_selection({('clip', 'a')})
        ws.timeline.focus_force()
        ws.timeline.event_generate('<KeyPress-d>')
        self.assertFalse(ws.project.clips[0].enabled)
        self.assertEqual(ws._timeline_selection, {('clip', 'a')})
        rectangles = [item for item, identifier in ws.timeline_clip_items.items()
                      if identifier == 'a' and ws.timeline.type(item) == 'rectangle']
        self.assertEqual(ws.timeline.itemcget(rectangles[0], 'fill'), '#383a3e')
        for _ in range(3): self.press()
        self.assertFalse(ws.project.clips[0].enabled)
        self.assertEqual(len(ws.undo_stack), 1)
        ws.timeline.event_generate('<KeyRelease-d>')
        ws.timeline.event_generate('<KeyPress-d>')
        self.assertTrue(ws.project.clips[0].enabled)
        ws.timeline.event_generate('<KeyRelease-d>')
        ws.undo()
        self.assertFalse(ws.project.clips[0].enabled)
        ws.redo()
        self.assertTrue(ws.project.clips[0].enabled)
        self.assertEqual(self.callback_errors, [])

    def test_entry_empty_selection_busy_and_focus_reset(self):
        ws = self.workspace
        ws._set_timeline_selection({('clip', 'a')})
        entry = tk.Entry(ws)
        self.assertIsNone(self.press(entry))
        self.assertTrue(ws.project.clips[0].enabled)
        self.press()
        self.assertFalse(ws.project.clips[0].enabled)
        ws._transport_focus_out()
        self.press()
        self.assertTrue(ws.project.clips[0].enabled)
        self.release()
        ws._set_timeline_selection(set())
        before = len(ws.undo_stack)
        self.press(); self.release()
        self.assertEqual(len(ws.undo_stack), before)
        ws._set_timeline_selection({('clip', 'a')})
        ws.busy = True
        self.press(); self.release()
        self.assertTrue(ws.project.clips[0].enabled)
        ws.busy = False
        entry.destroy()

    def test_whole_session_save_restores_disabled_audio_and_picture(self):
        from project_file import build_project_payload, write_project_file, read_project_file
        from studio_state import restore_okk
        ws = self.workspace
        self.root.multicam_workspace = ws
        ws._set_timeline_selection({('clip', 'a')})
        self.press(); self.release()
        path = self.folder/'禁用素材.rtxproj'
        write_project_file(path, build_project_payload(self.root, app_version='1.0.4'))
        ws.project = MulticamProject()
        restore_okk(self.root, read_project_file(path)['state']['okk_multicam'])
        self.assertFalse(ws.project.clips[0].enabled)
        self.assertIsNone(ws.project.media_at(1, 2.))
        self.assertFalse(audio_sources(ws.project))


class ClipEnableRenderTests(unittest.TestCase):
    setUpClass = classmethod(media_fixture.FFmpegMulticamTests.setUpClass.__func__)
    tearDownClass = classmethod(media_fixture.FFmpegMulticamTests.tearDownClass.__func__)
    run_command = staticmethod(media_fixture.FFmpegMulticamTests.run_command)
    process = classmethod(media_fixture.FFmpegMulticamTests.process.__func__)
    project = media_fixture.FFmpegMulticamTests.project
    render = media_fixture.FFmpegMulticamTests.render
    assert_color = media_fixture.FFmpegMulticamTests.assert_color

    def test_actual_export_silences_disabled_linked_sound_and_restores_on_second_toggle(self):
        from array import array
        project = self.project()
        project.master_audio = ''
        project.audio_track_enabled = {1: True, 2: False}
        project.clips[0].has_audio = True
        project.timeline_link_enabled = True
        disabled, _, _, keys = toggle_timeline_enabled(project, {('clip', 'red')})
        for stack in (False, True):
            disabled.video_stack_enabled = stack
            disabled.video_stack_order = [1, 2]
            output = self.render('disabled-stack.mp4' if stack else 'disabled.mp4', disabled)
            self.assert_color(output, .4, (0, 0, 0))
            self.assert_color(output, 1.4, (0, 0, 255))
            raw = self.run_command([self.ffmpeg, '-v', 'error', '-i', str(output),
                                    '-map', '0:a:0', '-ac', '1', '-f', 's16le', '-'])
            samples = array('h'); samples.frombytes(raw)
            self.assertTrue(samples)
            self.assertLessEqual(max(abs(value) for value in samples), 2)
        restored, _, _, _ = toggle_timeline_enabled(disabled, keys)
        restored.video_stack_enabled = False
        output = self.render('reenabled.mp4', restored)
        self.assert_color(output, .4, (255, 0, 0))
        raw = self.run_command([self.ffmpeg, '-v', 'error', '-i', str(output),
                               '-map', '0:a:0', '-ac', '1', '-f', 's16le', '-'])
        samples = array('h'); samples.frombytes(raw)
        self.assertGreater(max(abs(value) for value in samples[:24000]), 1000)


if __name__ == '__main__': unittest.main()
