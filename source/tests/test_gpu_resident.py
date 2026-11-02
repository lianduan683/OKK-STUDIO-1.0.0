"""Failure handling and coordination for the real GPU interop capability gate."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import gpu_resident as resident


class ResidentProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.binary = Path(self.temp.name) / 'ffmpeg.exe'
        self.binary.write_bytes(b'fixture')
        resident._cache.clear()

    def tearDown(self):
        self.temp.cleanup()

    def success(self):
        packets = '\n'.join(f'{stream}, {n}, {n}, 1, 10, 0x1234'
                            for stream in (0, 1) for n in range(32)).encode('ascii')
        return subprocess.CompletedProcess([], 0, packets, b'')

    def test_concurrent_callers_only_probe_once(self):
        def slow_probe(*args, **kwargs):
            time.sleep(.02)
            return self.success()
        with patch.object(resident.subprocess, 'run', side_effect=slow_probe) as run:
            with ThreadPoolExecutor(max_workers=5) as pool:
                self.assertTrue(all(pool.map(lambda _: resident.supported(self.binary), range(10))))
        self.assertEqual(run.call_count, 1)

    def test_failed_or_partial_output_does_not_enable_fast_path(self):
        results = [subprocess.CompletedProcess([], 1, b'', b'bad GPU'),
                   subprocess.CompletedProcess([], 0, b'0, 0, 0, 1, 10, 0x1\n', b'')]
        for result in results:
            resident._cache.clear()
            with patch.object(resident.subprocess, 'run', return_value=result):
                self.assertFalse(resident.supported(self.binary))
                self.assertTrue(resident.report(self.binary).reason)

    def test_timeout_is_safe_and_cached(self):
        with patch.object(resident.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffmpeg', 12)) as run:
            self.assertFalse(resident.supported(self.binary))
            self.assertFalse(resident.supported(self.binary))
        self.assertEqual(run.call_count, 1)

    def test_binary_change_and_gpu_selection_invalidate_cached_result(self):
        with patch.object(resident.subprocess, 'run', return_value=self.success()) as run:
            self.assertTrue(resident.supported(self.binary, 0))
            self.assertTrue(resident.supported(self.binary, 1))
            self.binary.write_bytes(b'changed-runtime')
            self.assertTrue(resident.supported(self.binary, 0))
        self.assertEqual(run.call_count, 3)

    def test_device_graph_uses_selected_cuda_ancestor(self):
        with patch.object(resident.subprocess, 'run', return_value=self.success()) as run:
            self.assertTrue(resident.supported(self.binary, 2))
        command = run.call_args.args[0]
        self.assertIn('cuda=okk_cu:2', command)
        self.assertIn('vulkan=okk_vk@okk_cu', command)
        graph = command[command.index('-filter_complex') + 1]
        self.assertNotIn('hwdownload', graph)
        self.assertIn('format=nv12', graph)
        self.assertIn('format=p010le', graph)
        self.assertEqual(resident.input_options()[3], 'okk_cu')

    def test_unavailable_runtime_or_bad_index_returns_false(self):
        self.assertFalse(resident.supported(self.binary.with_name('missing.exe')))
        self.assertFalse(resident.supported(self.binary, -1))
        self.assertFalse(resident.supported(self.binary, True))


if __name__ == '__main__':
    unittest.main()
