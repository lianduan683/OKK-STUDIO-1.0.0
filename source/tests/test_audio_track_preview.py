"""Realtime audio rows follow the timeline, independently of video cuts."""
import copy
import time
import unittest
from unittest.mock import Mock, patch

from multicam_core import CameraClip, Cut
from tests import test_multicam_player as fixture
from multicam_player import MulticamPlayer, _Slot


class AudioTrackPreviewTests(unittest.TestCase):
    setUp = fixture.MulticamPlayerTests.setUp
    tearDown = fixture.MulticamPlayerTests.tearDown
    _wait = fixture.MulticamPlayerTests._wait
    _state = fixture.MulticamPlayerTests._state
    _loads = fixture.MulticamPlayerTests._loads
    _commands = fixture.MulticamPlayerTests._commands
    _configure = fixture.MulticamPlayerTests._configure
    _project = staticmethod(fixture.MulticamPlayerTests._project)

    def _audible_paths(self):
        slots = [self.player._audio, *self.player._track_audio_slots.values()]
        return {slot.controller.path for slot in slots if slot.active and slot.controller}

    def test_selected_camera_tracks_use_original_audio_without_following_programme_cut(self):
        project = self._project()
        project.clips[0].proxy_path = 'proxy-a.mp4'
        project.audio_track_enabled = {0: False, 1: True, 2: True}
        self._configure(project)
        self._wait(lambda: self._audible_paths() == {'camera-a.mp4', 'camera-b.mp4'})
        self.assertFalse(self.player._audio.active)
        self.player.submit('seek', time=9.)
        self._wait(lambda: self.player._position >= 9.)
        self.assertEqual(self._audible_paths(), {'camera-a.mp4', 'camera-b.mp4'})
        self.assertTrue(all(slot.active for slot in self.player._track_audio_slots.values()))

    def test_mute_changes_keep_current_playhead_and_all_mute_really_stops_sound(self):
        project = self._project()
        project.audio_track_enabled = {0: True, 1: True, 2: True}
        self._configure(project)
        self._wait(lambda: len(self._audible_paths()) == 3)
        self.player.submit('scrub', time=4., request_id=1, pause=True)
        self._wait(lambda: abs(self.player._position-4.) < .01)
        changed = copy.deepcopy(project)
        changed.audio_track_enabled = {0: False, 1: False, 2: False}
        self.player.submit('configure', project=changed)
        self._wait(lambda: not self._audible_paths())
        self.assertAlmostEqual(self.player._position, 4., places=2)
        self.assertFalse(self.player._playing)

    def test_segment_offsets_gaps_pause_and_resume(self):
        project = self._project()
        project.audio_track_enabled = {0: False, 1: False, 2: True}
        project.clips[1] = CameraClip('b', 2, 'later.mp4', 3., 4., has_audio=True)
        self._configure(project)
        self.assertEqual(self._audible_paths(), set())
        self.player.submit('scrub', time=5., request_id=1, pause=True)
        self._wait(lambda: self._audible_paths() == {'later.mp4'})
        slot = self.player._track_audio_slots[2]
        self.assertAlmostEqual(slot.controller.position, 2., places=2)
        self.assertTrue(slot.controller.paused)
        self.player.submit('play')
        self._wait(lambda: not slot.controller.paused)
        self.player.submit('pause')
        self._wait(lambda: slot.controller.paused)
        self.player.submit('scrub', time=8., request_id=2, pause=True)
        self._wait(lambda: not slot.active)

    def test_known_silent_source_does_not_start_an_audio_decoder(self):
        project = self._project()
        project.audio_track_enabled = {0: False, 1: True}
        project.clips[0].has_audio = False
        self._configure(project)
        time.sleep(.03)
        self.assertFalse(self.player._track_audio_slots[1].active)
        self.assertEqual(self._audible_paths(), set())

    def test_removing_camera_releases_its_audio_process(self):
        project = self._project()
        project.audio_track_enabled = {0: False, 2: True}
        self._configure(project)
        self._wait(lambda: self._audible_paths() == {'camera-b.mp4'})
        controller = self.player._track_audio_slots[2].controller
        changed = copy.deepcopy(project)
        changed.clips = [clip for clip in changed.clips if clip.camera == 1]
        changed.cuts = [Cut(0., 1)]
        self.player.submit('configure', project=changed)
        self._wait(lambda: 2 not in self.player._track_audio_slots)
        self.assertIn(('dispose', controller.window, None), [row[:3] for row in self.trace.snapshot()])


class AudioTrackClockTests(unittest.TestCase):
    def setUp(self):
        self.player = MulticamPlayer.__new__(MulticamPlayer)
        self.player._project = fixture.MulticamPlayerTests._project()
        self.player._position, self.player._anchor = 9., 19.
        self.player._playing = True
        self.player._audio = _Slot(300)
        self.player._audio_offset = 0.
        self.player._audio_seek_pending = None
        self.player._error = Mock()
        self.primary = _Slot(301, controller=self.controller(5.), active=True, audio_offset=2.)
        self.player._track_audio_slots = {1: self.primary}
        self.clock = patch('multicam_player.time.monotonic', return_value=20.)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    @staticmethod
    def controller(position):
        def command(values, **_):
            if values == ['get_property', 'time-pos']: return position
            if values == ['get_property', 'eof-reached']: return False
            return None
        return Mock(command=Mock(side_effect=command))

    def test_camera_sound_is_clock_reference_when_master_muted(self):
        self.assertEqual(self.player._clock(update_position=False), 7.)
        self.assertEqual(self.player._position, 9.)
        self.assertEqual(self.player._clock(), 7.)
        self.assertEqual(self.player._position, 7.)

    def test_camera_seek_ack_ignores_stale_audio_position(self):
        self.primary.audio_seek_pending = (8., 20.)
        self.assertEqual(self.player._clock(update_position=False), 10.)

    def test_secondary_audio_corrects_drift_against_enabled_reference(self):
        secondary = _Slot(302, controller=self.controller(1.), active=True, audio_offset=3.)
        self.player._track_audio_slots[2] = secondary
        self.player._correct_audio_drift(secondary)
        secondary.controller.command.assert_any_call(['seek', 4., 'absolute+exact'])
        self.assertEqual(secondary.audio_seek_pending, (4., 20.))
        # A pending seek must not issue another correction every worker tick.
        secondary.controller.command.reset_mock()
        self.player._correct_audio_drift(secondary)
        secondary.controller.command.assert_not_called()

    def test_failed_audio_disposal_retains_owner_for_retry(self):
        self.player._project.clips = []
        self.player._dispose = Mock(side_effect=RuntimeError('close failed'))
        self.player._prune_track_audio_slots()
        self.assertIn(1, self.player._track_audio_slots)
        self.player._dispose.side_effect = None
        self.player._prune_track_audio_slots()
        self.assertEqual(self.player._track_audio_slots, {})


if __name__ == '__main__':
    unittest.main()
