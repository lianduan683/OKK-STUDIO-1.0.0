"""Track selection persistence and sample-based audio mixing regressions."""
from __future__ import annotations

import array
import copy
import math
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from exporter_core import Capabilities, ExportSettings, find_binary
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from timeline_audio import (audio_sources, audio_track_enabled, build_audio_mix_args,
                            probe_audio_sources, render_audio_mix)


class TimelineAudioModelTests(unittest.TestCase):
    def project(self):
        return MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', 0., 2.),
                                     CameraClip('b', 2, 'b.mp4', .5, 1.)],
                               master_audio='master.wav', master_duration=2., cuts=[Cut(0., 1)])

    def test_legacy_default_and_explicit_mute_all(self):
        project = self.project()
        self.assertTrue(audio_track_enabled(project, 0))
        self.assertFalse(audio_track_enabled(project, 1))
        project.master_audio = ''
        self.assertTrue(audio_track_enabled(project, 1))
        self.assertFalse(audio_track_enabled(project, 2))
        project.audio_track_enabled = {0: False, 1: False, 2: False}
        self.assertEqual(audio_sources(project), [])

    def test_dict_projects_supported_for_player(self):
        project = {'master_audio': '', 'clips': [{'camera': 2, 'has_audio': False},
                    {'camera': 3, 'has_audio': True}], 'audio_track_enabled': {}}
        self.assertFalse(audio_track_enabled(project, 2))
        self.assertTrue(audio_track_enabled(project, 3))
        project['audio_track_enabled'] = {'3': False}
        self.assertFalse(audio_track_enabled(project, 3))

    def test_save_load_preserves_track_choices_markers_and_media_metadata(self):
        project = self.project()
        project.audio_track_enabled = {0: False, 1: True, 2: False}
        project.timeline_markers = [0., .75, 4.]
        project.camera_names = {1: '主机位'}
        project.clips[0].has_audio = True
        project.clips[1].has_audio = False
        payload = project.to_payload('.')
        self.assertEqual(payload['version'], 11)
        self.assertEqual(payload['audio_track_enabled'], {'0': False, '1': True, '2': False})
        restored = MulticamProject.from_payload(payload, '.')
        self.assertEqual(restored.audio_track_enabled, project.audio_track_enabled)
        self.assertEqual(restored.timeline_markers, project.timeline_markers)
        self.assertEqual(restored.camera_names, project.camera_names)
        self.assertEqual([clip.has_audio for clip in restored.clips], [True, False])

    def test_old_project_does_not_gain_new_fields_or_format(self):
        project = self.project()
        payload = project.to_payload('.')
        self.assertEqual(payload['version'], 8)
        self.assertNotIn('audio_track_enabled', payload)
        self.assertNotIn('timeline_markers', payload)
        self.assertNotIn('has_audio', payload['clips'][0])
        restored = MulticamProject.from_payload(payload, '.')
        self.assertIsNone(restored.clips[0].has_audio)

    def test_invalid_track_and_marker_payloads_are_rejected(self):
        payload = self.project().to_payload('.')
        for value in ({'01': True}, {'-1': True}, {'True': True}, {'1': 1}, [], {'０': False}):
            with self.subTest(audio=value), self.assertRaises(ValueError):
                MulticamProject.from_payload(dict(payload, audio_track_enabled=value), '.')
        for value in ([float('nan')], [-.1], [True], 'wrong'):
            with self.subTest(markers=value), self.assertRaises(ValueError):
                MulticamProject.from_payload(dict(payload, timeline_markers=value), '.')

    def test_source_offsets_and_latest_overlap_match_picture_track(self):
        project = self.project()
        project.audio_track_enabled = {0: False, 1: True}
        project.clips[0].start = -.5
        project.clips.append(CameraClip('new', 1, 'new.mp4', .5, .5, proxy_path='proxy.mp4'))
        sources = audio_sources(project)
        self.assertEqual([(item.path, item.start, item.duration, item.source_start) for item in sources],
                         [('a.mp4', 0., .5, .5), ('new.mp4', .5, .5, 0.), ('a.mp4', 1., .5, 1.5)])
        self.assertNotIn('proxy.mp4', [item.path for item in sources])


class TimelineAudioSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = Path(__file__).resolve().parents[1]
        cls.ffmpeg, cls.ffprobe = find_binary('ffmpeg', app), find_binary('ffprobe', app)
        if not cls.ffmpeg or not cls.ffprobe: raise unittest.SkipTest('FFmpeg runtime unavailable')

    def setUp(self):
        work = Path(__file__).resolve().parents[1]/'work'
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='timeline_audio_', dir=work)
        self.folder = Path(self.temp.name)
        self.master = self.tone('master.wav', 440)
        self.camera = self.tone('camera.wav', 880)
        self.other = self.tone('other.wav', 1320)

    def tearDown(self): self.temp.cleanup()

    def tone(self, name, frequency, duration=2.):
        path = self.folder/name
        samples = array.array('h', (round(5000*math.sin(2*math.pi*frequency*n/48000))
                                   for n in range(round(duration*48000))))
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            stream.writeframes(samples.tobytes())
        return path

    def project(self, states, start=0., duration=2.):
        return MulticamProject(clips=[CameraClip('a', 1, str(self.camera), start, duration),
                                     CameraClip('b', 2, str(self.other), 0., 2.)],
                               master_audio=str(self.master), master_duration=2.,
                               audio_track_enabled=states, cuts=[Cut(0., 1)])

    def samples(self, path):
        with wave.open(str(path), 'rb') as stream:
            values = array.array('h', stream.readframes(stream.getnframes()))
            self.assertEqual(stream.getframerate(), 48000)
            return values[::stream.getnchannels()]

    def amplitude(self, samples, frequency, start=.2, end=.4):
        segment = samples[round(start*48000):round(end*48000)]
        sine = sum(value*math.sin(2*math.pi*frequency*i/48000) for i, value in enumerate(segment))
        cosine = sum(value*math.cos(2*math.pi*frequency*i/48000) for i, value in enumerate(segment))
        return 2*math.hypot(sine, cosine)/len(segment)

    def test_only_selected_camera_is_audible_and_all_muted_is_silent(self):
        output = self.folder/'selected.wav'
        project = self.project({0: False, 1: True, 2: False})
        render_audio_mix(project, output, self.ffmpeg, self.ffprobe)
        samples = self.samples(output)
        self.assertEqual(len(samples), 96000)
        self.assertGreater(self.amplitude(samples, 880), 3000)
        self.assertLess(self.amplitude(samples, 440), 2)
        self.assertLess(self.amplitude(samples, 1320), 2)
        project.audio_track_enabled = {0: False, 1: False, 2: False}
        render_audio_mix(project, output, self.ffmpeg, self.ffprobe)
        self.assertEqual(max(map(abs, self.samples(output))), 0)

    def test_multiple_tracks_mix_without_normalizing_single_track_gain(self):
        output = self.folder/'mixed.wav'
        render_audio_mix(self.project({0: True, 1: True, 2: False}), output, self.ffmpeg, self.ffprobe)
        samples = self.samples(output)
        self.assertGreater(self.amplitude(samples, 440), 3000)
        self.assertGreater(self.amplitude(samples, 880), 3000)
        self.assertLess(self.amplitude(samples, 1320), 2)

    def test_source_track_leading_and_trailing_gaps_are_silent(self):
        output = self.folder/'gaps.wav'
        render_audio_mix(self.project({0: False, 1: True, 2: False}, start=.5, duration=1.),
                         output, self.ffmpeg, self.ffprobe)
        samples = self.samples(output)
        self.assertEqual(max(map(abs, samples[:20000])), 0)
        self.assertGreater(self.amplitude(samples, 880, .6, .8), 3000)
        self.assertEqual(max(map(abs, samples[77000:])), 0)

    def test_silent_video_is_skipped_and_does_not_break_mix(self):
        path = self.folder/'silent.mp4'
        subprocess.run([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=black:s=96x54:r=30:d=2', '-c:v', 'libx264', '-preset',
                        'ultrafast', '-an', str(path)], check=True, capture_output=True)
        project = self.project({0: False, 1: True, 2: False})
        project.clips[0].path = str(path)
        sources, info = probe_audio_sources(self.ffprobe, audio_sources(project))
        self.assertEqual(sources, [])
        output = self.folder/'silent.wav'
        render_audio_mix(project, output, self.ffmpeg, self.ffprobe)
        self.assertEqual(max(map(abs, self.samples(output))), 0)

    def test_final_video_export_uses_track_selection_not_master_or_camera_cuts(self):
        video = self.folder/'camera.mp4'
        subprocess.run([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=blue:s=96x54:r=30:d=2', '-i', str(self.camera),
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                        '-shortest', str(video)], check=True, capture_output=True)
        project = MulticamProject(clips=[CameraClip('a', 1, str(video), 0., 2.)],
            master_audio=str(self.master), master_duration=2., cuts=[Cut(0., 1)],
            audio_track_enabled={0: False, 1: True})
        def runner(command, callback):
            result = subprocess.run(command, capture_output=True, timeout=30)
            return result.returncode, result.stderr.decode('utf-8', 'replace')
        for codec, states in [('pcm_s16le', {0: False, 1: True}),
                              ('aac', {0: True, 1: True}),
                              ('pcm_s16le', {0: False, 1: False})]:
            with self.subTest(codec=codec, states=states):
                project.audio_track_enabled = states
                settings = ExportSettings(codec='libx264', preset='ultrafast', decode_mode='cpu',
                                          audio=codec, bit_depth='8-bit')
                output = self.folder/'result.mov'
                with patch('multicam_core.cache_dir', return_value=self.folder):
                    render_multicam(project, output, self.ffmpeg, self.ffprobe, settings,
                                    Capabilities(ffmpeg_path=self.ffmpeg, ffprobe_path=self.ffprobe), runner)
                pcm = self.folder/'exported.wav'
                subprocess.run([self.ffmpeg, '-v', 'error', '-y', '-i', str(output), '-vn',
                                '-c:a', 'pcm_s16le', str(pcm)], check=True, capture_output=True)
                samples = self.samples(pcm)
                if states[1]: self.assertGreater(self.amplitude(samples, 880), 2500)
                else: self.assertEqual(max(map(abs, samples)), 0)
                if states[0]: self.assertGreater(self.amplitude(samples, 440), 2500)
                else: self.assertLess(self.amplitude(samples, 440), 10)


if __name__ == '__main__': unittest.main()
