"""Native gestures, project roundtrips, and playback share independent audio."""
from copy import deepcopy
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from exporter_core import ExportSettings, Capabilities
from multicam_player import MulticamPlayer
from timeline_audio import audio_sources, source_audio_clips
from timeline_edits import delete_source_clips, move_source_clips, finalize_source_edit
from timeline_link import move_source_audio
from tests import test_timeline_drag_merge as timeline_fixture
from tests import test_timeline_audio_model as sample_fixture


class TimelineLinkModelTests(unittest.TestCase):
    def project(self):
        return MulticamProject(clips=[CameraClip('one', 1, str(Path('one.mp4').resolve()), 0., 10., has_audio=True)],
                               cuts=[Cut(0., 1)])

    def test_picture_only_move_keeps_original_audio_and_new_imports_follow(self):
        project = self.project()
        project.timeline_link_enabled = False
        move_source_clips(project, {'one'}, 2.)
        finalize_source_edit(project)
        self.assertEqual(project.clips[0].start, 2.)
        self.assertEqual(audio_sources(project)[0].start, 0.)
        project.clips.append(CameraClip('two', 2, 'two.mp4', 3., 6.))
        self.assertEqual([c.start for c in source_audio_clips(project)], [0., 3.])

    def test_relink_preserves_offset_and_moves_pair_once(self):
        project = self.project()
        project.timeline_link_enabled = False
        move_source_audio(project, {'one'}, 1.)
        project.timeline_link_enabled = True
        move_source_clips(project, {'one'}, 2.)
        self.assertEqual(project.clips[0].start, 2.)
        self.assertEqual(audio_sources(project)[0].start, 3.)
        self.assertEqual(project.duration, 13.)

    def test_picture_delete_keeps_audio_playable_and_exportable(self):
        project = self.project()
        project.timeline_link_enabled = False
        delete_source_clips(project, {'one'})
        self.assertEqual(project.clips, [])
        self.assertEqual(project.timeline_cameras, [1])
        self.assertEqual(project.duration, 10.)
        self.assertEqual(audio_sources(project)[0].path, str(Path('one.mp4').resolve()))
        player = object.__new__(MulticamPlayer)
        player._project = project
        self.assertEqual(player._camera_audio_spec(1, 2.)[2], 2.)

    def test_linked_delete_removes_detached_audio_too(self):
        project = self.project()
        move_source_audio(project, {'one'}, 2.)
        delete_source_clips(project, {'one'})
        self.assertEqual(source_audio_clips(project), [])
        self.assertEqual(project.duration, 0.)

    def test_save_load_preserves_switches_paths_audio_and_tombstones(self):
        project = self.project()
        project.clips.append(CameraClip('two', 2, str(Path('two.mp4').resolve()), 0., 10.))
        project.timeline_link_enabled = project.timeline_snap_enabled = False
        move_source_audio(project, {'one'}, 1.)
        project.audio_overrides['two'] = None
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'edit.multicam.json'
            project.save(path)
            loaded = MulticamProject.load(path)
        self.assertEqual(loaded, project)
        self.assertEqual(loaded.to_payload('.')['version'], 12)

    def test_legacy_projects_default_to_enabled_without_independent_audio(self):
        payload = self.project().to_payload('.')
        self.assertNotIn('audio_overrides', payload)
        loaded = MulticamProject.from_payload(payload, '.')
        self.assertTrue(loaded.timeline_link_enabled)
        self.assertTrue(loaded.timeline_snap_enabled)

    def test_invalid_overrides_and_switches_are_rejected(self):
        payload = self.project().to_payload('.')
        for fields in ({'timeline_snap_enabled': 1}, {'timeline_link_enabled': 'false'},
                       {'audio_overrides': []}, {'audio_overrides': {'wrong': payload['clips'][0]}},
                       {'audio_overrides': {'': None}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                MulticamProject.from_payload(dict(payload, **fields), '.')


class TimelineControlsUITests(unittest.TestCase):
    setUp = timeline_fixture.TimelineDropTests.setUp
    tearDown = timeline_fixture.TimelineDropTests.tearDown
    timeline_event = timeline_fixture.TimelineDropTests.timeline_event

    def event_at(self, value, y):
        w = self.workspace
        return SimpleNamespace(x=w._timeline_x(value)-w.timeline.canvasx(0), y=y-w.timeline.canvasy(0))

    def audio_event(self, identifier):
        w = self.workspace
        item = next(item for item, value in w.timeline_camera_audio_items.items()
                    if value == identifier and w.timeline.type(item) == 'rectangle')
        x1, y1, x2, y2 = w.timeline.coords(item)
        return SimpleNamespace(x=(x1+x2)/2-w.timeline.canvasx(0), y=(y1+y2)/2-w.timeline.canvasy(0))

    def test_scrubbing_snaps_program_cuts_and_source_edges_only_when_enabled(self):
        w = self.workspace
        w.project.cuts.append(Cut(4., 2))
        w.project.clips[1].start = 2.
        w._refresh()
        for boundary in (2., 4.):
            event = self.event_at(boundary, 8)
            event.x += 3
            w.timeline_press(event)
            self.assertAlmostEqual(w.current, boundary)
            w.timeline_release(event)
        w.timeline_snap_button.invoke()
        self.assertFalse(w.project.timeline_snap_enabled)
        event = self.event_at(4., 8)
        event.x += 7
        w.timeline_press(event); w.timeline_release(event)
        self.assertNotEqual(w.current, 4.)
        self.assertEqual(self.errors, [])


    def test_snapping_after_horizontal_scroll_and_zoom(self):
        w = self.workspace
        w.project.cuts.append(Cut(4., 2))
        w.change_zoom(8)
        w.timeline.xview_moveto(.2)
        w.draw_timeline()
        event = self.event_at(4., 8)
        event.x += 4
        self.assertEqual(w._timeline_pointer_time(event.x), 4.)

    def test_linked_audio_click_moves_both_with_one_undo(self):
        w = self.workspace
        press = self.audio_event('one')
        w.timeline_press(press)
        w.timeline_motion(SimpleNamespace(x=press.x+w._timeline_scale(), y=press.y))
        w.timeline_release(press)
        self.assertEqual(w.project.clips[0].start, 1.)
        self.assertEqual(w.project.clips[0].camera, 1)
        self.assertEqual(source_audio_clips(w.project)[0].start, 1.)
        self.assertEqual(len(w.undo_stack), 1)
        w.undo()
        self.assertEqual(source_audio_clips(w.project)[0].start, 0.)

    def test_unlinked_audio_drag_delete_undo_redo_does_not_move_picture(self):
        w = self.workspace
        w.timeline_link_button.invoke()
        press = self.audio_event('one')
        w.timeline_press(press)
        w.timeline_motion(SimpleNamespace(x=press.x+w._timeline_scale(), y=press.y))
        w.timeline_release(press)
        self.assertEqual(w.project.clips[0].start, 0.)
        self.assertEqual(source_audio_clips(w.project)[0].start, 1.)
        w._delete_timeline_selection()
        self.assertEqual([c.id for c in source_audio_clips(w.project)], ['two'])
        self.assertEqual(len(w.project.clips), 2)
        w.undo()
        self.assertEqual(source_audio_clips(w.project)[0].start, 1.)
        w.redo()
        self.assertEqual([c.id for c in source_audio_clips(w.project)], ['two'])
        self.assertEqual(self.errors, [])

    def test_enabling_link_normalizes_selection_for_ctrl_toggle(self):
        w = self.workspace
        w.timeline_link_button.invoke()
        w._set_timeline_selection({('camera_audio', 'one')})
        w.timeline_link_button.invoke()
        self.assertEqual(w._timeline_selection, {('clip', 'one')})
        press = self.audio_event('one')
        press.state = 4
        w.timeline_press(press); w.timeline_release(press)
        self.assertEqual(w._timeline_selection, set())

    def test_unlinked_video_move_cancel_and_vertical_move_preserve_audio(self):
        w = self.workspace
        w.timeline_link_button.invoke()
        baseline = deepcopy(w.project)
        press = self.timeline_event('one')
        w.timeline_press(press)
        y = sum(w._timeline_track_bounds[2])/2-w.timeline.canvasy(0)
        w.timeline_motion(SimpleNamespace(x=press.x, y=y))
        self.assertEqual(w.project.clips[0].camera, 2)
        self.assertEqual(source_audio_clips(w.project)[0].camera, 1)
        w._finish_selection_gesture(cancel=True)
        self.assertEqual(w.project, baseline)

    def test_proxy_badges_refresh_for_partial_missing_and_preview_disabled(self):
        w = self.workspace
        with tempfile.TemporaryDirectory() as folder:
            proxy = Path(folder)/'proxy.mp4'
            proxy.write_bytes(b'fixture')
            w.project.clips[0].proxy_path = str(proxy)
            w.proxy_var.set(False)
            w._refresh_proxy_badges(force=True)
            self.assertEqual(w.camera_proxy_badges[1].cget('text'), '已代理')
            self.assertEqual(w.camera_proxy_badges[2].cget('text'), '未代理')
            w.project.clips.append(CameraClip('three', 1, 'three.mp4', 10., 4.))
            w._refresh_proxy_badges(force=True)
            self.assertEqual(w.camera_proxy_badges[1].cget('text'), '部分代理 1/2')
            proxy.unlink()
            w._refresh_proxy_badges(force=True)
            self.assertEqual(w.camera_proxy_badges[1].cget('text'), '代理丢失')
        self.assertEqual(self.errors, [])


class IndependentAudioExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sample_fixture.TimelineAudioSampleTests.setUpClass.__func__(cls)

    setUp = sample_fixture.TimelineAudioSampleTests.setUp
    tearDown = sample_fixture.TimelineAudioSampleTests.tearDown
    tone = sample_fixture.TimelineAudioSampleTests.tone
    samples = sample_fixture.TimelineAudioSampleTests.samples
    amplitude = sample_fixture.TimelineAudioSampleTests.amplitude

    def test_final_video_export_uses_independent_audio_timing(self):
        video = self.folder/'camera.mp4'
        subprocess.run([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=blue:s=96x54:r=30:d=2', '-i', str(self.camera),
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                        '-shortest', str(video)], check=True, capture_output=True)
        project = MulticamProject(clips=[CameraClip('one', 1, str(video), 0., 2., has_audio=True)],
                                  cuts=[Cut(0., 1)], timeline_link_enabled=False)
        move_source_audio(project, {'one'}, .5)
        def runner(command, callback):
            result = subprocess.run(command, capture_output=True, timeout=30)
            return result.returncode, result.stderr.decode('utf-8', 'replace')
        output = self.folder/'result.mov'
        settings = ExportSettings(codec='libx264', preset='ultrafast', decode_mode='cpu',
                                  audio='pcm_s16le', bit_depth='8-bit')
        with patch('multicam_core.cache_dir', return_value=self.folder):
            render_multicam(project, output, self.ffmpeg, self.ffprobe, settings,
                            Capabilities(ffmpeg_path=self.ffmpeg, ffprobe_path=self.ffprobe), runner)
        pcm = self.folder/'exported.wav'
        subprocess.run([self.ffmpeg, '-v', 'error', '-y', '-i', str(output), '-vn',
                        '-c:a', 'pcm_s16le', str(pcm)], check=True, capture_output=True)
        samples = self.samples(pcm)
        self.assertEqual(len(samples), 120000)
        self.assertEqual(max(map(abs, samples[:20000])), 0)
        self.assertGreater(self.amplitude(samples, 880, .7, .9), 2500)

