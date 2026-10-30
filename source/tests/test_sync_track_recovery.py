"""Real recordings and track-level contracts for robust camera alignment."""
from pathlib import Path
import copy
import math
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

import audio_sync
import audio_sync_fallback
import reference_sync
from editor_core import AlignmentResult
from exporter_core import find_binary
from multicam_core import CameraClip


class SyncRecoveryMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = find_binary('ffmpeg', Path(__file__).resolve().parents[1])
        if not cls.ffmpeg:
            raise unittest.SkipTest('FFmpeg unavailable')
        cls.temp = tempfile.TemporaryDirectory(prefix='sync_recovery_')
        cls.root = Path(cls.temp.name)
        rate = cls.rate = 4000
        rng = np.random.default_rng(101728)
        samples = 360 * rate
        level = np.repeat(rng.uniform(.03, 1, math.ceil(samples / 320)) ** 1.5, 320)[:samples]
        t = np.arange(samples) / rate
        signal = np.rint(10000 * level * (np.sin(2 * np.pi * 211 * t)
                         + .3 * np.sin(2 * np.pi * 337 * t))).astype('<i2')
        cls.master = cls.root / 'master.wav'
        cls.write(cls.master, signal[20 * rate:340 * rate])
        camera = signal[55 * rate:315 * rate]
        cls.opposite = cls.root / 'camera opposite stereo.wav'
        cls.write(cls.opposite, np.stack((camera, -camera), axis=1))
        cls.quiet = cls.root / 'camera quiet middle.wav'
        camera = camera.copy()
        camera[60 * rate:200 * rate] = 0
        cls.write(cls.quiet, camera)
        cls.parts = []
        for index in range(3):
            path = cls.root / f'part {index + 1}.wav'
            samples = signal[(55 + index * 50) * rate:(105 + index * 50) * rate].copy()
            if index == 1:
                samples[:] = 0
            cls.write(path, samples)
            cls.parts.append(CameraClip(str(index), 1, str(path), index * 50, 50))
        cls.delayed_master = cls.root / 'master video with delayed audio.mkv'
        subprocess.run([cls.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=s=16x16:r=1:d=323', '-itsoffset', '3', '-i', str(cls.master),
                        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'mpeg4', '-q:v', '31',
                        '-c:a', 'pcm_s16le', str(cls.delayed_master)],
                       check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def write(cls, path, data):
        with wave.open(str(path), 'wb') as out:
            out.setparams((1 if data.ndim == 1 else data.shape[1], 2, cls.rate, 0, 'NONE', ''))
            out.writeframes(np.asarray(data, dtype='<i2').tobytes())

    def align(self, source):
        return audio_sync.align_audio_segments_fast(
            self.ffmpeg, [str(source)], str(self.master), cache_dir=self.root / self._testMethodName,
            continue_on_error=True)[0]

    def test_opposite_stereo_channels_do_not_cancel_the_sync_signal(self):
        result = self.align(self.opposite)
        self.assertGreaterEqual(result.confidence, .5, result.engine)
        self.assertAlmostEqual(result.offset_seconds, -35, delta=.06)

    def test_quiet_middle_recovers_from_bounded_edge_window(self):
        with patch('audio_sync._stream_energy', wraps=audio_sync._stream_energy) as decode:
            result = self.align(self.quiet)
        self.assertGreaterEqual(result.confidence, .5, result.engine)
        self.assertAlmostEqual(result.offset_seconds, -35, delta=.06)
        reads = [call for call in decode.call_args_list if Path(call.args[1]) == self.quiet]
        self.assertLessEqual(len(reads), 7)
        self.assertTrue(all(0 < call.kwargs['duration'] <= 45 for call in reads))

    def test_video_reference_retains_its_leading_audio_timestamp(self):
        # The same master waveform now begins at video time 3, not at 0.
        # Reading raw PCM without timestamp padding used to give a confident
        # answer exactly three seconds early.
        for mode in ('fast', 'full'):
            result = audio_sync.align_audio_segments_fast(
                self.ffmpeg, [str(self.opposite)], str(self.delayed_master),
                cache_dir=self.root / self._testMethodName, mode=mode)[0]
            self.assertGreaterEqual(result.confidence, .5)
            self.assertAlmostEqual(result.offset_seconds, -38, delta=.06)

    def test_python_fallback_retains_stereo_signal_and_reference_timestamps(self):
        result = audio_sync_fallback.align_without_numpy(
            self.ffmpeg, [str(self.opposite)], str(self.delayed_master), envelope_rate=20)[0]
        self.assertGreaterEqual(result.confidence, .5)
        self.assertAlmostEqual(result.offset_seconds, -38, delta=.1)

    def test_real_sequential_parts_keep_continuity_across_a_silent_recording(self):
        results = reference_sync.align_camera_tracks(
            self.ffmpeg, self.parts, str(self.master), cache_dir=self.root / self._testMethodName)
        self.assertTrue(all(result.confidence >= .2 for result in results))
        for result, expected in zip(results, [35, 85, 135]):
            self.assertAlmostEqual(-result.offset_seconds, expected, delta=.06)


class CameraTrackAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.clips = [CameraClip(str(i), 1, f'{i}.mp4', i * 100, 100) for i in range(3)]

    def align(self, matches, clips=None):
        clips = clips or self.clips
        original = copy.deepcopy(clips)
        with patch('reference_sync.align_audio_segments', return_value=matches):
            result = reference_sync.align_camera_tracks('ffmpeg', clips, 'master.wav')
        self.assertEqual(clips, original, 'analysis must not mutate the project')
        return result

    def test_agreeing_segments_translate_the_complete_track_with_no_gaps(self):
        matches = [AlignmentResult(-(12 + clip.start + drift), .9, 50)
                   for clip, drift in zip(self.clips, [0, .02, .04])]
        results = self.align(matches)
        starts = [-item.offset_seconds for item in results]
        self.assertAlmostEqual(starts[1] - starts[0], 100)
        self.assertAlmostEqual(starts[2] - starts[1], 100)
        self.assertTrue(all(item.confidence >= .2 for item in results))

    def test_two_independent_strong_segments_anchor_the_silent_middle(self):
        results = self.align([AlignmentResult(-12, .9, 50), AlignmentResult(0, 0, 50),
                              AlignmentResult(-212, .95, 50)])
        self.assertEqual([-item.offset_seconds for item in results], [12, 112, 212])
        self.assertTrue(all(item.confidence >= .2 for item in results))
        self.assertIn('共同平移', results[1].engine)

    def test_one_match_cannot_silently_reposition_unmatched_segments(self):
        results = self.align([AlignmentResult(-12, .9, 50), AlignmentResult(0, 0, 50),
                              AlignmentResult(0, 0, 50)])
        self.assertEqual([-item.offset_seconds for item in results], [0, 100, 200])
        self.assertTrue(all(item.confidence == 0 for item in results))

    def test_conflicting_matches_preserve_the_entire_track(self):
        results = self.align([AlignmentResult(-12, .9, 50), AlignmentResult(-119, .9, 50),
                              AlignmentResult(-212, .95, 50)])
        self.assertEqual([-item.offset_seconds for item in results], [0, 100, 200])
        self.assertTrue(all(item.confidence == 0 for item in results))
        self.assertIn('不一致', results[0].engine)

    def test_disconnected_clips_and_different_cameras_keep_independent_offsets(self):
        clips = [CameraClip('a', 1, 'a.mp4', 0, 10), CameraClip('b', 1, 'b.mp4', 20, 10),
                 CameraClip('c', 2, 'c.mp4', 0, 10)]
        results = self.align([AlignmentResult(-12, .9, 50), AlignmentResult(-37, .95, 50),
                              AlignmentResult(-5, .9, 50)], clips)
        self.assertEqual([-item.offset_seconds for item in results], [12, 37, 5])

    def test_same_source_duplicated_twice_is_not_independent_evidence(self):
        self.clips[2].path = self.clips[0].path
        results = self.align([AlignmentResult(-12, .9, 50), AlignmentResult(0, 0, 50),
                              AlignmentResult(-212, .95, 50)])
        self.assertTrue(all(item.confidence == 0 for item in results))


if __name__ == '__main__':
    unittest.main()
