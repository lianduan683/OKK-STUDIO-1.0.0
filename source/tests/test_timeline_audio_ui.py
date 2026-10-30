"""Track controls preserve other audible tracks and draw linked audio rows."""
from copy import deepcopy
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

from audio_waveform import Waveform
from timeline_audio import audio_track_enabled
from timeline_audio_ui import TimelineAudioUIMixin
from exporter_core import find_binary

FFMPEG = find_binary('ffmpeg', Path(__file__).resolve().parents[1])

try:
    from . import test_program_edit_ui as native_fixture
except ImportError:
    import test_program_edit_ui as native_fixture


class Canvas:
    def __init__(self): self.items = {}; self.serial = 0
    def create(self, kind, args, options):
        self.serial += 1
        self.items[self.serial] = kind, args, options
        return self.serial
    def create_rectangle(self, *args, **options): return self.create('rectangle', args, options)
    def create_text(self, *args, **options): return self.create('text', args, options)
    def create_polygon(self, *args, **options): return self.create('polygon', args, options)
    def canvasx(self, value): return value
    def canvasy(self, value): return value
    def winfo_width(self): return 800


class Harness(TimelineAudioUIMixin):
    def __init__(self, master=''):
        self.project = SimpleNamespace(master_audio=master, master_duration=10., cameras=[1,2],
            audio_track_enabled={}, clips=[SimpleNamespace(path='one.mov', camera=1, start=0., duration=10., has_audio=True),
                                          SimpleNamespace(path='two.mov', camera=2, start=2., duration=6., has_audio=False)])
        self.timeline = Canvas()
        self.timeline_audio_items = set()
        self.status = Mock()
        self.history = []
        self._audio_track_changed = Mock()
        self.draw_timeline = Mock()
        self._timeline_left = 128
        self.busy = False
        self.app = SimpleNamespace(working=False)
    def _available(self): return not self.busy
    def _remember(self): self.history.append(deepcopy(self.project))
    def _timeline_camera_order(self): return [1,2]
    def _timeline_view_duration(self): return 10.
    def _timeline_scale(self): return 60.
    def _timeline_fit_label(self, font, title, width): return title
    def _px(self, value): return value
    def _waveform_for(self, path): return Waveform(np.full(400, .5), duration=12.)


class TimelineAudioControlsTests(unittest.TestCase):
    def draw(self, workspace):
        workspace._draw_timeline_audio([1,2], 150., 40., 800., 'test-font',
                                      lambda value: 128+value*60, lambda *bounds: True)

    def test_each_video_has_corresponding_audio_row(self):
        workspace = Harness('master.wav')
        self.draw(workspace)
        self.assertEqual(list(workspace._timeline_audio_track_bounds), [0,1,2])
        self.assertEqual(workspace._timeline_audio_bounds, (150.,190.))
        texts = [options['text'] for kind,args,options in workspace.timeline.items.values() if kind == 'text']
        self.assertTrue({'A0','A1','A2','主音频 · 出声'}.issubset(texts))
        self.assertTrue(any('无音频' in value for value in texts))
        self.assertTrue(any(kind == 'polygon' for kind,_,_ in workspace.timeline.items.values()))

    def test_only_master_audio_is_independently_deletable(self):
        workspace = Harness('master.wav')
        self.draw(workspace)
        self.assertEqual(len(workspace.timeline_audio_items), 2)
        for item in workspace.timeline_audio_items:
            _, coordinates, _ = workspace.timeline.items[item]
            self.assertGreaterEqual(coordinates[1], 150.)
            self.assertLess(coordinates[1], 190.)

    def test_first_override_enables_second_without_silently_muting_first(self):
        workspace = Harness()
        self.assertTrue(audio_track_enabled(workspace.project, 1))
        workspace._toggle_timeline_audio_track(2)
        self.assertTrue(audio_track_enabled(workspace.project, 1))
        self.assertTrue(audio_track_enabled(workspace.project, 2))
        self.assertEqual(len(workspace.history), 1)
        self.assertEqual(workspace.history[0].audio_track_enabled, {})
        workspace._audio_track_changed.assert_called_once()

    def test_mute_all_stays_muted_and_restores_when_toggled(self):
        workspace = Harness()
        workspace._toggle_timeline_audio_track(1)
        self.assertFalse(any(audio_track_enabled(workspace.project, track) for track in [1,2]))
        workspace._toggle_timeline_audio_track(2)
        self.assertFalse(audio_track_enabled(workspace.project, 1))
        self.assertTrue(audio_track_enabled(workspace.project, 2))

    def test_solo_mutes_master_and_other_camera_tracks_in_one_undo_entry(self):
        workspace = Harness('master.wav')
        workspace._set_timeline_audio_track(2, True, solo=True)
        self.assertEqual(workspace.project.audio_track_enabled, {0:False,1:False,2:True})
        self.assertEqual(len(workspace.history), 1)

    def test_header_button_click_changes_only_the_intended_track(self):
        workspace = Harness('master.wav')
        self.draw(workspace)
        self.assertTrue(workspace._timeline_audio_press(SimpleNamespace(x=50, y=210)))
        self.assertTrue(audio_track_enabled(workspace.project, 0))
        self.assertTrue(audio_track_enabled(workspace.project, 1))
        self.assertFalse(audio_track_enabled(workspace.project, 2))
        self.assertFalse(workspace._timeline_audio_press(SimpleNamespace(x=500, y=210)))

    def test_busy_and_unchanged_action_leave_history_untouched(self):
        workspace = Harness('master.wav')
        workspace.busy = True
        workspace._toggle_timeline_audio_track(1)
        workspace.busy = False
        workspace._set_timeline_audio_track(0, True)
        self.assertEqual(workspace.history, [])

    def test_right_click_header_exposes_mute_and_solo(self):
        workspace = Harness('master.wav')
        self.draw(workspace)
        with patch('timeline_audio_ui.tk.Menu') as constructor:
            self.assertTrue(workspace._timeline_audio_context(SimpleNamespace(x=40,y=210,x_root=40,y_root=210)))
        labels = [call.kwargs['label'] for call in constructor.return_value.add_command.call_args_list]
        self.assertEqual(labels, ['设为出声','仅此轨出声'])


class NativeTimelineWaveformTests(unittest.TestCase):
    setUp = native_fixture.ProgramEditWorkspaceTests.setUp
    tearDown = native_fixture.ProgramEditWorkspaceTests.tearDown

    @unittest.skipUnless(FFMPEG,
                         'real FFmpeg required')
    def test_async_decoded_waveform_appears_in_real_tk_timeline(self):
        workspace = self.workspace
        path = self.folder/'actual-audio.wav'
        signal = (14000*np.sin(2*np.pi*440*np.arange(16000)/8000)).astype('<i2')
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1,2,8000,0,'NONE','not compressed'))
            stream.writeframes(signal.tobytes())
        workspace.app.caps.ffmpeg_path = FFMPEG
        workspace.project.clips[0].path = str(path)
        workspace.project.clips[0].duration = 2.
        workspace.project.clips[0].has_audio = True
        workspace._refresh(reconfigure=False)
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            self.root.update()
            if workspace.timeline.find_withtag('audio-waveform'): break
            time.sleep(.01)
        self.assertTrue(workspace.timeline.find_withtag('audio-waveform'))
        self.assertIn(1, workspace._timeline_audio_track_bounds)
        self.assertEqual(self.callback_errors, [])


if __name__ == '__main__': unittest.main()
