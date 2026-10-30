"""Opt-in rendering: real cache files, invalidation, UI and playback routing."""
from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from creative_core import ColorGrade
from exporter_core import CREATE_NO_WINDOW, Capabilities, MediaInfo, probe_media, ExportCancelled
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_core import render_multicam
from multicam_player import MulticamPlayer
from prerender import (PreviewBlock, PrerenderWorker, cached_spec, check_folder, needs_render,
                       picture_signature, plan_blocks, render_block, validate_resolution)
from video_stack_preview import cached_stack_segments, preview_command, preview_segments
from tests import test_program_edit_ui as fixture
from tests import test_gpu_live_player as gpu_fixture


class PrerenderCoreTests(unittest.TestCase):
    def project(self):
        return MulticamProject(clips=[CameraClip('a', 1, 'original.mp4', 0, 9.1)], cuts=[Cut(0, 1)], fps=30000/1001)

    def test_frame_aligned_coverage_and_resolution_keys(self):
        project = self.project()
        blocks = plan_blocks(project)
        self.assertEqual(blocks[0].first, 0)
        self.assertTrue(all(left.last == right.first for left, right in zip(blocks, blocks[1:])))
        self.assertEqual(blocks[-1].last, round(project.duration*project.fps))
        self.assertNotEqual(picture_signature(project), picture_signature(project, (1920, 1080)))
        project.video_stack_order = [1]
        self.assertNotEqual(picture_signature(project), picture_signature(self.project()))
        for size in ((17, 720), (0, 720), (True, 720), (1920, 10000)):
            with self.subTest(size=size), self.assertRaises(ValueError): validate_resolution(*size)

    def test_signature_tracks_media_and_picture_changes_but_ignores_audio_view_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'media.mp4'; path.write_bytes(b'a')
            project = self.project(); project.clips[0].path = str(path)
            original = picture_signature(project)
            project.audio_track_enabled = {0: False}; project.monitor_layout = dict(rows=1, columns=1)
            project.range_start = 1; project.timeline_markers = [2]
            self.assertEqual(picture_signature(project), original)
            path.write_bytes(b'changed')
            self.assertNotEqual(picture_signature(project), original)
            original = picture_signature(project)
            project.global_grade = ColorGrade(exposure=1)
            self.assertNotEqual(picture_signature(project), original)

    def test_cost_analysis_and_half_open_cache_lookup(self):
        project = self.project(); block = plan_blocks(project)[0]
        media = {project.clips[0].path: MediaInfo('', width=1920, height=1080, fps=30)}
        self.assertEqual(needs_render(project, block, media), '')
        media[project.clips[0].path].bit_depth = 10
        self.assertTrue(needs_render(project, block, media))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'cache.mp4'; path.touch()
            items = [(str(path), 0., 4.)]
            self.assertEqual(cached_spec(items, 2.)[2], 2.)
            self.assertIsNone(cached_spec(items, 4.))
            player = object.__new__(MulticamPlayer)
            player._prerender_items = items; player._grade_draft = None
            player._audition = None; player._comparison_enabled = False
            self.assertEqual(player._prerender_spec(1.)[0][0], 'prerender')
            player._grade_draft = ('global', ColorGrade())
            self.assertIsNone(player._prerender_spec(1.))
            self.assertTrue(player._is_baked_key(('prerender',)))


