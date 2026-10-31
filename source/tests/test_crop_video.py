"""Current-frame-only cropping, validated proxies and a continuous video viewport."""
from pathlib import Path
import copy
import json
import shutil
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crop_editor import open_crop_editor
from crop_frame import choose_preview_source, decode_frame, _crop_media_info
from crop_geometry import transform_from_rect
from crop_video import CropVideoStream
from exporter_core import MediaInfo
from multicam_core import CameraClip, Cut, MulticamProject
from tests.test_crop_editor import CropMediaFixture
from tests import test_timeline_drag_merge as fixture


class ProxyCropTests(CropMediaFixture, unittest.TestCase):
    def test_unknown_sar_uses_square_pixels_and_rotation_is_in_display_aspect(self):
        payload = dict(streams=[dict(codec_type='video', codec_name='h264', width=720, height=480,
            avg_frame_rate='30/1', sample_aspect_ratio='N/A', duration='4', tags=dict(rotate='90'))],
            format=dict(duration='4'))
        with patch('crop_frame._run_crop_command', return_value=json.dumps(payload).encode()):
            media, aspect = _crop_media_info('ffprobe', self.source, threading.Event())
        self.assertAlmostEqual(aspect, 2/3)

    def test_proxy_must_match_full_duration_and_display_aspect(self):
        original = MediaInfo(str(self.source), duration=4, width=160, height=120, fps=30)
        proxy = MediaInfo(str(self.source_clean), duration=4, width=80, height=60, fps=15)
        with patch('crop_frame._crop_media_info', side_effect=[(original, 4/3), (proxy, 4/3)]):
            selected, note = choose_preview_source('ffprobe', self.source, self.source_clean, threading.Event())
            self.assertEqual(selected, str(self.source_clean))
            self.assertIn('代理预览', note)
        proxy.duration = 3
        with patch('crop_frame._crop_media_info', side_effect=[(original, 4/3), (proxy, 4/3)]):
            self.assertEqual(choose_preview_source('ffprobe', self.source, self.source_clean, threading.Event())[0], str(self.source))
        proxy.duration = 4
        with patch('crop_frame._crop_media_info', side_effect=[(original, 4/3), (proxy, 16/9)]):
            self.assertEqual(choose_preview_source('ffprobe', self.source, self.source_clean, threading.Event())[0], str(self.source))

    def test_continuous_decoder_has_one_frame_mailbox_real_timing_and_stops(self):
        player = CropVideoStream(self.ffmpeg, self.source, .2, .8, fps=10)
        observed = []
        started = time.monotonic()
        try:
            while time.monotonic()-started < 4:
                picture = player.read()
                if picture is not None:
                    observed.append((time.monotonic(), picture[0], picture[1]))
                    self.assertLessEqual(player.latest.qsize(), 1)
                if player.done.is_set(): break
                time.sleep(.01)
            self.assertGreaterEqual(len(observed), 5)
            self.assertGreater(observed[-1][0]-observed[0][0], .5)
            self.assertGreater(observed[-1][1]-observed[0][1], .5)
            self.assertGreaterEqual(observed[-1][0]-observed[0][0],
                                    .85*(observed[-1][1]-observed[0][1]))
            self.assertGreater(float(observed[0][2].mean()), 20)
            self.assertFalse(player.error)
        finally:
            player.close()
        self.assertFalse(player._thread.is_alive())
        self.assertIsNotNone(player._process.poll())


class CropVideoTkTests(CropMediaFixture, unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)
        self.proxy = self.folder/'matching-proxy.mkv'
        if not self.proxy.exists(): shutil.copyfile(self.source, self.proxy)
        self.workspace.project = MulticamProject(clips=[CameraClip('video', 1, str(self.source), 0, 4,
                                                    proxy_path=str(self.proxy))], cuts=[Cut(0, 1)], fps=10)
        self.root.caps.ffmpeg_path = str(self.ffmpeg)
        self.root.caps.ffprobe_path = str(self.ffmpeg.with_name('ffprobe.exe'))
        self.workspace._refresh()
        self.workspace.select_program_range(0, 4)

    tearDown = fixture.TimelineDropTests.tearDown

    def wait(self, predicate, seconds=10):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            self.root.update()
            if predicate(): return
            time.sleep(.01)
        self.fail('crop video did not become ready')

    def test_auto_reads_one_current_frame_and_repeated_auto_uses_displayed_frame(self):
        original = copy.deepcopy(self.workspace.project)
        with patch('crop_editor.decode_frame', wraps=decode_frame) as decoder:
            editor = open_crop_editor(self.workspace, automatic=True)
            self.wait(lambda: editor._pixels is not None and not editor._pending)
            self.assertEqual(decoder.call_count, 1)
            self.assertEqual(decoder.call_args.args[1], str(self.proxy))
            self.assertIn('当前帧', editor.status.get())
            self.assertIn('代理', editor.frame_label.get())
            editor.automatic()
            editor.automatic()
            self.assertEqual(decoder.call_count, 1)
            self.assertIn('当前这一帧', editor.status.get())
            self.assertAlmostEqual(transform_from_rect(editor.rect, editor.source_size).zoom, 160/130, delta=.02)
            editor.close()
        self.assertEqual(self.workspace.project, original)
        self.assertEqual(self.workspace.undo_stack, [])

    def test_play_pause_and_crop_on_current_picture_keep_one_decoder_and_geometry(self):
        with patch('crop_editor.decode_frame', wraps=decode_frame) as decoder:
            editor = open_crop_editor(self.workspace)
            self.wait(lambda: editor._pixels is not None and not editor._pending)
            editor._zoom_changed('180')
            target = transform_from_rect(editor.rect, editor.source_size)
            initial = editor.frame_time.get()
            editor.toggle_video()
            stream = editor._video_stream
            self.assertIsNotNone(stream)
            self.wait(lambda: editor.frame_time.get() > initial+.3)
            self.assertEqual(decoder.call_count, 1, 'playback must not issue repeated single-frame seeks')
            self.assertGreater(stream.frames, 2)
            self.assertAlmostEqual(transform_from_rect(editor.rect, editor.source_size).zoom, target.zoom)
            editor.toggle_video()
            self.assertIsNone(editor._video_stream)
            self.assertFalse(stream._thread.is_alive())
            stopped = editor.frame_time.get()
            for _ in range(3): self.root.update(); time.sleep(.02)
            self.assertEqual(editor.frame_time.get(), stopped)
            editor.toggle_video()
            self.wait(lambda: editor.frame_time.get() > stopped+.1)
            editor.automatic()
            self.assertIsNone(editor._video_stream)
            self.assertEqual(decoder.call_count, 1)
            self.assertIn('当前这一帧', editor.status.get())
            editor.close()
        self.assertEqual(self.errors, [])

    def test_read_error_is_visible_and_retry_recovers_in_same_window(self):
        with patch('crop_editor.decode_frame', side_effect=ValueError('测试取帧失败')):
            editor = open_crop_editor(self.workspace)
            self.wait(lambda: not editor._pending)
            self.assertIsNone(editor._pixels)
            self.assertIn('测试取帧失败', editor.status.get())
            self.assertIsNotNone(editor._poll_after)
        editor._request_frame()
        self.wait(lambda: editor._pixels is not None and not editor._pending)
        self.assertTrue(editor.canvas.find_withtag('crop'))
        editor.close()
        self.assertEqual(self.errors, [])


if __name__ == '__main__': unittest.main()
