"""Real PCM waveform extraction plus pixel-resolution envelope regressions."""
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import wave

import numpy as np

from audio_waveform import Waveform, decode_waveform, envelope_points, waveform_key
from exporter_core import CREATE_NO_WINDOW, find_binary


FFMPEG = Path(find_binary('ffmpeg', Path(__file__).resolve().parents[1]) or 'ffmpeg-not-installed')


class WaveformEnvelopeTests(unittest.TestCase):
    def test_pixel_aggregation_preserves_brief_peaks(self):
        peaks = np.zeros(1000, dtype=np.float32)
        peaks[437] = .9
        result = envelope_points(Waveform(peaks, rate=100.), 0., 10., 10)
        self.assertEqual(len(result), 10)
        self.assertAlmostEqual(float(result[4]), .9, places=6)
        self.assertEqual(int(np.count_nonzero(result)), 1)

    def test_zoomed_range_uses_source_time_and_zeros_beyond_end(self):
        result = envelope_points(Waveform(np.array([.2,.4,.6,.8]), rate=1.), 2., 6., 4)
        np.testing.assert_allclose(result, [.6,.8,0,0])

    def test_file_signature_invalidates_replaced_media(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'recording.wav'
            path.write_bytes(b'old')
            old = waveform_key(path)
            path.write_bytes(b'new audio content')
            self.assertNotEqual(waveform_key(path), old)
            self.assertIsNone(waveform_key(path.with_name('missing.wav')))

    def test_precancel_does_not_start_decoder(self):
        cancel = threading.Event(); cancel.set()
        self.assertIsNone(decode_waveform('missing.exe', 'missing.wav', cancel, use_cache=False))


@unittest.skipUnless(FFMPEG.is_file(), 'FFmpeg is required for real audio envelope checks')
class RealWaveformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='audio_waveform_')
        self.folder = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def audio(self, name, samples):
        path = self.folder/name
        samples = np.asarray(samples, dtype='<i2')
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1 if samples.ndim == 1 else samples.shape[1], 2, 8000, 0, 'NONE', 'not compressed'))
            stream.writeframes(samples.tobytes())
        return path

    def test_sine_and_real_silence_have_distinct_actual_envelopes(self):
        samples = (16000*np.sin(2*np.pi*440*np.arange(8000)/8000)).astype('<i2')
        audible = decode_waveform(FFMPEG, self.audio('tone.wav', samples), threading.Event(), use_cache=False)
        silent = decode_waveform(FFMPEG, self.audio('silence.wav', np.zeros(8000)), threading.Event(), use_cache=False)
        self.assertEqual(audible.state, 'ready')
        self.assertEqual(silent.state, 'ready')
        self.assertAlmostEqual(audible.duration, 1., places=3)
        # FFmpeg's mono-to-stereo conversion uses equal-power (-3 dB) gain.
        self.assertGreater(float(np.max(audible.peaks)), .3)
        self.assertEqual(float(np.max(silent.peaks)), 0.)

    def test_inverse_stereo_channels_do_not_cancel_waveform(self):
        samples = (14000*np.sin(2*np.pi*440*np.arange(8000)/8000)).astype('<i2')
        stereo = np.column_stack([samples,-samples])
        value = decode_waveform(FFMPEG, self.audio('antiphase.wav', stereo), threading.Event(), use_cache=False)
        self.assertEqual(value.state, 'ready')
        self.assertGreater(float(np.max(value.peaks)), .4)

    def test_video_with_no_audio_stream_does_not_show_fake_waveform(self):
        path = self.folder/'silent-video.mp4'
        subprocess.run([str(FFMPEG), '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'color=blue:size=32x32:rate=5:duration=1', '-an', '-c:v', 'libx264', str(path)],
            check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
        value = decode_waveform(FFMPEG, path, threading.Event(), use_cache=False)
        self.assertEqual(value.state, 'no_audio')
        self.assertEqual(len(value.peaks), 0)

    def test_cancel_terminates_running_decoder(self):
        cancel = threading.Event()
        processes = []
        def started(process):
            processes.append(process)
            cancel.set()
        path = self.audio('tone.wav', np.zeros(8000))
        self.assertIsNone(decode_waveform(FFMPEG, path, cancel, process_started=started, use_cache=False))
        self.assertIsNotNone(processes[0].poll())


if __name__ == '__main__': unittest.main()
