"""Monitor edit intent and transport following in isolated real Tk widgets."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import program_spans
from . import test_timeline_drag_merge as drop_fixture
from . import test_program_edit_ui as program_fixture
from .test_transport_controls import RecordingPlayer


class MulticamClickFollowTests(unittest.TestCase):
    setUp = drop_fixture.TimelineDropTests.setUp
    tearDown = drop_fixture.TimelineDropTests.tearDown
    program_event = program_fixture.ProgramEditWorkspaceTests.program_event

    def flush(self):
        self.root.after(35, self.root.quit)
        self.root.mainloop()
        self.assertEqual(self.errors, [])
        self.mocks[2].assert_not_called()

    def select_middle(self):
        ws = self.workspace
        ws.project.edit_points = [2., 5., 8.]
        ws._refresh()
        ws.seek(3.)
        event = self.program_event((2., 5.))
        ws.timeline.event_generate('<ButtonPress-1>', x=int(event.x), y=int(event.y))
        ws.timeline.event_generate('<ButtonRelease-1>', x=int(event.x), y=int(event.y))
        self.flush()
        self.assertEqual(ws.selected_program_range, (2., 5.))
        return ws

    def test_selected_segment_monitor_click_replaces_whole_segment_and_keeps_selection(self):
        ws = self.select_middle()
        before = deepcopy(ws.project)
        ws.player = RecordingPlayer()
        ws.camera_surfaces[2].event_generate('<Button-1>', x=5, y=5)
        self.flush()
        self.assertEqual([ws.project.camera_at(t) for t in (1., 2., 3., 4.9, 5., 9.)],
                         [1, 2, 2, 2, 1, 1])
        self.assertEqual(ws.current, 3.)
        self.assertEqual(ws.selected_program_range, (2., 5.))
        self.assertEqual(ws._timeline_selection, {('program', (2., 5.))})
        self.assertTrue(ws.timeline.find_withtag('timeline-selected'))
        self.assertTrue(ws._camera_highlight_states[2][2])
        self.assertEqual([(s.start, s.end) for s in program_spans(ws.project)],
                         [(0., 2.), (2., 5.), (5., 8.), (8., 10.)])
        self.assertEqual(len(ws.undo_stack), 1)
        ws.undo()
        self.assertEqual(ws.project, before)
        ws.redo()
        self.assertEqual(ws.project.camera_at(4.), 2)

    def test_repeated_monitor_choices_keep_replacing_the_same_selected_segment(self):
        ws = self.select_middle()
        for camera in (2, 1, 2):
            ws.camera_buttons[camera].invoke()
            self.flush()
            self.assertEqual(ws.selected_program_range, (2., 5.))
            self.assertEqual(ws.project.camera_at(2.), camera)
            self.assertEqual(ws.project.camera_at(4.9), camera)
            self.assertEqual(ws.project.camera_at(5.), 1)
            self.assertTrue(ws.timeline.find_withtag('timeline-selected'))
        self.assertEqual(len(ws.undo_stack), 3)

    def test_selection_applies_even_if_playhead_has_moved_outside_it_and_recording_is_off(self):
        ws = self.select_middle()
        ws.seek(7.)
        ws.record_var.set(False)
        ws.camera_proxy_badges[2].event_generate('<Button-1>', x=1, y=1)
        self.flush()
        self.assertEqual([ws.project.camera_at(t) for t in (1., 3., 6., 7., 9.)], [1, 2, 1, 1, 1])
        self.assertEqual(ws.current, 7.)
        self.assertEqual(ws.selected_program_range, (2., 5.))

    def test_deselect_then_monitor_click_inserts_only_a_cut_at_playhead(self):
        ws = self.select_middle()
        ws._key(SimpleNamespace(keysym='Escape', char='', state=0, widget=ws.timeline))
        self.assertIsNone(ws.selected_program_range)
        ws.project.cuts.append(Cut(8., 1))
        ws.camera_surfaces[2].event_generate('<Button-1>', x=5, y=5)
        self.flush()
        self.assertIsNone(ws.selected_program_range)
        self.assertEqual([ws.project.camera_at(t) for t in (1., 2., 2.9, 3., 7., 8., 9.)],
                         [1, 1, 1, 2, 2, 1, 1])
        self.assertEqual(len(ws.undo_stack), 1)

    def test_monitor_click_records_a_cut_after_replay_disabled_recording(self):
        ws = self.workspace
        ws.record_var.set(False)
        ws.current = 3.
        ws.player = RecordingPlayer()
        ws.camera_buttons[2].invoke()
        self.flush()
        self.assertEqual(ws.project.camera_at(2.), 1)
        self.assertEqual(ws.project.camera_at(3.), 2)
        self.assertEqual(len(ws.undo_stack), 1)
        self.assertNotIn('试听', ws.program_var.get())
        self.assertNotIn(('audition', {'camera': 2}), ws.player.commands)

    def long_timeline(self, origin=0.):
        ws = self.workspace
        ws.project = MulticamProject(clips=[
            CameraClip('one', 1, 'one.mp4', origin, 3600.-origin),
            CameraClip('two', 2, 'two.mp4', 0., 3600.)], cuts=[Cut(0., 1)])
        ws.zoom = 20
        ws._refresh()
        self.flush()
        return ws

    def assert_head_visible(self):
        ws = self.workspace
        x = ws._timeline_x(ws.current)-ws.timeline.canvasx(0)
        self.assertGreaterEqual(x, ws._timeline_left)
        self.assertLessEqual(x, ws.timeline.winfo_width()-ws._px(5))

    def test_playback_follows_across_multiple_pages_and_renders_new_shots(self):
        ws = self.long_timeline()
        ws.project.cuts.append(Cut(1000., 2))
        before = deepcopy(ws.project)
        vertical = ws.timeline.yview()
        for position in (170., 350., 900., 1100., 2000., 3599.9):
            ws.player_queue.put(('state', dict(time=position, playing=True, camera=1)))
            ws._poll_player(reschedule=False)
            self.assert_head_visible()
            self.flush()
            self.assertTrue(ws.timeline_program_items)
        self.assertEqual(ws.project, before)
        self.assertEqual(ws.undo_stack, [])
        self.assertEqual(ws.timeline.yview(), vertical)

    def test_seek_forward_back_and_to_end_reveals_playhead_with_negative_source_origin(self):
        ws = self.long_timeline(origin=-15.)
        for position in (1800., 30., 3600., 0.):
            ws.seek(position)
            self.assert_head_visible()
            self.flush()
        self.assertEqual(ws.undo_stack, [])

    def test_paused_redraw_and_idle_player_state_allow_manual_scrolling(self):
        ws = self.long_timeline()
        ws.timeline.xview_moveto(.6)
        self.flush()
        original = ws.timeline.xview()
        ws.draw_playhead()
        ws.player_queue.put(('state', dict(time=0., playing=False, camera=1)))
        ws._poll_player(reschedule=False)
        self.flush()
        self.assertEqual(ws.timeline.xview(), original)

    def test_one_frame_navigation_at_right_edge_scrolls_before_head_is_hidden(self):
        ws = self.long_timeline()
        edge = ws.timeline.winfo_width()-ws._px(24)
        position = (ws.timeline.canvasx(edge)-ws._timeline_left)/ws._timeline_scale()
        ws.current = int(position*ws.project.fps)/ws.project.fps
        ws.seek(ws.current+1/ws.project.fps)
        self.assertGreater(ws.timeline.canvasx(0), 0.)
        self.assert_head_visible()
        self.flush()


if __name__ == '__main__':
    unittest.main()
