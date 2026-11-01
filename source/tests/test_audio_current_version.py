"""Audio upgrade coexists with current ordering, framing and project sessions."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest

from audio_mixer import strip
from clip_transform import ClipTransform, ClipTransformRange
from multicam_core import CameraClip, Cut, MulticamProject
from project_file import build_project_payload, read_project_file, write_project_file
from studio_state import restore_okk
from tests import test_camera_order_ui as fixture
from timeline_edits import delete_source_clips, finalize_source_edit


class AudioCurrentVersionTests(unittest.TestCase):
    setUp = fixture.CameraOrderUITests.setUp
    tearDown = fixture.CameraOrderUITests.tearDown
    prepare = fixture.CameraOrderUITests.prepare
    point = fixture.CameraOrderUITests.point
    press = fixture.CameraOrderUITests.press
    drag = fixture.CameraOrderUITests.drag

    def seed(self):
        self.prepare(collapsed=True)
        ws = self.workspace
        for clip in ws.project.clips: clip.has_audio = True
        ws.project.cuts = [Cut(0., 1), Cut(3., 3), Cut(6., 2)]
        ws.project.clip_transforms = [ClipTransformRange(3., 6., ClipTransform('fill', 1.3, .1, -.1))]
        ws.project.timeline_view = dict(position=4., zoom=2., x_fraction=.1, y_fraction=.2)
        ws._refresh()
        return ws, ws.audio_workspace

    def test_reorder_updates_console_labels_and_keeps_effects_on_original_source(self):
        ws, panel = self.seed()
        panel.commit_console('1', dict(gain_db=-6., pan=.25))
        edit = deepcopy(ws.project)
        self.drag('camera_3', 'camera_1')
        self.assertEqual(panel.track_ids, ['master', '3', '1', '2'])
        self.assertTrue(panel.console.channels['3'].name.get().startswith('A1\n'))
        self.assertTrue(panel.console.channels['1'].name.get().startswith('A2\n'))
        self.assertEqual(strip(ws.project, '1')['gain_db'], -6.)
        self.assertEqual(strip(ws.project, '3')['gain_db'], 0.)
        self.assertEqual(ws.project.clips, edit.clips)
        self.assertEqual(ws.project.cuts, edit.cuts)
        self.assertEqual(ws.project.clip_transforms, edit.clip_transforms)
        ws.undo()
        self.assertEqual(panel.track_ids, ['master', '1', '2', '3'])
        ws.redo()
        self.assertEqual(panel.track_ids, ['master', '3', '1', '2'])
        self.assertEqual(strip(ws.project, '1')['pan'], .25)
        self.assertEqual(self.errors, [])

    def test_audio_and_all_current_edit_fields_roundtrip_in_both_formats(self):
        ws, panel = self.seed()
        panel.commit_console('2', dict(gain_db=-4.))
        ws.project.video_stack_order = [3, 1, 2]
        before = deepcopy(ws.project)
        payload = ws.project.to_payload('.')
        self.assertEqual(payload['version'], 20)
        self.assertEqual(MulticamProject.from_payload(payload, '.').to_payload('.'), payload)
        legacy = dict(payload, version=18)
        legacy.pop('clip_transforms'); legacy.pop('timeline_view')
        self.assertEqual(MulticamProject.from_payload(legacy, '.').audio_mixer, before.audio_mixer)
        self.root.multicam_workspace = ws
        with __import__('tempfile').TemporaryDirectory() as folder:
            path = Path(folder)/'调音与机位排序.rtxproj'
            write_project_file(path, build_project_payload(self.root, app_version='1.0.8'))
            ws.project = MulticamProject()
            restore_okk(self.root, read_project_file(path)['state']['okk_multicam'])
            self.assertEqual(ws.project.audio_mixer, before.audio_mixer)
            self.assertEqual(ws.project.clip_transforms, before.clip_transforms)
            self.assertEqual(ws.project.video_stack_order, [3, 1, 2])
            self.assertEqual(ws.project.cuts, before.cuts)
        self.assertEqual(self.errors, [])

    def test_deleting_source_does_not_transfer_old_insert_settings_to_reused_id(self):
        ws, panel = self.seed()
        panel.commit_console('2', dict(gain_db=-10.))
        delete_source_clips(ws.project, ['two'])
        finalize_source_edit(ws.project)
        self.assertNotIn('2', ws.project.audio_mixer['strips'])
        ws.project.clips.append(CameraClip('replacement', 2, 'replacement.mp4', 0., 10., has_audio=True))
        ws._refresh()
        self.assertEqual(strip(ws.project, '2')['gain_db'], 0.)
        self.assertEqual(self.errors, [])

    def test_fader_arrow_keys_adjust_audio_without_changing_picture_selection(self):
        ws, panel = self.seed()
        ws.finishing_workspace.show('audio')
        self.root.update()
        channel = panel.console.channels['1']
        ws._tag_transport_widgets(ws)
        self.assertNotIn(ws._transport_tag, channel.fader.bindtags())
        channel.fader.focus_force()
        self.root.update()
        channel.fader.event_generate('<Up>')
        self.assertAlmostEqual(strip(ws.project, '1')['gain_db'], .1)
        self.assertEqual(ws.project.camera_at(0.), 1)
        self.assertEqual(self.errors, [])


if __name__ == '__main__': unittest.main()
