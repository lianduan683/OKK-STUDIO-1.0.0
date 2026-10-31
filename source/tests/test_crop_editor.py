"""Geometry, sampled automatic crop and real Tk rectangle interactions."""
from __future__ import annotations

import copy
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clip_transform import ClipTransform, framing_filter, transform_at
from crop_geometry import (ASPECT, automatic_crop, crop_at, draw_crop, maximum_crop,
    move_crop, rect_from_transform, resize_crop, transform_from_rect, zoom_crop)
from crop_frame import CropCancelled, content_bounds, decode_frame, pixels_ppm, ppm_pixels
from crop_editor import open_crop_editor
from exporter_core import CREATE_NO_WINDOW
from multicam_core import CameraClip, Cut, MulticamProject
from tests import test_timeline_drag_merge as fixture


class CropGeometryTests(unittest.TestCase):
    def assert_rect(self, rect, size):
        l, t, r, b = rect
        self.assertGreaterEqual(l, -1e-8); self.assertGreaterEqual(t, -1e-8)
        self.assertLessEqual(r, size[0]+1e-8); self.assertLessEqual(b, size[1]+1e-8)
        self.assertAlmostEqual((r-l)/(b-t), ASPECT, places=7)
        self.assertGreaterEqual(r-l+1e-8, maximum_crop(size)[0]/4)

    def test_move_resize_and_redraw_preserve_16_9_inside_every_source_shape(self):
        rng = random.Random(481)
        for size in ((160, 120), (3240, 2160), (1080, 1920), (4096, 2160)):
            rect = automatic_crop(size)
            for _ in range(60):
                rect = zoom_crop(rect, rng.uniform(1, 4), size)
                rect = move_crop(rect, (rng.uniform(-size[0], size[0]), rng.uniform(-size[1], size[1])), size)
                self.assert_rect(rect, size)
                handle = rng.choice(('nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'))
                rect = resize_crop(rect, handle, (rng.uniform(-100, size[0]+100), rng.uniform(-100, size[1]+100)), size)
                self.assert_rect(rect, size)
                rebuilt = rect_from_transform(transform_from_rect(rect, size), size)
                for actual, expected in zip(rebuilt, rect): self.assertAlmostEqual(actual, expected, places=6)
            self.assert_rect(draw_crop((size[0]-1, 1), (size[0]*2, -100), size), size)

    def test_vertical_selection_keeps_top_or_bottom_and_corner_anchor(self):
        size = (160, 120)
        self.assertEqual(rect_from_transform(ClipTransform('fill', 1, 0, -1), size), (0, 0, 160, 90))
        self.assertEqual(rect_from_transform(ClipTransform('fill', 1, 0, 1), size), (0, 30, 160, 120))
        original = rect_from_transform(ClipTransform('fill', 2), size)
        smaller = resize_crop(original, 'se', (original[2]-20, original[3]-10), size)
        self.assertEqual(smaller[:2], original[:2])
        self.assertGreater(transform_from_rect(smaller, size).zoom, 2)

    def test_black_borders_union_samples_and_blank_fallback(self):
        source = np.zeros((120, 160, 3), dtype=np.uint8)
        source[:, 20:150] = (170, 120, 80)
        self.assertEqual(content_bounds([source]*4), (20, 0, 150, 120))
        rect = automatic_crop((160, 120), content_bounds([source]*4))
        self.assertAlmostEqual(transform_from_rect(rect, (160, 120)).zoom, 160/130)
        self.assertAlmostEqual(transform_from_rect(rect, (160, 120)).x, 1/3)
        temporary_dark = source.copy(); temporary_dark[:25] = 0
        self.assertEqual(content_bounds([source, temporary_dark]), (20, 0, 150, 120))
        self.assertEqual(content_bounds([np.zeros_like(source)]*4), (0, 0, 160, 120))
        full = np.full_like(source, 90)
        self.assertEqual(content_bounds([source, full]), (0, 0, 160, 120))
        self.assertTrue(np.array_equal(ppm_pixels(pixels_ppm(source)), source))


