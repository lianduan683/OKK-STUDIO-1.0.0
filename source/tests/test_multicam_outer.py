"""Compound programme layers preserve edits, ordering and preview/export pixels."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from exporter_core import Capabilities, ExportSettings, find_binary, probe_media
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from multicam_outer import outer_order, place_outer, resolve_compound
from tests import test_camera_order_ui as fixture
from video_stack_preview import preview_segments, preview_command


class CompoundUITests(unittest.TestCase):
    setUp = fixture.CameraOrderUITests.setUp
    tearDown = fixture.CameraOrderUITests.tearDown

    def collapsed(self):
        ws = self.workspace
        ws.set_multicam_timeline_view(True)
        self.root.update()
        return ws

    def test_insert_above_expand_temporarily_pins_and_collapse_restores(self):
        ws = self.collapsed()
        before = deepcopy(ws.project)
        camera = ws.insert_video_track(('above', 0))
        self.assertEqual(ws.project.multicam_outer_order, [camera, 0])
        self.assertLess(ws._timeline_track_bounds[camera][0], ws._timeline_program_bounds[0])
        self.assertEqual(ws.project.cuts, before.cuts)
        self.assertEqual(ws.project.clips, before.clips)
        self.assertFalse(ws.project.multicam_inner_stack)
        ws.set_multicam_timeline_view(False)
        self.assertEqual(ws._timeline_program_bounds[0], ws._timeline_ruler_h)
        self.assertEqual(ws.project.multicam_outer_order, [camera, 0])
        ws.set_multicam_timeline_view(True)
        self.assertLess(ws._timeline_track_bounds[camera][0], ws._timeline_program_bounds[0])
        ws.undo()
        self.assertFalse(ws.project.timeline_multicam_collapsed)
        ws.redo()
        self.assertTrue(ws.project.timeline_multicam_collapsed)
        self.assertEqual(self.errors, [])

    def test_drag_compound_header_reorders_with_undo_and_esc(self):
        ws = self.collapsed()
        camera = ws.insert_video_track(('above', 0))
        canvas = ws.timeline
        def point(track):
            top, bottom = ws._timeline_track_bounds[track]
            return dict(x=10, y=round((top+bottom)/2-canvas.canvasy(0)))
        before = len(ws.undo_stack)
        canvas.event_generate('<ButtonPress-1>', **point(0))
        canvas.event_generate('<B1-Motion>', **point(camera))
        canvas.event_generate('<ButtonRelease-1>', **point(camera))
        self.assertEqual(ws.project.multicam_outer_order, [0, camera])
        self.assertEqual(len(ws.undo_stack), before+1)
        ws.undo()
        self.assertEqual(ws.project.multicam_outer_order, [camera, 0])
        self.assertEqual(self.errors, [])

    def test_group_context_has_above_below_and_open_internal_view(self):
        ws = self.collapsed()
        top, bottom = ws._timeline_program_bounds
        event = SimpleNamespace(x=10, y=(top+bottom)/2, x_root=10000, y_root=10000)
        with patch('video_stack_ui.tk.Menu.tk_popup'):
            self.assertTrue(ws._video_stack_context(event))
        menu = ws.video_stack_context_menu
        labels = [menu.entrycget(i, 'label') for i in range(menu.index('end')+1) if menu.type(i) != 'separator']
        self.assertIn('在时间线上打开多机位', labels)
        tracks = next(menu.nametowidget(menu.entrycget(i, 'menu'))
            for i in range(menu.index('end')+1) if menu.type(i) == 'cascade' and menu.entrycget(i, 'label') == '插入视频轨道')
        self.assertIn('插入 多机位轨道 上方', [tracks.entrycget(i, 'label') for i in range(tracks.index('end')+1)])
        self.assertEqual(self.errors, [])

    def test_outer_order_roundtrip_and_editing_internal_cut_keeps_composition(self):
        ws = self.collapsed()
        camera = ws.insert_video_track(('below', 0))
        payload = ws.project.to_payload('.')
        self.assertEqual(payload['version'], 21)
        restored = MulticamProject.from_payload(payload, '.')
        self.assertEqual(restored.multicam_outer_order, [0, camera])
        ws.selected_program_range = (0., 4.)
        ws._switch_selected_camera(2)
        self.assertTrue(ws.project.video_stack_enabled)
        self.assertEqual(ws.project.multicam_outer_order, [0, camera])
        self.assertEqual(ws.project.camera_at(1.), 2)
        self.assertEqual(self.errors, [])

    def test_full_session_retains_outer_order_and_audio_settings(self):
        from project_file import build_project_payload, write_project_file, read_project_file
        from studio_state import restore_okk
        ws = self.collapsed()
        camera = ws.insert_video_track(('above', 0))
        ws.project.audio_mixer = {'enabled': True, 'strips': {'master': {'gain_db': -7.}}}
        original = deepcopy(ws.project)
        self.root.multicam_workspace = ws
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'外层机位.rtxproj'
            write_project_file(path, build_project_payload(self.root, app_version='1.0.8'))
            ws.project = MulticamProject()
            restore_okk(self.root, read_project_file(path)['state']['okk_multicam'])
        self.assertEqual(ws.project.multicam_outer_order, [camera, 0])
        self.assertEqual(ws.project.audio_mixer, original.audio_mixer)
        self.assertEqual(ws.project.cuts, original.cuts)
        self.assertEqual([c.id for c in ws.project.clips], [c.id for c in original.clips])
        self.assertLess(ws._timeline_track_bounds[camera][0], ws._timeline_program_bounds[0])
        self.assertEqual(self.errors, [])


class CompoundRenderTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1]
        self.ffmpeg, self.ffprobe = find_binary('ffmpeg', source), find_binary('ffprobe', source)
        if not self.ffmpeg or not self.ffprobe: self.skipTest('FFmpeg required')
        self.temp = tempfile.TemporaryDirectory(prefix='compound-render-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.patch = patch('cache_paths.cache_dir', return_value=self.folder/'cache')
        self.patch.start(); self.addCleanup(self.patch.stop)
        (self.folder/'cache').mkdir()
        for name, color in [('red', 'red'), ('green', 'lime'), ('blue', 'blue')]:
            subprocess.run([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                f'color=c={color}:s=96x54:r=10:d=4', '-c:v', 'libx264', '-preset', 'ultrafast',
                '-an', str(self.folder/(name+'.mp4'))], check=True, capture_output=True)
        self.project = MulticamProject(fps=10., clips=[
            CameraClip('r', 1, str(self.folder/'red.mp4'), 0., 4., has_audio=False),
            CameraClip('g', 2, str(self.folder/'green.mp4'), 0., 4., has_audio=False),
            CameraClip('b', 3, str(self.folder/'blue.mp4'), 1., 2., has_audio=False)],
            cuts=[Cut(0., 1), Cut(2., 2)])
        place_outer(self.project, 3, ('above', 0))

    def runner(self, command, callback):
        process = subprocess.run(command, capture_output=True, timeout=45)
        return process.returncode, process.stderr.decode('utf-8', 'replace')

    def render(self, name):
        output = self.folder/name
        settings = ExportSettings(codec='libx264', preset='ultrafast', quality=0,
            resolution='timeline', audio='none', decode_mode='cpu', bit_depth='8-bit')
        with patch('multicam_core.cache_dir', return_value=self.folder):
            render_multicam(self.project, output, self.ffmpeg, self.ffprobe, settings,
                Capabilities(ffmpeg_path=self.ffmpeg, ffprobe_path=self.ffprobe), self.runner)
        return output

    def pixel(self, path, time):
        raw = subprocess.run([self.ffmpeg, '-v', 'error', '-ss', str(time), '-i', str(path),
            '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
            check=True, capture_output=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(54,96,3)[27,48].astype(int)

    def test_opaque_outer_track_covers_only_its_range_and_below_group_is_hidden(self):
        before = deepcopy(self.project)
        above = self.render('above.mp4')
        self.assertGreater(self.pixel(above, .5)[0], 230)
        self.assertGreater(self.pixel(above, 1.5)[2], 230)
        self.assertGreater(self.pixel(above, 2.5)[2], 230)
        self.assertGreater(self.pixel(above, 3.5)[1], 230)
        self.assertEqual(self.project, before)
        self.project.multicam_outer_order = [0, 3]
        below = self.render('below.mp4')
        self.assertGreater(self.pixel(below, 1.5)[0], 230)
        self.assertGreater(self.pixel(below, 2.5)[1], 230)

    def test_alpha_preview_and_export_use_the_same_nested_programme(self):
        alpha = self.folder/'alpha.mov'
        subprocess.run([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=c=blue@0.5:s=96x54:r=10:d=2,format=argb', '-c:v', 'qtrle', '-an', str(alpha)],
            check=True, capture_output=True)
        self.project.clips[2].path = str(alpha)
        self.project.clips[2].has_alpha = True
        output = self.render('alpha-output.mp4')
        exported = self.pixel(output, 1.5)
        self.assertTrue(100 < exported[0] < 155 and 100 < exported[2] < 155, exported)
        segment = next(preview_segments(self.project, 1.5, single_frame=True))
        segment = resolve_compound(segment, self.project, self.ffmpeg, self.ffprobe, 96, 54, preview=True)
        media = {layer.path: probe_media(self.ffprobe, layer.path) for layer in segment.layers}
        command, _ = preview_command(segment, self.project, self.ffmpeg, media,
                                     96, 54, single_frame=True)
        raw = subprocess.run(command, check=True, capture_output=True, timeout=30).stdout
        preview = np.frombuffer(raw, np.uint8).reshape(54,96,4)[27,48,:3].astype(int)
        np.testing.assert_allclose(preview, exported, atol=8)

    def test_nested_transition_is_preserved_and_reuses_atomic_compound_cache(self):
        from creative_core import Transition
        self.project.transitions = [Transition(2., 'fade', .8)]
        self.project.multicam_outer_order = [0, 3]
        before = deepcopy(self.project)
        output = self.render('transition.mp4')
        middle = self.pixel(output, 2.)
        self.assertTrue(middle[0] > 70 and middle[1] > 70 and middle[2] < 15, middle)
        self.assertEqual(self.project, before)
        segment = next(preview_segments(self.project, 2., single_frame=True))
        resolved = resolve_compound(segment, self.project, self.ffmpeg, self.ffprobe, 96, 54, preview=True)
        path = Path(next(layer.path for layer in resolved.layers if layer.camera == 0))
        stamp = path.stat().st_mtime_ns
        self.assertEqual(resolve_compound(segment, self.project, self.ffmpeg, self.ffprobe,
                         96, 54, preview=True), resolved)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_outer_extension_does_not_extend_or_replace_the_compound_edit(self):
        self.project.clips[2].start = 4.
        self.project.clips[2].duration = 2.
        self.project.multicam_outer_order = [0, 3]
        output = self.render('extension.mp4')
        self.assertGreater(self.pixel(output, 3.5)[1], 230)
        self.assertGreater(self.pixel(output, 4.5)[2], 230)
        self.assertEqual(self.project.cuts, [Cut(0.,1), Cut(2.,2)])

    def test_programme_grade_stays_inside_the_compound_and_does_not_grade_new_top_track(self):
        from creative_core import ColorGrade
        from program_edits import ClipGrade
        self.project.clip_grades = [ClipGrade(0., 4., ColorGrade(exposure=-1.))]
        output = self.render('graded-group.mp4')
        base = self.pixel(output, .5)
        self.assertTrue(90 < base[0] < 180, base)
        self.assertGreater(self.pixel(output, 1.5)[2], 230)

    def test_complex_compound_supports_ten_bit_cache_and_cancel(self):
        import threading
        from creative_core import Transition
        from exporter_core import ExportCancelled
        from video_stack_render import plan_stack_segments
        self.project.transitions = [Transition(2., 'fade', .8)]
        segment = next(s for s in plan_stack_segments(self.project) if s.start <= 2. < s.end)
        resolved = resolve_compound(segment, self.project, self.ffmpeg, self.ffprobe, 96, 54, deep=True)
        cache = next(layer.path for layer in resolved.layers if layer.camera == 0)
        self.assertIn('10', probe_media(self.ffprobe, cache).pixel_format)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(ExportCancelled):
            resolve_compound(segment, self.project, self.ffmpeg, self.ffprobe, 96, 54, cancel=cancel)


class CompoundClockTests(unittest.TestCase):
    def test_outer_half_frame_offsets_use_the_existing_project_rounding_rule(self):
        from video_stack_render import plan_stack_segments
        project = MulticamProject(fps=10., clips=[CameraClip('inside', 1, 'inside.mp4', 0., 2.),
            CameraClip('outer', 2, 'outer.mp4', .05, .1)], cuts=[Cut(0., 1)])
        place_outer(project, 2, ('above', 0))
        active = [(segment.start, segment.frames) for segment in plan_stack_segments(project)
                  if any(layer.camera == 2 for layer in segment.layers)]
        self.assertEqual(active, [(.1, 1)])


if __name__ == '__main__': unittest.main()
