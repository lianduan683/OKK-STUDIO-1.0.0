"""Real preview rendering, independent playback, and dense monitor labels."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from effect_preview import EffectPreviewResult, render_effect_preview
from effects_browser import PRESETS, PRESET_BY_KEY, EffectPreset
from effects_library import OverlayAsset, TransitionMask
from exporter_core import CREATE_NO_WINDOW, ExportCancelled, probe_media
from multicam_core import CameraClip, Cut, MulticamProject
from . import test_timeline_drag_merge as fixture
from .test_transport_controls import RecordingPlayer


class EffectPreviewRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(__file__).resolve().parents[1] / 'bin/ffmpeg.exe'
        cls.ffprobe = cls.ffmpeg.with_name('ffprobe.exe')
        if not cls.ffmpeg.is_file():
            raise unittest.SkipTest('Bundled FFmpeg required')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def render(self, item, strength=1., duration=.5):
        return render_effect_preview(item, strength, duration, self.ffmpeg, self.ffprobe, self.folder)

    def pixel(self, path, position):
        import subprocess
        result = subprocess.run([str(self.ffmpeg), '-v', 'error', '-ss', str(position), '-i', path,
            '-frames:v', '1', '-vf', 'scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
            check=True, capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        return tuple(result.stdout)

    def test_every_builtin_transition_renders_a_loopable_movie(self):
        for item in PRESETS:
            if item.kind != 'transition':
                continue
            with self.subTest(item=item.key):
                result = self.render(item)
                media = probe_media(str(self.ffprobe), result.path)
                self.assertEqual((media.width, media.height), (480, 270))
                self.assertGreaterEqual(result.duration, 1.5)
                first = self.pixel(result.path, .1)
                last = self.pixel(result.path, result.duration - .1)
                self.assertGreater(sum(abs(a-b) for a, b in zip(first, last)), 30)

    def test_color_preview_reuses_cache_and_strength_changes_its_key(self):
        item = PRESET_BY_KEY['mono']
        first = self.render(item)
        stamp = Path(first.path).stat().st_mtime_ns
        second = self.render(item)
        self.assertEqual(first, second)
        self.assertEqual(Path(first.path).stat().st_mtime_ns, stamp)
        self.assertNotEqual(self.render(item, .2).path, first.path)
        color = self.pixel(first.path, .5)
        self.assertLess(max(color)-min(color), 6)

    def test_all_color_and_reset_presets_render(self):
        for item in PRESETS:
            if item.kind in ('grade', 'reset'):
                with self.subTest(item=item.key):
                    result = self.render(item)
                    self.assertGreater(result.duration, 2.9)

    def test_actual_mask_file_is_used_and_mask_replacement_invalidates_cache(self):
        mask = self.folder / 'mask.png'
        Image.linear_gradient('L').resize((96, 54)).save(mask)
        item = EffectPreset('test-mask', '测试遮罩', '遮罩', '', kind='mask',
                            asset=TransitionMask('test', '测试', '测试', 'mask.png'))
        with patch('effect_preview.transition_mask_path', return_value=str(mask)):
            first = self.render(item)
            Image.new('L', (96, 54), 100).save(mask)
            second = self.render(item)
        self.assertNotEqual(first.path, second.path)

    def test_alpha_and_screen_overlays_use_real_media(self):
        import subprocess
        baseline = self.pixel(self.render(PRESET_BY_KEY['reset']).path, .3)
        for kind, name, codec, color in (('alpha', 'alpha.mov', 'qtrle', 'red@0.5'),
                                         ('screen', 'screen.mp4', 'libx264', '0x505050')):
            path = self.folder / name
            command = [str(self.ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                       f'color=c={color}:s=96x54:r=30:d=0.5' + (',format=argb' if kind == 'alpha' else ''),
                       '-c:v', codec, str(path)]
            subprocess.run(command, check=True, capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
            asset = OverlayAsset(kind, kind, kind, 'test', name, 96, 54, 30, 15, .5, 'fixture')
            item = EffectPreset(kind, kind, '叠加', '', kind='overlay', asset=asset)
            with patch('effect_preview.asset_path', return_value=str(path)):
                result = self.render(item, .6)
            self.assertGreater(result.duration, 2.9)
            self.assertTrue(Path(result.path).is_file())
            changed = self.pixel(result.path, .3)
            if kind == 'alpha':
                self.assertGreater(changed[0], baseline[0] + 10)
            else:
                self.assertTrue(all(a > b + 5 for a, b in zip(changed, baseline)), (changed, baseline))

    def test_cancelled_or_invalid_requests_do_not_publish_movies(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(ExportCancelled):
            render_effect_preview(PRESET_BY_KEY['clean'], 1., .5, self.ffmpeg, self.ffprobe,
                                  self.folder, cancel_event=cancel)
        for strength, duration in ((float('nan'), .5), (1., float('inf')), (1., 0.), (2., .5)):
            with self.assertRaises(ValueError):
                self.render(PRESET_BY_KEY['clean'], strength, duration)
        self.assertEqual(list(self.folder.glob('*.mp4')), [])


class FakePreviewStream:
    instances = []

    def __init__(self, *args, **kwargs):
        self.done = threading.Event()
        self.error = ''
        self.closed = False
        self.picture = np.full((90, 160, 3), 120, dtype=np.uint8)
        self.instances.append(self)

    def read(self):
        picture, self.picture = self.picture, None
        return (0., picture) if picture is not None else None

    def close(self):
        self.closed = True


class EffectPreviewWorkspaceTests(unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.preview_patches = [
            patch('effects_browser.library_presets', return_value=PRESETS),
            patch('effect_preview_ui.cache_dir', return_value=self.folder),
            patch('effect_preview_ui.render_effect_preview', side_effect=lambda item, *args, **kwargs:
                  EffectPreviewResult(str(self.folder / 'demo.mp4'), 2., item.name)),
            patch('effect_preview_ui.CropVideoStream', FakePreviewStream)]
        self.preview_mocks = [item.start() for item in self.preview_patches]
        self.browser = self.workspace.show_effects_library()
        self.root.update()
        self.workspace.player = RecordingPlayer()
        FakePreviewStream.instances.clear()

    def tearDown(self):
        fixture.TimelineDropTests.tearDown(self)
        for item in reversed(self.preview_patches):
            item.stop()
        self.temporary.cleanup()

    def settle(self):
        self.root.after(100, self.root.quit)
        self.root.mainloop()
        self.assertEqual(self.errors, [])

    def test_preview_button_works_before_apply_and_without_a_selected_clip(self):
        ws, browser = self.workspace, self.browser
        before = deepcopy(ws.project)
        undo, position, zoom = len(ws.undo_stack), ws.current, ws.zoom
        browser.list.selection_set('transition_wipeleft'); browser._selection_changed()
        self.assertEqual(str(browser.preview_button['state']), 'normal')
        browser.preview_button.invoke()
        self.settle()
        window = browser._effect_preview_window
        self.assertIsNotNone(window)
        self.assertIsNotNone(window._result)
        self.assertTrue(window.canvas.find_all())
        self.assertEqual(ws.project, before)
        self.assertEqual((len(ws.undo_stack), ws.current, ws.zoom), (undo, position, zoom))
        self.assertEqual(ws.player.commands, [])
        self.assertFalse(ws.busy)
        self.assertFalse(self.root.working)

    def test_masks_and_overlays_enable_preview_but_tools_do_not(self):
        browser = self.browser
        for kind in ('mask', 'overlay'):
            item = EffectPreset('fixture-' + kind, kind, 'test', '', kind=kind)
            browser._catalog_by_key[item.key] = item
            browser.list.set_items([item])
            browser.list.selection_set(item.key); browser._selection_changed()
            self.assertEqual(str(browser.preview_button['state']), 'normal')
        browser.list.set_items([PRESET_BY_KEY['subtitle']])
        browser.list.selection_set('subtitle'); browser._selection_changed()
        self.assertEqual(str(browser.preview_button['state']), 'disabled')

    def test_pause_replay_and_close_only_control_the_preview(self):
        self.browser.preview_effect()
        self.settle()
        window = self.browser._effect_preview_window
        stream = window._stream
        window.toggle()
        self.assertTrue(stream.closed)
        self.assertTrue(window._paused)
        window.toggle()
        self.assertFalse(window._paused)
        self.assertIsNotNone(window._stream)
        window.replay()
        self.workspace._tag_transport_widgets(self.workspace)
        self.assertNotIn(self.workspace._transport_tag, window.canvas.bindtags())
        window.close()
        self.assertIsNone(self.browser._effect_preview_window)
        self.assertTrue(all(stream.closed for stream in FakePreviewStream.instances))
        self.assertEqual(self.workspace.player.commands, [])
        self.settle()

    def test_loop_restarts_and_library_hide_closes_the_preview(self):
        self.browser.preview_effect(); self.settle()
        window = self.browser._effect_preview_window
        stream = window._stream
        stream.done.set()
        self.settle()
        self.assertTrue(stream.closed)
        self.assertIsNot(window._stream, stream)
        self.browser.stop_preview(quiet=True)
        self.assertTrue(window._closed)
        self.assertIsNone(window._poll_after)

    def test_invalid_duration_does_not_start_a_preview(self):
        browser = self.browser
        browser.list.selection_set('transition_fade'); browser._selection_changed()
        browser.duration.set('nan')
        self.assertFalse(browser.preview_effect())
        self.assertIsNone(browser._effect_preview_window)
        self.preview_mocks[2].assert_not_called()

    def test_closing_a_generating_preview_cancels_only_its_worker(self):
        started, finished = threading.Event(), threading.Event()
        def generate(*args, cancel_event, **kwargs):
            started.set()
            cancel_event.wait(2)
            finished.set()
            raise ExportCancelled('cancelled')
        self.preview_mocks[2].side_effect = generate
        self.browser.preview_effect()
        self.assertTrue(started.wait(.5))
        window = self.browser._effect_preview_window
        window.close()
        self.assertTrue(finished.wait(.5))
        self.assertEqual(self.workspace.player.commands, [])
        self.settle()


class DenseCameraNameTests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown

    def test_six_column_monitor_keeps_numbers_names_and_native_handles(self):
        ws = self.workspace
        ws.project = MulticamProject(clips=[CameraClip(str(i), i, f'camera-{i}.mp4', 0., 10.)
                                           for i in range(1, 37)], cuts=[Cut(0., 1)])
        ws.project.set_camera_name(1, '正面主机位')
        ws.project.set_camera_name(26, '天涯')
        ws.project.set_camera_name(31, '右侧远景')
        ws.set_camera_grid(5, 6)
        self.root.update()
        self.assertEqual(ws.camera_buttons[1].cget('text'), '1 · 正面主机位')
        self.assertEqual(ws.camera_buttons[26].cget('text'), '26 · 天涯')
        handles = {camera: surface.winfo_id() for camera, surface in ws.camera_surfaces.items()}
        with patch('multicam_ui.simpledialog.askstring', return_value='正面新名称'):
            ws.rename_camera(1)
        self.assertIn('正面新名称', ws.camera_buttons[1].cget('text'))
        self.assertEqual(handles, {camera: surface.winfo_id() for camera, surface in ws.camera_surfaces.items()})
        ws.undo()
        self.assertIn('正面主机位', ws.camera_buttons[1].cget('text'))
        ws.redo()
        self.assertIn('正面新名称', ws.camera_buttons[1].cget('text'))
        ws._change_camera_page(1)
        self.assertEqual(ws.camera_buttons[31].cget('text'), '1:31 · 右侧远景')
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
