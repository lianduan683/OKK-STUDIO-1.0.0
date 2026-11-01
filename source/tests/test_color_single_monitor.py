"""The colour viewer must not leave a hidden multicamera grid decoding."""
from pathlib import Path
import tempfile
import time
import unittest

from . import test_multicam_player as playback
from . import test_gpu_live_player as gpu
from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject


class ColorSingleMonitorTests(unittest.TestCase):
    setUp = playback.MulticamPlayerTests.setUp
    tearDown = playback.MulticamPlayerTests.tearDown
    _wait = playback.MulticamPlayerTests._wait
    _state = playback.MulticamPlayerTests._state
    _configure = playback.MulticamPlayerTests._configure

    def test_enter_colour_unloads_nine_grid_streams_and_return_restores_proxies(self):
        with tempfile.TemporaryDirectory() as directory:
            clips = []
            for i in range(1, 10):
                proxy = Path(directory) / f'proxy-{i}.mp4'
                proxy.write_bytes(b'proxy')
                clips.append(CameraClip(str(i), i, f'original-{i}.mp4', 0, 12, proxy_path=str(proxy)))
            project = MulticamProject(clips, [Cut(0, 1), Cut(6, 2)], master_audio='master.wav', master_duration=12)
            windows = {i: 100+i for i in range(1, 10)}
            self.player.submit('camera_windows', windows=windows)
            self._configure(project, use_proxies=True)
            self._wait(lambda: len(self.player._slots) == 9 and all(s.active for s in self.player._slots.values()))
            mapping = dict(self.player.camera_windows)
            self.player.submit('play')
            self._state(lambda state: state['playing'] and state['time'] > .03)
            self.player.submit('color_monitoring', enabled=True)
            self.player.submit('original_cameras', cameras=(1, 2))
            self.player.submit('audition', camera=1)
            self._wait(lambda: self.player._color_monitoring and
                all(not s.active and not s.controller.path for s in self.player._slots.values()) and
                self.player._program.controller.path == 'original-1.mp4')
            self.assertTrue(self.player._playing)
            trace_start = len(self.trace.snapshot())
            self.player.submit('seek', time=3.)
            self.player.submit('audition', camera=2)
            self.player.submit('grade_preview', target=('camera', 2), grade=ColorGrade(exposure=.2))
            self.player.submit('proxy_update', bindings=[], use_proxies=True)
            self.player.submit('camera_windows', windows=windows)
            self._wait(lambda: self.player._program.controller.path == 'original-2.mp4' and
                self.player._grade_draft is not None)
            self._state(lambda state: state['time'] > 3.05)
            changes = self.trace.snapshot()[trace_start:]
            self.assertFalse(any(window in windows.values() and
                (kind == 'load' or kind == 'command' and value[0] in ('seek', 'loadfile'))
                for kind, window, value, _ in changes))
            self.assertEqual(self.player.camera_windows, mapping)
            self.player.submit('color_monitoring', enabled=False)
            self.player.submit('original_cameras', cameras=())
            self.player.submit('audition', camera=None)
            self._wait(lambda: all(s.active and s.controller.path == project.clips[i-1].proxy_path
                for i, s in self.player._slots.items()))
            self.assertTrue(self.player._use_proxies)
            self.assertEqual(self.player.camera_windows, mapping)

    def test_starting_in_colour_never_opens_grid_streams_even_after_reconfigure(self):
        project = MulticamProject([CameraClip(str(i), i, f'original-{i}.mp4', 0, 12)
            for i in (1, 2)], [Cut(0, 1)])
        self._configure(project, use_proxies=True, color_monitoring=True, original_cameras=(1, 2))
        self._wait(lambda: self.player._program.active)
        self.player.submit('play')
        self._state(lambda state: state['time'] > .05)
        self._configure(project, use_proxies=True, color_monitoring=True, original_cameras=(1, 2))
        self._state(lambda state: state['time'] > .1)
        self.assertTrue(all(s.controller is None for s in self.player._slots.values()))
        self.assertFalse(any(kind == 'load' and window in (101, 102)
            for kind, window, _, _ in self.trace.snapshot()))


class GpuColorSingleMonitorTests(unittest.TestCase):
    setUp = gpu.GpuLiveWorkerTests.setUp
    tearDown = gpu.GpuLiveWorkerTests.tearDown
    wait = gpu.GpuLiveWorkerTests.wait
    drain = gpu.GpuLiveWorkerTests.drain

    def test_gpu_colour_keeps_one_current_picture_and_pauses_hidden_buffer(self):
        self.player.submit('color_monitoring', enabled=True)
        self.player.submit('audition', camera=1)
        self.player.submit('play')
        self.wait(lambda: self.player._playing and self.player._position > .1 and
            self.player._gpu_context is None and self.player._program.active)
        self.assertTrue(all(not s.active for s in self.player._slots.values()))
        staging = self.player._program_standby
        backend = self.backends[0]
        self.assertTrue(backend.controllers[staging.controller.slot].paused)
        loads = len([item for item in self.trace.snapshot() if item[0] == 'load'])
        self.wait(lambda: self.player._position > .4)
        self.assertEqual(len([item for item in self.trace.snapshot() if item[0] == 'load']), loads)


if __name__ == '__main__':
    unittest.main()
