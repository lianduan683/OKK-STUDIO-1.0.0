"""Real small-media encodes verify chosen format, rate and destination."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from configured_proxy import ProxyOptions, prepare_configured_proxy, build_proxy_command
from exporter_core import probe_media, CREATE_NO_WINDOW, ExportCancelled
from tests import test_timeline_drag_merge as fixture

FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')


class ConfiguredProxyTests(unittest.TestCase):
    @unittest.skipUnless(FFMPEG and FFPROBE, 'FFmpeg and FFprobe required')
    def test_all_formats_really_encode_selected_rate_and_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder/'中文 原片.mp4'
            subprocess.run([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=60',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '0.7',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)],
                check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            original = source.read_bytes()
            for codec, expected in [('h264', 'h264'), ('dnxhr_lb', 'dnxhd'), ('prores_proxy', 'prores')]:
                with self.subTest(codec=codec):
                    target = folder/'代理 文件'/codec
                    options = ProxyOptions(str(target), codec, 960, 540, 30)
                    with patch('configured_proxy.register_proxy'):
                        output = prepare_configured_proxy(str(source), FFMPEG, FFPROBE, SimpleNamespace(), options)
                        result = probe_media(FFPROBE, output)
                        self.assertEqual(Path(output).parent, target)
                        self.assertEqual(result.video_codec, expected)
                        self.assertAlmostEqual(result.fps, 30., delta=.01)
                        self.assertTrue(result.has_audio)
                        self.assertAlmostEqual(result.duration, .7, delta=.08)
                        self.assertEqual(source.read_bytes(), original)
                        with patch('configured_proxy._run_encode') as encode:
                            self.assertEqual(prepare_configured_proxy(str(source), FFMPEG, FFPROBE,
                                SimpleNamespace(), options), output)
                            encode.assert_not_called()
                        self.assertFalse(list(target.glob('*.pending.*')))

    def test_frame_rate_and_resolution_reach_each_encoder_command(self):
        media = SimpleNamespace(fps=60., duration=10., width=3840, height=2160)
        for codec in ('h264', 'dnxhr_lb', 'prores_proxy'):
            command = build_proxy_command('ffmpeg', 'source.mov', 'proxy.mov', media,
                                          ProxyOptions('folder', codec, 1280, 720, 0))
            graph = command[command.index('-vf')+1]
            self.assertIn('fps=fps=60/1', graph)
            self.assertIn('1280', graph)
            self.assertIn('720', graph)

    def test_cancel_before_start_does_not_create_output_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'output'
            cancel = threading.Event(); cancel.set()
            with self.assertRaises(ExportCancelled):
                prepare_configured_proxy('source.mov', 'ffmpeg', 'ffprobe', SimpleNamespace(),
                                         ProxyOptions(str(target)), cancel_event=cancel)
            self.assertFalse(target.exists())

    def test_cancelled_encode_cleans_partial_file_and_never_registers_proxy(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'source.mov'; source.write_bytes(b'original')
            target = Path(folder)/'output'
            media = SimpleNamespace(fps=30., duration=10., width=1920, height=1080)
            with patch('configured_proxy._probe_media_cancel', return_value=media), \
                 patch('configured_proxy._run_encode', side_effect=ExportCancelled('cancelled')), \
                 patch('configured_proxy.register_proxy') as register:
                with self.assertRaises(ExportCancelled):
                    prepare_configured_proxy(str(source), 'ffmpeg', 'ffprobe', SimpleNamespace(),
                                             ProxyOptions(str(target), 'dnxhr_lb'))
            register.assert_not_called()
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(source.read_bytes(), b'original')


class ProxySettingsUITests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown

    def test_each_file_has_icon_and_icon_changes_when_proxy_is_missing(self):
        w = self.workspace
        w._refresh_proxy_badges(force=True)
        before = w.media_tree.item('one', 'image')
        self.assertTrue(before)
        with tempfile.TemporaryDirectory() as folder:
            proxy = Path(folder)/'one.mp4'; proxy.write_bytes(b'fixture')
            w.project.clips[0].proxy_path = str(proxy)
            w.proxy_var.set(False)
            w._refresh_proxy_badges(force=True)
            ready = w.media_tree.item('one', 'image')
            self.assertNotEqual(ready, before)
            proxy.unlink()
            w._refresh_proxy_badges(force=True)
            self.assertNotEqual(w.media_tree.item('one', 'image'), ready)

    def test_right_click_file_opens_settings_and_selected_values_start_batch(self):
        w = self.workspace
        w.media_tree.item('camera_1', open=True)
        self.root.update()
        bounds = w.media_tree.bbox('one')
        event = SimpleNamespace(x=15, y=bounds[1]+bounds[3]/2, x_root=0, y_root=0)
        with patch('tkinter.Menu.tk_popup'): self.assertEqual(w._camera_context(event), 'break')
        menu = w.camera_context_menu
        entry = next(index for index in range(menu.index('end')+1)
                     if menu.entrycget(index, 'label') == '转代理…')
        menu.invoke(entry)
        self.assertTrue(w._proxy_settings_window.winfo_exists())
        values = w._proxy_settings_vars
        with tempfile.TemporaryDirectory() as folder:
            values['format'].set('DNxHR LB / MOV')
            values['size'].set('1280 × 720')
            values['fps'].set('保留原始帧率')
            values['folder'].set(folder)
            with patch.object(w, 'start_quick_proxy', return_value=True) as start:
                w._proxy_settings_start()
            arguments = start.call_args.kwargs
            self.assertEqual([clip.id for clip in arguments['clips']], ['one'])
            self.assertEqual(arguments['options'], ProxyOptions(folder, 'dnxhr_lb', 1280, 720, 0))
        self.assertEqual(self.errors, [])
