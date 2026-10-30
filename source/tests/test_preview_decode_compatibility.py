"""Editing codecs keep their valid proxies and skip impossible hardware loads."""
from pathlib import Path
import queue
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import multicam_player as player_module
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_player import MulticamPlayer
from preview_decode import _read_codec, software_preview_codec
from .test_multicam_player import _Trace, _controller_type


class CodecProbeTests(unittest.TestCase):
    def setUp(self):
        _read_codec.cache_clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.mpv = self.folder/'mpv.exe'
        (self.folder/'ffprobe.exe').touch()
        self.media = self.folder/'proxy.mov'
        self.media.write_bytes(b'media')

    def result(self, codec):
        return subprocess.CompletedProcess([], 0, ('{"streams":[{"codec_name":"'+codec+'"}]}').encode())

    def test_dnxhr_and_prores_require_compatible_decode(self):
        for codec in ('dnxhd', 'prores'):
            with self.subTest(codec=codec), patch('preview_decode.subprocess.run', return_value=self.result(codec)):
                _read_codec.cache_clear()
                self.assertEqual(software_preview_codec(self.mpv, self.media), codec)

    def test_h264_in_mov_keeps_native_gpu_path(self):
        with patch('preview_decode.subprocess.run', return_value=self.result('h264')):
            self.assertEqual(software_preview_codec(self.mpv, self.media), '')

    def test_probe_cache_rechecks_file_replacement(self):
        with patch('preview_decode.subprocess.run', side_effect=[self.result('dnxhd'), self.result('h264')]) as run:
            self.assertEqual(software_preview_codec(self.mpv, self.media), 'dnxhd')
            self.assertEqual(software_preview_codec(self.mpv, self.media), 'dnxhd')
            self.assertEqual(run.call_count, 1)
            self.media.write_bytes(b'replaced media')
            self.assertEqual(software_preview_codec(self.mpv, self.media), '')
            self.assertEqual(run.call_count, 2)


class EditingProxyPlaybackTests(unittest.TestCase):
    def test_dnxhr_proxy_remains_attached_to_programme_and_thumbnail(self):
        self.check_proxy_playback(late=False)

    def test_later_proxy_attachment_with_native_gpu_off_rechecks_decode_support(self):
        self.check_proxy_playback(late=True)

    def check_proxy_playback(self, late):
        trace = _Trace()
        base = _controller_type(trace)
        class Controller(base):
            def load(self, path):
                if path.endswith('.mov') and not self.allow_software_decode:
                    raise RuntimeError('DNxHR does not use D3D11VA/NVDEC')
                return super().load(path)
        with tempfile.TemporaryDirectory() as folder:
            proxy = Path(folder)/'proxy.mov'
            proxy.write_bytes(b'valid proxy fixture')
            def codec(executable, path):
                return 'dnxhd' if str(path).endswith('.mov') else ''
            events = queue.Queue()
            received = []
            with patch.object(player_module, 'MpvController', Controller), \
                 patch.object(player_module, '_AudioController', Controller), \
                 patch('preview_decode.software_preview_codec', side_effect=codec), \
                 patch('gpu_live_controller.GpuLiveSession') as session:
                player = MulticamPlayer('mpv.exe', {1:101}, 200, 300, events,
                                        gpu_live=not late, gpu_output_window=201)
                try:
                    project = MulticamProject([CameraClip('a',1,'original.mp4',0.,8.,
                                                          proxy_path='' if late else str(proxy))], [Cut(0.,1)])
                    player.submit('configure', project=project, use_proxies=True)
                    if late:
                        deadline = time.monotonic()+3.
                        while time.monotonic()<deadline and not player._program.active:
                            time.sleep(.005)
                        self.assertTrue(player._program.active)
                        self.assertFalse(player._program.controller.allow_software_decode)
                        player.submit('proxy_update', bindings=[{'id':'a','source':'original.mp4',
                                                                'proxy':str(proxy)}], use_proxies=True)
                    deadline = time.monotonic()+3.
                    while time.monotonic()<deadline:
                        while not events.empty(): received.append(events.get_nowait())
                        slot = player._slots.get(1)
                        programme = player._program
                        if slot and slot.active and programme.active and slot.controller.path == str(proxy):
                            break
                        time.sleep(.005)
                    self.assertTrue(player._slots[1].active, received)
                    self.assertTrue(player._program.active, received)
                    self.assertEqual(player._slots[1].controller.path, str(proxy))
                    self.assertEqual(player._program.controller.path, str(proxy))
                    self.assertFalse(player._proxy_resolver._rejected)
                    self.assertTrue(player._slots[1].controller.allow_software_decode)
                    self.assertTrue(player._program.controller.allow_software_decode)
                    self.assertFalse(player._gpu_live_enabled)
                    session.assert_not_called()
                    self.assertFalse([item for item in received if item[0]=='error'])
                finally:
                    player.close()
                    self.assertTrue(player.wait_closed(3.))


if __name__ == '__main__':
    unittest.main(verbosity=2)