class PrerenderUITests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def test_automatic_idle_render_to_green_and_publish_for_playback(self):
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not ffmpeg or not ffprobe: self.skipTest('FFmpeg unavailable')
        source = self.folder/'actual.mp4'
        result = subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=red:s=160x90:r=10:d=1', '-c:v', 'libx264', str(source)],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.workspace
        self.root.caps = Capabilities(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)
        workspace.project = MulticamProject(clips=[CameraClip('real', 1, str(source), 0, 1)],
            cuts=[Cut(0, 1)], fps=10., global_grade=ColorGrade(exposure=.5))
        workspace._apply_prerender_settings(True, str(self.folder/'actual-cache'), (320, 180))
        workspace._refresh()
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            self.root.update()
            if workspace._prerender_blocks and all(block.state == 'green' for block in workspace._prerender_blocks): break
            time.sleep(.02)
        self.assertTrue(all(block.state == 'green' for block in workspace._prerender_blocks), workspace.prerender_status_var.get())
        self.assertTrue(workspace.timeline.find_withtag('prerender-green'))
        player = Mock()
        with patch.object(workspace, 'player', player):
            workspace._publish_prerender()
        action, = player.submit.call_args.args
        self.assertEqual(action, 'prerender_cache')
        items = player.submit.call_args.kwargs['items']
        self.assertTrue(Path(items[0][0]).is_relative_to(self.folder/'actual-cache'))
        self.assertEqual((probe_media(ffprobe, items[0][0]).width, probe_media(ffprobe, items[0][0]).height), (320, 180))
        self.assertEqual(workspace.project.clips[0].path, str(source))
        self.assertEqual(self.callback_errors, [])

    def test_settings_opt_in_directory_custom_size_and_cancel(self):
        workspace = self.workspace
        self.assertFalse(workspace.prerender_enabled_var.get())
        workspace.show_project_settings(); workspace._project_settings_select('prerender')
        values = workspace._project_settings_vars
        values['prerender_enabled'].set(True); values['prerender_folder'].set(str(self.folder/'chosen'))
        values['prerender_preset'].set('自定义')
        values['prerender_width'].set('854'); values['prerender_height'].set('480')
        workspace._project_settings_window.destroy()
        self.assertFalse(workspace.prerender_enabled_var.get())
        workspace._prerender_preferences = self.folder/'prerender-settings.json'
        workspace.show_project_settings(); workspace._project_settings_select('prerender')
        values = workspace._project_settings_vars
        values['prerender_enabled'].set(True); values['prerender_folder'].set(str(self.folder/'chosen'))
        values['prerender_preset'].set('自定义')
        values['prerender_width'].set('854'); values['prerender_height'].set('480')
        workspace._project_settings_apply()
        self.assertTrue(workspace.prerender_enabled_var.get())
        self.assertEqual(workspace._prerender_resolution, (854, 480))
        saved = json.loads(workspace._prerender_preferences.read_text(encoding='utf-8'))
        self.assertEqual((saved['width'], saved['height']), (854, 480))
        self.assertTrue((self.folder/'chosen').is_dir())
        self.assertEqual(self.callback_errors, [])

    def test_colors_pinned_ruler_disable_and_edit_invalidation(self):
        workspace = self.workspace
        workspace._apply_prerender_settings(True, str(self.folder))
        workspace._check_prerender_project()
        blocks = plan_blocks(workspace.project)
        workspace._prerender_blocks = [replace(block, state=state) for block, state in zip(blocks, ('yellow', 'red', 'green'))]
        workspace.draw_timeline()
        for state in ('yellow', 'red', 'green'):
            self.assertTrue(workspace.timeline.find_withtag('prerender-'+state))
        workspace.timeline.yview_moveto(.4); workspace.draw_timeline()
        y = workspace.timeline.coords(workspace.timeline.find_withtag('prerender-line')[0])[1]
        self.assertAlmostEqual(y, workspace.timeline.canvasy(0)+workspace._timeline_ruler_h-workspace._px(3))
        old = workspace._prerender_generation
        workspace.project.global_grade = ColorGrade(exposure=1)
        workspace._refresh()
        self.assertGreater(workspace._prerender_generation, old)
        self.assertFalse(any(block.state == 'green' for block in workspace._prerender_blocks))
        workspace._apply_prerender_settings(False, str(self.folder))
        self.assertFalse(workspace.timeline.find_withtag('prerender-line'))

    def test_late_generation_cannot_publish_and_24fps_is_not_a_stall(self):
        workspace = self.workspace
        workspace._apply_prerender_settings(True, str(self.folder))
        generation = workspace._prerender_generation
        block = replace(plan_blocks(workspace.project)[0], state='green', path='obsolete.mp4')
        workspace._prerender_events.put((generation-1, 'ready', dict(block=block)))
        workspace.playing = True
        workspace._prerender_source_rates = {workspace.project.clips[0].path: 24.}
        workspace.current = 1.
        with patch('prerender_ui.time.monotonic', side_effect=[1., 3.]):
            workspace._observe_prerender_fps(24.)
            workspace._observe_prerender_fps(24.)
        self.assertFalse(workspace._prerender_slow)
        workspace._poll_prerender()
        self.assertFalse(any(block.state == 'green' for block in workspace._prerender_blocks))


class PrerenderMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which('ffmpeg')
        cls.ffprobe = shutil.which('ffprobe')
        if not cls.ffmpeg or not cls.ffprobe: raise unittest.SkipTest('FFmpeg unavailable')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='prerender_test_')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder/'source.mp4'
        result = subprocess.run([self.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=s=160x90:r=10:d=2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.source)],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.project = MulticamProject(clips=[CameraClip('a', 1, str(self.source), 0, 2)],
            cuts=[Cut(0, 1)], fps=10., global_grade=ColorGrade(exposure=.5))
        self.caps = Capabilities(ffmpeg_path=self.ffmpeg, ffprobe_path=self.ffprobe)

    def test_real_render_chosen_resolution_reuse_cancel_and_corruption_repair(self):
        signature = picture_signature(self.project, (320, 180))
        block = PreviewBlock(5, 15, 10., 'red')
        path = render_block(self.project, block, self.folder/'chosen', signature, self.caps,
                            threading.Event(), resolution=(320, 180))
        info = probe_media(self.ffprobe, path)
        self.assertEqual((info.width, info.height), (320, 180))
        self.assertAlmostEqual(info.duration, 1., places=2)
        self.assertEqual(info.fps, 10.)
        self.assertFalse(info.has_audio)
        stamp = Path(path).stat().st_mtime_ns
        reused = render_block(self.project, block, self.folder/'chosen', signature, self.caps,
                            threading.Event(), resolution=(320, 180))
        self.assertEqual(reused, path)
        self.assertEqual(Path(path).stat().st_mtime_ns, stamp)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(ExportCancelled):
            render_block(self.project, block, self.folder, signature, self.caps, cancel, resolution=(320, 180))
        Path(path).write_bytes(b'corrupt')
        repaired = render_block(self.project, block, self.folder/'chosen', signature, self.caps,
                            threading.Event(), resolution=(320, 180))
        self.assertGreater(probe_media(self.ffprobe, repaired).duration, .9)
        self.assertFalse(list((self.folder/'chosen').rglob('.render-*')))

    def test_worker_publishes_only_finished_cache_and_stops(self):
        events = queue.Queue(); worker = PrerenderWorker(events)
        self.addCleanup(worker.close)
        worker.request(7, self.project, str(self.folder/'worker'), self.caps, resolution=(160, 90))
        ready = None; deadline = time.monotonic()+35
        while time.monotonic() < deadline:
            generation, kind, payload = events.get(timeout=30)
            self.assertEqual(generation, 7)
            if kind in ('failed', 'error'): self.fail(str(payload))
            if kind == 'plan': self.assertEqual(payload['blocks'][0].state, 'red')
            if kind == 'ready': ready = payload['block']
            if kind == 'done': break
        self.assertIsNotNone(ready)
        self.assertTrue(Path(ready.path).is_file())
        self.assertEqual(ready.state, 'green')
        worker.close(); worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())

    def test_monitor_subtitles_are_kept_when_export_burn_in_is_disabled(self):
        from subtitle_core import SubtitleCue
        self.project.subtitles.burn_in = False
        self.project.subtitles.cues = [SubtitleCue('s', 0., 1., 'Preview')]
        signature = picture_signature(self.project, (320, 180))
        with patch('prerender.render_multicam', wraps=render_multicam) as renderer:
            render_block(self.project, PreviewBlock(0, 10, 10., 'red'), self.folder,
                         signature, self.caps, threading.Event(), resolution=(320, 180))
            self.assertTrue(renderer.call_args.args[0].subtitles.burn_in)
        self.assertFalse(self.project.subtitles.burn_in)

    def test_stack_cache_command_uses_baked_picture_and_seek_offsets(self):
        self.project.video_stack_enabled = True
        signature = picture_signature(self.project, (160, 90))
        path = render_block(self.project, PreviewBlock(0, 20, 10., 'red'), self.folder,
                            signature, self.caps, threading.Event(), resolution=(160, 90))
        segments = tuple(cached_stack_segments(preview_segments(self.project, .5), [(path, 0., 1.)], 10.))
        self.assertEqual(segments[0].camera, -1)
        self.assertEqual(segments[0].layers[0].source_start, .5)
        self.assertEqual(segments[-1].camera, 0)
        command, fps = preview_command(segments[0], self.project, self.ffmpeg, {}, 160, 90)
        self.assertNotIn('exposure', command[command.index('-filter_complex')+1])
        result = subprocess.run(command, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout), 5*160*90*4)


class PrerenderGpuPlaybackTests(unittest.TestCase):
    setUp = gpu_fixture.GpuLiveWorkerTests.setUp
    tearDown = gpu_fixture.GpuLiveWorkerTests.tearDown
    wait = gpu_fixture.GpuLiveWorkerTests.wait
    drain = gpu_fixture.GpuLiveWorkerTests.drain

    def test_cache_replaces_live_mix_without_duplicate_treatment_and_audio_owner_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'baked.mp4'; path.touch()
            audio = self.player._audio.controller
            self.player.submit('prerender_cache', signature=picture_signature(self.project),
                               items=[(str(path), .4, 1.)])
            self.wait(lambda: bool(self.player._prerender_items))
            self.player.submit('seek', time=.7)
            self.wait(lambda: self.player._program.key and self.player._program.key[0] == 'prerender')
            self.assertIsNone(self.player._gpu_context)
            self.assertIs(self.player._audio.controller, audio)
            compose = next(call for call in reversed(self.backends[0].calls) if call['op'] == 'compose')
            self.assertIsNone(compose['subtitle'])
            self.assertFalse(compose['active'])
            self.player.submit('seek', time=1.1)
            self.wait(lambda: self.player._program.key and self.player._program.key[0] != 'prerender')
            self.assertIs(self.player._audio.controller, audio)


if __name__ == '__main__': unittest.main()
