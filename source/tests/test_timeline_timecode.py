"""Timeline time display and seeking through real, isolated Tk bindings."""
from copy import deepcopy
import tkinter as tk
import unittest

from editor_core import format_time
from multicam_core import CameraClip, Cut, MulticamProject
from . import test_timeline_drag_merge as fixture
from .test_transport_controls import RecordingPlayer


class TimelineTimecodeTests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown

    def key(self, sequence):
        # Execute the registered widget binding without taking system focus.
        entry = self.workspace.seek_entry
        event = '<<TimelineTimecodeTest>>'
        script = entry.bind(sequence)
        self.assertTrue(script)
        script = script.replace('%T', '2')
        entry.bind(event, script)
        try:
            entry.event_generate(event, when='now')
        finally:
            entry.unbind(event)
        self.assertEqual(self.errors, [])

    def test_seeking_and_frame_steps_update_the_display(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        ws.seek(2.)
        self.assertEqual(ws.seek_entry.get(), '00:00:02.000')
        ws.step_frame(1)
        self.assertEqual(ws.seek_entry.get(), format_time(2. + 1 / ws.project.fps))

    def test_player_state_updates_the_display(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        for position in (1.25, 4.5, 8.):
            ws.player_queue.put(('state', dict(time=position, playing=True, camera=1)))
            ws._poll_player(reschedule=False)
            self.assertEqual(ws.seek_entry.get(), format_time(position))
        self.assertEqual(ws.player.scrubs, [])

    def test_ruler_drag_updates_the_display(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        canvas = ws.timeline
        y = max(1, round(ws._px(12) - canvas.canvasy(0)))
        for index, position in enumerate((2., 4., 6.)):
            x = round(ws._timeline_x(position) - canvas.canvasx(0))
            canvas.event_generate('<ButtonPress-1>' if index == 0 else '<B1-Motion>', x=x, y=y)
            self.assertEqual(ws.seek_entry.get(), format_time(ws.current))
        canvas.event_generate('<ButtonRelease-1>', x=x, y=y)
        self.assertAlmostEqual(ws.current, 6., delta=1 / ws.project.fps)

    def test_enter_and_keypad_enter_seek_and_normalize_the_actual_frame(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        before = deepcopy(ws.project)
        for sequence, text in (('<Return>', '00:00:03.512'), ('<KP_Enter>', '6.25')):
            ws.seek_var.set(text)
            self.key(sequence)
            self.assertEqual(ws.seek_entry.get(), format_time(ws.current))
            self.assertAlmostEqual(ws.current, float(text.split(':')[-1]), delta=.5 / ws.project.fps)
            self.assertEqual(ws.player.scrubs[-1]['time'], ws.current)
            self.assertFalse(ws.playing)
        self.assertEqual(ws.project, before)
        self.assertEqual(ws.undo_stack, [])

    def test_locate_button_uses_the_same_seek_path(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        ws.seek_var.set('00:00:07.000')
        button = next(child for child in ws.timeline_bar.winfo_children()
                      if child.winfo_class() == 'TButton' and child.cget('text') == '定位')
        button.invoke()
        self.assertEqual(ws.current, 7.)
        self.assertEqual(ws.seek_entry.get(), '00:00:07.000')

    def test_playback_preserves_an_unfinished_edit_until_commit(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        ws.seek_var.set('00:00:06.')
        ws.player_queue.put(('state', dict(time=2., playing=True, camera=1)))
        ws._poll_player(reschedule=False)
        self.assertEqual(ws.current, 2.)
        self.assertEqual(ws.seek_entry.get(), '00:00:06.')
        self.key('<Return>')
        self.assertEqual(ws.current, 6.)
        self.assertEqual(ws.seek_entry.get(), '00:00:06.000')

    def test_focus_out_commits_a_draft_but_leaves_unchanged_playback_running(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        ws.playing = True
        ws.seek_entry.event_generate('<FocusOut>', when='now')
        self.assertTrue(ws.playing)
        self.assertEqual(ws.player.scrubs, [])
        ws.seek_var.set('4.5')
        ws.seek_entry.event_generate('<FocusOut>', when='now')
        self.assertEqual(ws.current, 4.5)
        self.assertEqual(ws.seek_entry.get(), '00:00:04.500')
        self.assertEqual(len(ws.player.scrubs), 1)
        self.assertFalse(ws.playing)

    def test_escape_restores_the_current_position_without_seeking(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        ws.seek(2.)
        count = len(ws.player.scrubs)
        ws.seek_var.set('8')
        self.key('<Escape>')
        self.assertEqual(ws.current, 2.)
        self.assertEqual(ws.seek_entry.get(), '00:00:02.000')
        self.assertEqual(len(ws.player.scrubs), count)

    def test_invalid_input_restores_the_display_without_moving_the_playhead(self):
        ws = self.workspace
        ws.player = RecordingPlayer()
        ws.seek(2.)
        count = len(ws.player.scrubs)
        for text in ('', 'bad', '-1', '00:00:61.000', 'nan', 'inf'):
            with self.subTest(text=text):
                ws.seek_var.set(text)
                self.key('<Return>')
                self.assertEqual(ws.current, 2.)
                self.assertEqual(ws.seek_entry.get(), '00:00:02.000')
                self.assertEqual(len(ws.player.scrubs), count)
        self.assertEqual(self.mocks[2].call_count, 6)
        self.mocks[2].reset_mock()
        ws.seek_var.set('bad')
        ws.seek_entry.event_generate('<FocusOut>', when='now')
        self.mocks[2].assert_not_called()
        self.assertIn('定位时间无效', ws.status.get())

    def test_out_of_range_input_shows_the_clamped_position(self):
        ws = self.workspace
        ws.seek_var.set('01:00:00.000')
        self.key('<Return>')
        self.assertEqual(ws.current, ws.project.duration)
        self.assertEqual(ws.seek_entry.get(), format_time(ws.project.duration))

    def test_long_timeline_seek_reveals_the_playhead(self):
        ws = self.workspace
        ws.project = MulticamProject(clips=[CameraClip('one', 1, 'one.mp4', 0., 3600.)],
                                     cuts=[Cut(0., 1)])
        ws.zoom = 20
        ws._refresh()
        self.root.update_idletasks()
        ws.seek_var.set('00:30:00.000')
        self.key('<Return>')
        x = ws._timeline_x(ws.current) - ws.timeline.canvasx(0)
        self.assertEqual(ws.current, 1800.)
        self.assertGreaterEqual(x, ws._timeline_left)
        self.assertLessEqual(x, ws.timeline.winfo_width())

    def test_new_project_clears_an_old_draft(self):
        ws = self.workspace
        ws.seek_var.set('7')
        ws.new()
        self.assertEqual(ws.current, 0.)
        self.assertEqual(ws.seek_entry.get(), '00:00:00.000')


if __name__ == '__main__':
    unittest.main()
