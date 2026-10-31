"""Camera cuts, stable view sizing and long NTSC preview seek regressions."""
from copy import deepcopy
from fractions import Fraction
from types import SimpleNamespace
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from . import test_program_edit_ui as fixture
from .test_transport_controls import RecordingPlayer
from multicam_frame_clock import canonical_frame_rate
from video_stack_preview import preview_command, preview_segments
from video_stack_preview import StackPreview
from multicam_core import CameraClip, MulticamProject


class MulticamMonitorFixTests(unittest.TestCase):
    setUp = fixture.ProgramEditWorkspaceTests.setUp
    tearDown = fixture.ProgramEditWorkspaceTests.tearDown

    def stacked(self):
        ws = self.workspace
        ws.project.video_stack_enabled = True
        ws.project.video_stack_order = [2, 1]
        ws.project.video_stack_hidden = [1]
        ws._refresh()
        ws.player = RecordingPlayer()
        ws._set_stack_preview_mode(True)
        return ws

    def test_click_exits_composition_records_current_cut_and_is_one_undo(self):
        ws = self.stacked()
        before = deepcopy(ws.project)
        ws.current = 3.
        ws.camera_buttons[2].invoke()
        self.assertFalse(ws.project.video_stack_enabled)
        self.assertFalse(ws.video_stack_var.get())
        self.assertFalse(ws._stack_preview_active)
        self.assertEqual(ws.project.camera_at(3.), 2)
        self.assertEqual(ws.project.camera_at(2.), 1)
        self.assertEqual(ws.project.video_stack_order, before.video_stack_order)
        self.assertEqual(ws.project.video_stack_hidden, before.video_stack_hidden)
        self.assertEqual(len(ws.undo_stack), 1)
        configured = [values['project'] for action, values in ws.player.commands if action == 'configure']
        self.assertTrue(configured)
        self.assertFalse(configured[-1].video_stack_enabled)
        self.assertEqual(configured[-1].camera_at(3.), 2)
        ws.undo()
        self.assertEqual(ws.project, before)
        ws.redo()
        self.assertFalse(ws.project.video_stack_enabled)
        self.assertEqual(ws.project.camera_at(3.), 2)
        self.assertEqual(self.callback_errors, [])

    def test_selected_range_camera_switch_exits_composition_in_one_undo(self):
        ws = self.stacked()
        before = deepcopy(ws.project)
        ws.selected_program_range = (3., 6.)
        ws.switch(2)
        self.assertFalse(ws.project.video_stack_enabled)
        self.assertEqual([ws.project.camera_at(t) for t in (2., 4., 7.)], [1, 2, 1])
        self.assertEqual(len(ws.undo_stack), 1)
        ws.undo()
        self.assertEqual(ws.project, before)

    def test_recording_disabled_auditions_camera_without_adding_a_cut(self):
        ws = self.stacked()
        cuts = deepcopy(ws.project.cuts)
        ws.record_var.set(False)
        ws.switch(2)
        self.assertFalse(ws.project.video_stack_enabled)
        self.assertEqual(ws.project.cuts, cuts)
        self.assertIn(('audition', {'camera': 2}), ws.player.commands)

    def test_empty_viewport_space_never_expands_tracks(self):
        ws = self.workspace
        ws.project.master_audio = 'master.wav'
        before = deepcopy(ws.project)
        def sizes(cameras, viewport):
            row, _, _ = ws._timeline_track_layout(cameras, viewport, 100, 22, 40)
            return row, dict(ws._timeline_audio_row_heights)
        small = sizes([1, 2], 200)
        large = sizes([1, 2], 2000)
        self.assertEqual(small, large)
        one = sizes([1], 2000)
        self.assertEqual(one[0], large[0])
        self.assertEqual(one[1][0], large[1][0])
        ws._camera_audio_collapsed = True
        folded = sizes([1], 2000)
        self.assertEqual(folded[0], large[0])
        self.assertEqual(folded[1][0], large[1][0])
        ws.project.timeline_multicam_collapsed = True
        collapsed = sizes([], 2000)
        self.assertEqual(collapsed[1][0], large[1][0])
        ws.project.timeline_multicam_collapsed = before.timeline_multicam_collapsed
        self.assertEqual(ws.project, before)

    def test_manual_heights_round_trip_as_workspace_preferences(self):
        ws = self.workspace
        before = deepcopy(ws.project)
        dirty, undo = ws.dirty, len(ws.undo_stack)
        ws._set_timeline_track_height('video', 64)
        ws._set_timeline_track_height('audio', 96)
        data = ws._workspace_chrome_state()
        self.assertEqual(data['timeline_track_heights'], {'video': 64., 'audio': 96.})
        ws._reset_timeline_track_heights()
        ws._restore_workspace_chrome(data)
        self.root.update_idletasks()
        self.assertEqual((ws._timeline_video_height, ws._timeline_audio_height), (64., 96.))
        self.assertEqual(ws.project, before)
        self.assertEqual((ws.dirty, len(ws.undo_stack)), (dirty, undo))
        for value in (None, True, float('nan'), float('inf'), 0, 10000, 'large'):
            self.assertFalse(ws._set_timeline_track_height('audio', value))
        self.assertEqual(ws._timeline_audio_height, 96.)

    def test_timeline_grip_resizes_whole_pane_without_stretching_rows(self):
        ws = self.workspace
        before = deepcopy(ws.project)
        original = ws.workspace_split.sashpos(0)
        heights = ws._timeline_video_height, ws._timeline_audio_height
        ws._begin_timeline_pane_resize(SimpleNamespace(y_root=400))
        ws._move_timeline_pane_resize(SimpleNamespace(y_root=350))
        self.root.update_idletasks()
        self.assertLess(ws.workspace_split.sashpos(0), original)
        self.assertEqual((ws._timeline_video_height, ws._timeline_audio_height), heights)
        ws._end_timeline_pane_resize(cancel=True)
        self.assertEqual(ws.workspace_split.sashpos(0), original)
        self.assertEqual(ws.project, before)

    def test_twenty_minute_ntsc_seek_has_bounded_decode_prefix(self):
        project = deepcopy(self.workspace.project)
        project.fps = 29.97
        project.master_duration = 3600.
        project.video_stack_enabled = True
        for clip in project.clips: clip.duration = 3600.
        segment = next(preview_segments(project, 1376., single_frame=True))
        command, _ = preview_command(segment, project, 'ffmpeg', {}, 128, 72,
            single_frame=True, source_frame_rates={clip.path: Fraction(60000, 1001) for clip in project.clips})
        seeks = [float(command[i+1]) for i, value in enumerate(command) if value == '-ss']
        self.assertTrue(seeks)
        self.assertTrue(all(value > 1375. for value in seeks), seeks)
        self.assertEqual(canonical_frame_rate(project.fps), Fraction(30000, 1001))