class CropMediaFixture:
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(__file__).resolve().parents[1]/'bin/ffmpeg.exe'
        if not cls.ffmpeg.is_file(): raise unittest.SkipTest('FFmpeg unavailable')
        cls.temp = tempfile.TemporaryDirectory(prefix='crop_rectangle_')
        cls.folder = Path(cls.temp.name)
        cls.source = cls.folder/'asymmetric-open-gate.mkv'
        cls.source_clean = cls.folder/'open-gate.mkv'
        raw = np.zeros((120, 160, 3), dtype=np.uint8)
        raw[:, 20:150] = (180, 140, 90)
        for y in range(120): raw[y, 20:150, 0] = 50+y
        cls.raw = raw
        cls.run_command([cls.ffmpeg, '-v', 'error', '-y', '-f', 'rawvideo', '-pixel_format', 'rgb24',
            '-video_size', '160x120', '-framerate', '10', '-i', 'pipe:0', '-an', '-c:v', 'ffv1',
            '-threads', '1', str(cls.source)], input=raw.tobytes()*40)
        clean = raw.copy(); clean[:, :20] = clean[:, 20:21]; clean[:, 150:] = clean[:, 149:150]
        cls.run_command([cls.ffmpeg, '-v', 'error', '-y', '-f', 'rawvideo', '-pixel_format', 'rgb24',
            '-video_size', '160x120', '-framerate', '10', '-i', 'pipe:0', '-an', '-c:v', 'ffv1',
            '-threads', '1', str(cls.source_clean)], input=clean.tobytes()*40)

    @staticmethod
    def run_command(command, input=None):
        result = subprocess.run(list(map(str, command)), input=input, capture_output=True, timeout=30,
                                creationflags=CREATE_NO_WINDOW)
        if result.returncode: raise AssertionError(result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()


class CropFrameMediaTests(CropMediaFixture, unittest.TestCase):
    def test_real_frame_sampling_auto_crop_matches_export_filter_and_vertical_movement(self):
        frames = [decode_frame(self.ffmpeg, self.source, time, threading.Event()) for time in (.1, 1, 2, 3)]
        size = frames[0].shape[1], frames[0].shape[0]
        rect = automatic_crop(size, content_bounds(frames))
        transform = transform_from_rect(rect, size)
        self.assertAlmostEqual(transform.zoom, 160/130, delta=.02)
        self.assertAlmostEqual(transform.x, 1/3, delta=.05)
        def output(t):
            raw = self.run_command([self.ffmpeg, '-v', 'error', '-i', self.source,
                '-vf', framing_filter(t, 160, 90), '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
            return np.frombuffer(raw, dtype=np.uint8).reshape(90, 160, 3)
        cropped = output(transform)
        self.assertGreater(int(np.min(cropped[:, 3:-3, 1])), 120)
        top = output(ClipTransform('fill', 2, transform.x, -1))
        bottom = output(ClipTransform('fill', 2, transform.x, 1))
        self.assertGreater(int(bottom[45, 80, 0])-int(top[45, 80, 0]), 50)
        token = threading.Event(); token.set()
        with self.assertRaises(CropCancelled): decode_frame(self.ffmpeg, self.source, 0, token)


class CropEditorTkTests(CropMediaFixture, unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)
        self.workspace.project = MulticamProject(clips=[CameraClip('open', 1, str(self.source), 0, 4)],
                                                cuts=[Cut(0, 1)], fps=10)
        self.root.caps.ffmpeg_path = str(self.ffmpeg)
        self.workspace._refresh()
        self.workspace.select_program_range(0, 4)

    def tearDown(self):
        fixture.TimelineDropTests.tearDown(self)

    def wait(self, predicate):
        end = time.monotonic()+12
        while time.monotonic() < end:
            self.root.update()
            if predicate(): return
            time.sleep(.01)
        self.fail('Crop preview did not become ready')

    def editor(self, automatic=False):
        result = open_crop_editor(self.workspace, automatic=automatic)
        self.wait(lambda: not result._pending and result._pixels is not None
                  and (not automatic or not result._detecting))
        return result

    def test_drag_frame_resize_then_commit_is_one_undo_and_saved_in_existing_format(self):
        ws = self.workspace
        original = copy.deepcopy(ws.project)
        editor = self.editor()
        self.assertTrue(editor.canvas.find_withtag('crop'))
        editor._zoom_changed('200')
        self.wait(lambda: editor._draw_after is None)
        old = editor.rect
        cx, cy = editor._canvas_point(((old[0]+old[2])/2, (old[1]+old[3])/2))
        editor._press(SimpleNamespace(x=cx, y=cy))
        editor._release(SimpleNamespace(x=cx+20, y=cy-15))
        self.wait(lambda: editor._draw_after is None)
        self.assertGreater(editor.rect[0], old[0]); self.assertLess(editor.rect[1], old[1])
        moved = editor.rect
        x, y = editor._handles['se']
        editor._press(SimpleNamespace(x=x, y=y))
        editor._release(SimpleNamespace(x=x-15, y=y-10))
        self.assertLess(editor.rect[2]-editor.rect[0], moved[2]-moved[0])
        desired = transform_from_rect(editor.rect, editor.source_size)
        self.assertEqual(ws.project, original)
        self.assertTrue(editor.apply())
        self.assertEqual(transform_at(ws.project, 2), desired)
        self.assertEqual(len(ws.undo_stack), 1)
        self.assertEqual(ws.project.project_settings['width']*9, ws.project.project_settings['height']*16)
        path = self.folder/'manual.multicam.json'
        ws.project.save(path)
        self.assertEqual(MulticamProject.load(path).clip_transforms, ws.project.clip_transforms)
        ws.undo(); self.assertEqual(ws.project, original)
        ws.redo(); self.assertEqual(transform_at(ws.project, 2), desired)
        self.assertEqual(self.errors, [])

    def test_automatic_black_border_crop_can_be_manually_repositioned_before_apply(self):
        editor = self.editor(automatic=True)
        auto = transform_from_rect(editor.rect, editor.source_size)
        self.assertAlmostEqual(auto.zoom, 160/130, delta=.02)
        self.assertIn('黑边', editor.status.get())
        editor.rect = move_crop(editor.rect, (0, -10000), editor.source_size)
        self.assertEqual(transform_from_rect(editor.rect, editor.source_size).y, -1)
        self.assertTrue(editor.apply())
        self.assertEqual(transform_at(self.workspace.project, 1).y, -1)

    def test_cancel_stale_result_and_frame_navigation_do_not_change_project_or_draft(self):
        ws = self.workspace
        original = copy.deepcopy(ws.project)
        editor = self.editor()
        editor._zoom_changed('180')
        before = transform_from_rect(editor.rect, editor.source_size)
        editor.frame_time.set(2)
        editor._request_frame()
        self.wait(lambda: not editor._pending)
        self.assertAlmostEqual(transform_from_rect(editor.rect, editor.source_size).zoom, before.zoom)
        self.assertEqual(ws.project, original)
        editor.close()
        self.assertEqual(ws.undo_stack, [])
        editor = self.editor()
        ws.project = copy.deepcopy(ws.project)
        self.assertFalse(editor.apply())
        self.assertIn('已变化', editor.status.get())
        editor.close()
        self.assertEqual(ws.project, original)

    def test_selection_required_buttons_route_and_closing_workspace_cancels_worker(self):
        ws = self.workspace
        with patch('crop_editor.open_crop_editor', return_value='manual') as opener:
            self.assertEqual(ws.clip_inspector.manual_framing(), 'manual')
            opener.assert_called_with(ws)
            ws.clip_inspector.auto_framing()
            opener.assert_called_with(ws, automatic=True)
        editor = self.editor()
        editor.automatic()
        editor.close()
        self.assertTrue(editor._cancel.is_set())
        ws.selected_program_range = None
        self.assertIsNone(open_crop_editor(ws))

    def test_crop_button_opens_real_frame_without_preselecting_programme_range(self):
        ws = self.workspace
        ws.project.edit_points = [2]
        ws._refresh()
        ws._set_timeline_selection(set())
        ws.current = 2.5
        ws._show_time()
        original = copy.deepcopy(ws.project)
        self.assertIsNone(ws.selected_program_range)
        editor = ws.clip_inspector.manual_framing()
        self.assertIsNotNone(editor)
        self.wait(lambda: not editor._pending and editor._pixels is not None)
        self.assertEqual(ws.selected_program_range, (2, 4))
        self.assertTrue(editor.canvas.find_withtag('crop'))
        editor.close()
        self.assertEqual(ws.project, original)
        self.assertEqual(ws.undo_stack, [])

    def test_reopen_16_9_crop_keeps_rectangle_and_second_adjustment_preserves_neighbours(self):
        ws = self.workspace
        ws.select_program_range(1, 3)
        editor = self.editor()
        editor._zoom_changed('160')
        first = transform_from_rect(editor.rect, editor.source_size)
        self.assertTrue(editor.apply())
        editor = self.editor()
        restored = transform_from_rect(editor.rect, editor.source_size)
        self.assertAlmostEqual(restored.zoom, first.zoom)
        self.assertAlmostEqual(restored.x, first.x)
        self.assertAlmostEqual(restored.y, first.y)
        editor.rect = move_crop(editor.rect, (0, -10000), editor.source_size)
        self.assertTrue(editor.apply())
        self.assertEqual(transform_at(ws.project, 1.5).y, -1)
        self.assertEqual(transform_at(ws.project, .5), ClipTransform())
        self.assertEqual(transform_at(ws.project, 3), ClipTransform())
        ws.undo(); self.assertEqual(transform_at(ws.project, 2), first)
        ws.redo()
        ws.select_program_range(1, 3)
        undo_count = len(ws.undo_stack)
        self.root.resolution_var = tk.StringVar(self.root, value='1080p')
        editor = self.editor()
        self.assertTrue(editor.apply())
        self.assertEqual(self.root.resolution_var.get(), '时间线分辨率')
        self.assertEqual(len(ws.undo_stack), undo_count)

    def test_close_cancels_running_decode_and_invalid_media_is_reported_in_dialog(self):
        entered = threading.Event()
        def waiting(ffmpeg, path, position, cancel):
            entered.set()
            cancel.wait(10)
            raise CropCancelled()
        with patch('crop_editor.decode_frame', side_effect=waiting):
            editor = open_crop_editor(self.workspace)
            self.wait(entered.is_set)
            editor.close()
            self.assertTrue(all(not worker.is_alive() for worker in editor._workers))
        with patch.object(MulticamProject, 'media_at', side_effect=ValueError('缓存已失效')):
            editor = open_crop_editor(self.workspace)
            self.assertIn('缓存已失效', editor.status.get())
            self.assertFalse(editor.apply())
            editor.close()
        self.assertEqual(self.workspace.undo_stack, [])
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
