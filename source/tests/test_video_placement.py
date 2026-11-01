"""Inspector placement, persistence, alpha pixels and preview/export parity."""
from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from exporter_core import Capabilities, ExportSettings, find_binary, probe_media
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from multicam_outer import place_outer
from prerender import picture_signature
from video_placement import has_placement, placement_values, placement_filter
from video_stack_preview import StackPreview, preview_command, preview_segments
from tests import test_clip_inspector as fixture


class PlacementInspectorTests(unittest.TestCase):
    setUp = fixture.ClipInspectorTests.setUp
    tearDown = fixture.ClipInspectorTests.tearDown

    def select(self, *identifiers):
        self.workspace._set_timeline_selection({('clip', identifier) for identifier in identifiers})

    def test_source_selection_updates_fields_and_only_moves_that_instance(self):
        w, p = self.workspace, self.panel
        self.select('two')
        before = deepcopy(w.project)
        p.placement_x.set('192'); p.placement_y.set('-108'); p.placement_scale.set('50')
        self.assertTrue(p.apply_placement())
        self.assertEqual(placement_values(w.project.clips[1]), (.1, -.1, .5))
        self.assertEqual(w.project.clips[0], before.clips[0])
        self.assertEqual(w.project.cuts, before.cuts)
        self.assertEqual(w.project.audio_overrides, before.audio_overrides)
        self.assertEqual(w.project.video_stack_enabled, before.video_stack_enabled)
        self.assertEqual(len(w.undo_stack), 1)
        self.assertEqual(p.placement_x.get(), '192')
        self.assertIn('two.mp4', p.placement_text.get())
        w.undo(); self.assertFalse(has_placement(w.project))
        w.redo(); self.assertEqual(placement_values(w.project.clips[1]), (.1, -.1, .5))

    def test_source_switch_and_reset_restore_correct_values(self):
        self.select('one')
        self.workspace.apply_video_placement(dict(x=.2, y=.1, scale=1.5))
        self.select('two')
        self.assertEqual(self.panel.placement_scale.get(), '100')
        self.select('one')
        self.assertEqual(self.panel.placement_scale.get(), '150')
        self.panel.reset_placement()
        self.assertFalse(has_placement(self.workspace.project))

    def test_multiple_selected_clips_apply_once_and_resize_preserves_timing(self):
        self.select('one', 'two')
        before = deepcopy(self.workspace.project.clips)
        self.workspace.apply_video_placement(dict(x=-.3, y=.4, scale=.25))
        self.assertEqual(len(self.workspace.undo_stack), 1)
        for old, clip in zip(before, self.workspace.project.clips):
            self.assertEqual((clip.id, clip.start, clip.duration, clip.source_in),
                             (old.id, old.start, old.duration, old.source_in))
            self.assertEqual(placement_values(clip), (-.3, .4, .25))

    def test_outer_layer_uses_same_inspector_without_modifying_compound(self):
        w = self.workspace
        w.set_multicam_timeline_view(True)
        w.project.clips.append(CameraClip('person', 3, 'person.mov', 0, 6, has_alpha=True))
        place_outer(w.project, 3, ('above', 0)); w._refresh()
        self.select('person')
        cuts = deepcopy(w.project.cuts)
        w.apply_video_placement(dict(x=.25, y=0, scale=.7))
        self.assertEqual(w.project.cuts, cuts)
        self.assertEqual(w.project.multicam_outer_order, [3, 0])
        self.assertEqual(placement_values(w.project.clips[-1]), (.25, 0, .7))

    def test_save_load_and_cache_signature_include_placement(self):
        w = self.workspace
        signature = picture_signature(w.project)
        self.select('one'); w.apply_video_placement(dict(x=.25, scale=.5))
        payload = w.project.to_payload('.')
        self.assertEqual(payload['version'], 22)
        restored = MulticamProject.from_payload(payload, '.')
        self.assertEqual(restored.clips[0].placement, w.project.clips[0].placement)
        self.assertNotEqual(picture_signature(w.project), signature)
        self.assertNotIn('placement', payload['clips'][1])

    def test_invalid_values_never_change_project(self):
        self.select('one')
        before = deepcopy(self.workspace.project)
        for value in ('NaN', '0', '401', '-10'):
            self.panel.placement_scale.set(value)
            self.panel.apply_placement()
            self.assertEqual(self.workspace.project, before)
            self.assertEqual(self.workspace.undo_stack, [])
        self.assertEqual(self.dialog.call_count, 4)

    def test_audio_only_selection_disables_video_controls(self):
        w = self.workspace
        w.project.master_audio = 'master.wav'; w.project.master_duration = 10
        w._set_timeline_selection({('audio', 'master')})
        self.assertEqual(w.video_placement_targets(), [])
        self.assertTrue(all(str(widget.cget('state')) == 'disabled' for widget in self.panel.placement_fields))

    def test_identity_keeps_old_payload_and_corrupt_placement_is_rejected(self):
        project = self.workspace.project
        self.assertLess(project.to_payload('.')['version'], 22)
        for value in ({'x': float('nan')}, {'scale': True}, {'scale': .001}, {'x': 3}, {'unknown': 1}):
            candidate = deepcopy(project)
            candidate.clips[0].placement = value
            with self.assertRaises(ValueError): candidate.validate()

    def test_merge_with_different_positions_is_rejected_instead_of_losing_settings(self):
        from timeline_merge import plan_source_merge
        w = self.workspace
        self.select('one'); w.apply_video_placement(dict(x=.2))
        with self.assertRaisesRegex(ValueError, '位置或大小不同'):
            plan_source_merge(w.project, {'one', 'two'})

    def test_player_keeps_single_placement_on_native_path(self):
        from video_stack_player import VideoStackPlayerMixin
        class Player(VideoStackPlayerMixin):
            def _emit(self, *args): pass
        player = Player()
        player._init_stack_preview()
        player._project = deepcopy(self.workspace.project)
        player._project.clips[0].placement = dict(x=.2, scale=.5)
        player._program = player._program_standby = player._comparison = player._comparison_standby = None
        player._grade_draft = None
        player._configure_stack_mode()
        self.assertFalse(player._stack_enabled)
        preview = player._stack_project()
        self.assertEqual(preview.multicam_outer_order, [0])
        self.assertFalse(preview.multicam_inner_stack)
        self.assertEqual(preview.cuts, self.workspace.project.cuts)
        self.assertFalse(player._project.video_stack_enabled)
        player._project.clips[0].placement = {}
        player._configure_stack_mode()
        self.assertFalse(player._stack_enabled)


class PlacementMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1]
        cls.ffmpeg, cls.ffprobe = find_binary('ffmpeg', source), find_binary('ffprobe', source)
        if not cls.ffmpeg or not cls.ffprobe: raise unittest.SkipTest('FFmpeg required')
        cls.temp = tempfile.TemporaryDirectory(prefix='video-placement-')
        cls.folder = Path(cls.temp.name)
        for name, color in [('blue', 'blue'), ('red', 'red'), ('alpha', 'red@0.5')]:
            subprocess.run([cls.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                f'color=c={color}:s=96x54:r=10:d=2,format=argb', '-c:v', 'qtrle', '-an',
                str(cls.folder/(name+'.mov'))], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def project(self, alpha=False):
        project = MulticamProject(fps=10, clips=[
            CameraClip('bg', 1, str(self.folder/'blue.mov'), 0, 2, has_audio=False),
            CameraClip('person', 2, str(self.folder/('alpha.mov' if alpha else 'red.mov')), 0, 2,
                       has_audio=False, has_alpha=alpha, placement=dict(x=.25, y=.1, scale=.5))],
            cuts=[Cut(0, 1)])
        place_outer(project, 2, ('above', 0))
        return project

    def test_alpha_placement_preview_and_export_pixels_agree(self):
        project = self.project(alpha=True)
        segment = next(preview_segments(project, .5, single_frame=True))
        from multicam_outer import resolve_compound
        segment = resolve_compound(segment, project, self.ffmpeg, self.ffprobe, 96, 54, preview=True)
        media = {layer.path: probe_media(self.ffprobe, layer.path) for layer in segment.layers}
        command, _ = preview_command(segment, project, self.ffmpeg, media, 96, 54, single_frame=True)
        raw = subprocess.run(command, check=True, capture_output=True, timeout=20).stdout
        pixels = np.frombuffer(raw, np.uint8).reshape(54, 96, 4)
        self.assertGreater(pixels[30, 24, 2], 230)
        self.assertGreater(pixels[8, 72, 2], 230)
        self.assertTrue(100 < pixels[30, 72, 0] < 155)
        self.assertTrue(100 < pixels[30, 72, 2] < 155)
        output = self.folder/'placed.mp4'
        settings = ExportSettings(codec='libx264', preset='ultrafast', quality=0, resolution='timeline',
                                  audio='none', decode_mode='cpu', bit_depth='8-bit')
        runner = lambda command, callback: self.run_encoder(command)
        render_multicam(project, output, self.ffmpeg, self.ffprobe, settings,
            Capabilities(ffmpeg_path=self.ffmpeg, ffprobe_path=self.ffprobe), runner)
        raw = subprocess.run([self.ffmpeg, '-v', 'error', '-ss', '0.5', '-i', str(output),
            '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], check=True, capture_output=True).stdout
        exported = np.frombuffer(raw, np.uint8).reshape(54, 96, 3)
        np.testing.assert_allclose(exported[30, 72], pixels[30, 72, :3], atol=10)
        self.assertGreater(exported[30, 24, 2], 230)

    def run_encoder(self, command):
        result = subprocess.run(command, capture_output=True, timeout=30)
        return result.returncode, result.stderr.decode('utf-8', 'replace')

    def test_reduced_opaque_layer_does_not_hide_background_during_live_preview(self):
        project = self.project()
        errors = []
        controller = StackPreview(self.ffmpeg, self.ffprobe, lambda *event: errors.append(event), width=96, height=54)
        try:
            controller.request(project, .5, False, 1)
            deadline = time.monotonic()+10
            frame = None
            while frame is None and time.monotonic() < deadline:
                frame = controller.take_frame()
                if frame is None: time.sleep(.02)
            self.assertIsNotNone(frame, errors)
            pixels = np.frombuffer(frame['rgba'], np.uint8).reshape(frame['height'], frame['width'], 4)
            self.assertGreater(pixels[30, 24, 2], 230)
        finally: controller.close()

    def test_plain_programme_export_preserves_camera_cuts_and_placement(self):
        project = self.project()
        project.multicam_outer_order = []
        project.video_stack_enabled = False
        project.cuts = [Cut(0, 2), Cut(1, 1)]
        output = self.folder/'programme.mp4'
        settings = ExportSettings(codec='libx264', preset='ultrafast', quality=0, resolution='timeline',
                                  audio='none', decode_mode='cpu', bit_depth='8-bit')
        render_multicam(project, output, self.ffmpeg, self.ffprobe, settings,
            Capabilities(ffmpeg_path=self.ffmpeg, ffprobe_path=self.ffprobe), lambda cmd, cb: self.run_encoder(cmd))
        for second, expected in [('0.5', 'red'), ('1.5', 'blue')]:
            raw = subprocess.run([self.ffmpeg, '-v', 'error', '-ss', second, '-i', str(output),
                '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], check=True, capture_output=True).stdout
            pixel = np.frombuffer(raw, np.uint8).reshape(54, 96, 3)
            if expected == 'red':
                self.assertGreater(pixel[30, 72, 0], 230)
                self.assertLess(pixel[30, 24].max(), 12)
            else: self.assertGreater(pixel[30, 24, 2], 230)

    def test_transition_retains_independent_placement_of_each_side(self):
        from creative_core import Transition
        project = self.project()
        project.multicam_outer_order = []; project.video_stack_enabled = False
        project.cuts = [Cut(0, 2), Cut(1, 1)]
        project.transitions = [Transition(1, 'fade', .4)]
        output = self.folder/'transition.mp4'
        settings = ExportSettings(codec='libx264', preset='ultrafast', quality=0, resolution='timeline',
                                  audio='none', decode_mode='cpu', bit_depth='8-bit')
        render_multicam(project, output, self.ffmpeg, self.ffprobe, settings,
            Capabilities(ffmpeg_path=self.ffmpeg, ffprobe_path=self.ffprobe), lambda cmd, cb: self.run_encoder(cmd))
        raw = subprocess.run([self.ffmpeg, '-v', 'error', '-ss', '1.0', '-i', str(output),
            '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], check=True, capture_output=True).stdout
        pixels = np.frombuffer(raw, np.uint8).reshape(54, 96, 3)
        self.assertGreater(pixels[30, 72, 0], 70)
        self.assertGreater(pixels[30, 72, 2], 70)
        self.assertLess(pixels[30, 24, 0], 15)
        self.assertGreater(pixels[30, 24, 2], 70)


if __name__ == '__main__': unittest.main()