@unittest.skipUnless(os.name == 'nt', 'Windows file identity regression')
class ProxyTimingCacheTests(unittest.TestCase):
    def test_proxy_case_alias_retains_fast_seek_after_validation(self):
        binaries = Path(__file__).resolve().parents[1]/'bin'
        if not (binaries/'ffmpeg.exe').is_file(): self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory(prefix='proxy_case_') as folder:
            source, proxy = Path(folder)/'Original.mp4', Path(folder)/'MixedProxy.mp4'
            subprocess.run([str(binaries/'ffmpeg.exe'), '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=blue:s=128x72:r=30:d=2', '-c:v', 'libx264', str(source)],
                capture_output=True, check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            shutil.copyfile(source, proxy)
            project = MulticamProject(clips=[CameraClip('one', 1, str(source), 0., 2.,
                proxy_path=str(proxy).swapcase())], fps=30., video_stack_enabled=True)
            preview = StackPreview(binaries/'ffmpeg.exe', binaries/'ffprobe.exe', lambda *args: None,
                                   width=128, height=72)
            try:
                with patch('video_stack_preview.preview_command', wraps=preview_command) as command:
                    preview.request(project, 1., False, 1, use_proxies=True)
                    deadline = time.monotonic()+5
                    frame = None
                    while frame is None and time.monotonic() < deadline:
                        frame = preview.take_frame()
                        time.sleep(.01)
                    self.assertIsNotNone(frame)
                    self.assertAlmostEqual(frame['time'], 1.)
                    args, kwargs = command.call_args
                    self.assertEqual(args[0].layers[0].path, str(proxy).swapcase())
                    built, _ = preview_command(*args, **kwargs)
                    self.assertGreater(float(built[built.index('-ss')+1]), .9)
            finally: preview.close()


if __name__ == '__main__': unittest.main()
