"""First-frame work is limited to visible sources, with alpha parity."""
from dataclasses import replace
from pathlib import Path
import queue
import threading
import time
import unittest
from unittest.mock import patch

from exporter_core import probe_media
from multicam_core import CameraClip
from video_stack_preview import StackPreview
from video_stack_render import StackLayer, StackSegment
from tests import test_video_stack_render as fixtures


class StackStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.StackMediaTests.setUpClass()
        cls.fixture = fixtures.StackMediaTests()

    @classmethod
    def tearDownClass(cls):
        # Windows may release FFmpeg's terminated input handles just after
        # wait(). All preview owners have already closed and reaped children.
        deadline = time.monotonic()+2.
        while True:
            try:
                fixtures.StackMediaTests.tearDownClass()
                break
            except PermissionError:
                if time.monotonic() >= deadline: raise
                time.sleep(.05)

    def frame(self, project):
        fixture = self.fixture
        events = queue.Queue()
        preview = StackPreview(fixture.ffmpeg, fixture.ffprobe,
            lambda *value: events.put(value), width=128, height=72)
        probes = []
        original = preview._probe
        def probe(path, cancel):
            probes.append(path)
            return original(path, cancel)
        preview._probe = probe
        try:
            preview.request(project, 0., False, 1, use_proxies=False)
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                frame = preview.take_frame()
                if frame is not None: return frame, probes
                time.sleep(.005)
            self.fail(f'No first frame: {list(events.queue)}')
        finally: preview.close()

    def test_31_opaque_layers_and_future_sources_do_not_all_get_probed(self):
        fixture = self.fixture
        opaque = fixture.folder/'opaque.mov'
        fixture.command([fixture.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=red:s=128x72:r=10:d=2', '-c:v', 'libx264', '-threads', '1', str(opaque)])
        project = fixture.project()
        project.clips = [CameraClip(str(i), i, str(opaque) if i == 1 else str(fixture.folder/f'hidden-{i}.mov'),
                                    0., 2.) for i in range(1, 32)]
        project.clips.append(CameraClip('future', 1, str(fixture.folder/'future.mov'), 3., 1.))
        project.video_stack_order = list(range(1, 32))
        project.master_duration = 4.
        frame, probes = self.frame(project)
        self.assertEqual(probes, [str(opaque)])
        self.assertGreater(frame['rgba'][4*(36*128+64)], 240)

    def test_alpha_and_letterbox_keep_background_and_match_full_composition(self):
        from video_stack_preview import preview_command, preview_segments
        fixture = self.fixture
        for foreground in (fixture.top, fixture.portrait):
            project = fixture.project()
            project.clips[0].path = str(foreground)
            # Use a wide canvas even when the first source is portrait.
            project.project_settings = {'width': 128, 'height': 72, 'sample_rate': 48000, 'bit_depth': '24bit'}
            frame, probes = self.frame(project)
            media = {clip.path: probe_media(fixture.ffprobe, clip.path) for clip in project.clips}
            command, _ = preview_command(next(preview_segments(project, 0., single_frame=True)),
                project, fixture.ffmpeg, media, 128, 72, single_frame=True)
            expected = fixture.command(command)
            self.assertEqual(frame['rgba'], expected)
            self.assertIn(str(fixture.blue), probes)

    def test_current_frame_is_published_before_future_metadata_is_probed(self):
        fixture = self.fixture
        entered, release = threading.Event(), threading.Event()
        preview = StackPreview(fixture.ffmpeg, fixture.ffprobe, lambda *args: None, width=128, height=72)
        project = fixture.project()
        segments = [StackSegment(0., 1., 10, (StackLayer(1, str(fixture.blue), str(fixture.blue), 0., 0.),)),
                    StackSegment(1., 1., 10, (StackLayer(1, 'slow-future.mov', 'slow-future.mov', 0., 1.),))]
        original = preview._probe
        def probe(path, cancel):
            if path == 'slow-future.mov':
                entered.set()
                release.wait(4.)
                preview._check(cancel)
            return original(path, cancel)
        preview._probe = probe
        try:
            with patch('video_stack_preview.preview_segments', return_value=iter(segments)):
                preview.request(project, 0., True, 1, use_proxies=False)
                self.assertTrue(entered.wait(4.))
                frame = preview.take_frame()
                self.assertIsNotNone(frame)
                self.assertEqual(frame['time'], 0.)
                preview.cancel()
                release.set()
        finally:
            release.set()
            preview.close()
