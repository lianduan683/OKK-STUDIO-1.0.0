"""Persistent original-only playback and media-list deletion regressions."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exporter_core import find_binary, CREATE_NO_WINDOW
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_player import MulticamPlayer
from proxy_media import PreviewProxyResolver
from transition_preview import transition_signature
from video_stack_preview import StackPreview
from tests import test_timeline_drag_merge as fixture


class OriginalOnlyModelTests(unittest.TestCase):
    def project(self):
        return MulticamProject(clips=[CameraClip('one', 1, 'source.mov', 0., 10.,
            proxy_path='proxy.mov', proxy_disabled=True)], cuts=[Cut(0., 1)])

    def test_roundtrip_opt_out_preserves_edits_and_old_projects_need_no_new_field(self):
        project = self.project()
        payload = project.to_payload('.')
        self.assertEqual(payload['version'], 23)
        restored = MulticamProject.from_payload(payload, '.')
        self.assertTrue(restored.clips[0].proxy_disabled)
        self.assertEqual(restored.cuts, project.cuts)
        project.clips[0].proxy_disabled = False
        legacy = project.to_payload('.')
        self.assertLess(legacy['version'], 23)
        self.assertNotIn('proxy_disabled', legacy['clips'][0])
        self.assertFalse(MulticamProject.from_payload(legacy, '.').clips[0].proxy_disabled)
        project.clips[0].proxy_disabled = 'yes'
        with self.assertRaises(ValueError): project.validate()

    def test_opt_out_bypasses_explicit_indexed_and_neighbor_proxy_discovery(self):
        project = self.project()
        with patch('multicam_core.find_proxy') as discovered:
            self.assertEqual(project.media_at(1, 3., use_proxies=True)[0], 'source.mov')
            discovered.assert_not_called()
        resolver = PreviewProxyResolver()
        with patch('proxy_media.find_proxy') as discovered:
            self.assertEqual(resolver('source.mov', 'proxy.mov', disabled=True), 'source.mov')
            self.assertEqual(resolver('source.mov', disabled=True), 'source.mov')
            discovered.assert_not_called()
        player = MulticamPlayer.__new__(MulticamPlayer)
        player._project = project
        player._audition = None
        player._color_monitoring = False
        player._use_proxies = True
        player._proxy_resolver = Mock(return_value='wrong-proxy.mov')
        self.assertEqual(player._clip(1, 3.)[1:], ('source.mov', 3.))
        player._proxy_resolver.assert_not_called()
        player._project = SimpleNamespace(clips=project.clips, cameras=[],
            clip_at=lambda _camera, _time: project.clips[0])
        self.assertEqual(player._clip(1, 3.)[1:], ('source.mov', 3.))

    def test_original_render_signature_is_independent_of_proxy_opt_out(self):
        project = self.project()
        before = transition_signature(project)
        project.clips[0].proxy_disabled = False
        self.assertEqual(transition_signature(project), before)


class MaterialActionsUITests(unittest.TestCase):
    setUp = fixture.TimelineDropTests.setUp
    tearDown = fixture.TimelineDropTests.tearDown

    def test_unlink_covers_split_instances_only_in_selected_camera_and_undo_restores(self):
        ws = self.workspace
        first = ws.project.clips[0]
        first.duration = 5.
        first.proxy_path = 'old-proxy.mov'
        ws.project.clips.append(replace(first, id='tail', start=5., source_in=5.))
        ws.project.clips[1].path = first.path
        ws.project.clips[1].proxy_path = 'other-proxy.mov'
        ws._refresh()
        before = deepcopy(ws.project)
        token = ws._quick_proxy_token
        history = len(ws.undo_stack)
        self.assertTrue(ws.unlink_material_proxies(('tail',)))
        for clip in ws.project.clips:
            self.assertEqual(clip.proxy_disabled, clip.camera == 1)
        self.assertEqual(ws.project.clips[1].proxy_path, 'other-proxy.mov')
        self.assertEqual(ws.project.cuts, before.cuts)
        self.assertEqual(ws.project.edit_points, before.edit_points)
        self.assertEqual(len(ws.undo_stack), history+1)
        self.assertGreater(ws._quick_proxy_token, token)
        self.assertEqual(ws.camera_proxy_badges[1].cget('text'), '使用原片')
        ws.undo()
        self.assertEqual(ws.project, before)
        ws.redo()
        self.assertTrue(ws.project.clips[0].proxy_disabled)
        self.assertEqual(self.errors, [])

    def test_context_menu_unlinks_and_camera_menu_handles_all_material(self):
        ws = self.workspace
        tree = ws.media_tree
        tree.item('camera_1', open=True)
        self.root.update()
        bounds = tree.bbox('one')
        event = SimpleNamespace(x=20, y=bounds[1]+bounds[3]/2, x_root=0, y_root=0)
        with patch('tkinter.Menu.tk_popup'): ws._camera_context(event)
        menu = ws.camera_context_menu
        entry = next(i for i in range(menu.index('end')+1)
                     if menu.type(i) != 'separator' and menu.entrycget(i, 'label') == '取消代理（使用原片）')
        menu.invoke(entry)
        self.assertTrue(ws.project.clips[0].proxy_disabled)
        self.assertFalse(ws.project.clips[1].proxy_disabled)
        self.assertEqual(self.errors, [])

    def test_media_backspace_removes_selected_old_halves_keeps_programme_and_undo(self):
        ws = self.workspace
        first = ws.project.clips[0]
        first.duration = 5.
        ws.project.clips.extend([replace(first, id='tail', start=5., source_in=5.),
                                 replace(first, id='replacement', duration=10.)])
        ws.project.cuts = [Cut(0., 1), Cut(3., 2), Cut(6., 1)]
        ws._refresh()
        before = deepcopy(ws.project)
        ws._set_timeline_selection({('program', (0., 3.))})
        tree = ws.media_tree
        tree.selection_set(('one', 'tail'))
        tree.focus_force()
        self.root.update()
        history = len(ws.undo_stack)
        self.assertTrue(tree.bind('<BackSpace>'))
        tree.event_generate('<KeyPress-BackSpace>')
        self.root.update()
        self.assertEqual({clip.id for clip in ws.project.clips}, {'two', 'replacement'})
        self.assertEqual(ws.project.cuts, before.cuts)
        self.assertEqual(len(ws.undo_stack), history+1)
        ws.undo()
        self.assertEqual(ws.project, before)
        self.assertEqual(self.errors, [])

    def test_backspace_in_text_field_does_not_delete_selected_media(self):
        ws = self.workspace
        before = deepcopy(ws.project)
        ws.media_tree.selection_set('one')
        entry = tk.Entry(self.root)
        entry.pack()
        entry.insert(0, 'test')
        entry.icursor('end')
        entry.focus_force()
        self.root.update()
        entry.event_generate('<KeyPress-BackSpace>')
        self.root.update()
        self.assertEqual(entry.get(), 'tes')
        self.assertEqual(ws.project, before)


class OriginalOnlyCompositionTests(unittest.TestCase):
    def test_ordinary_and_compound_composition_use_original_after_unlink(self):
        ffmpeg, ffprobe = find_binary('ffmpeg', '.'), find_binary('ffprobe', '.')
        if not ffmpeg or not ffprobe: self.skipTest('FFmpeg required')
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for name, color in [('source', 'red'), ('proxy', 'blue')]:
                subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    f'color=c={color}:s=96x54:r=30:d=1', '-c:v', 'libx264',
                    '-preset', 'ultrafast', '-an', str(folder/(name+'.mp4'))],
                    capture_output=True, check=True, creationflags=CREATE_NO_WINDOW)
            project = MulticamProject(clips=[CameraClip('one', 1, str(folder/'source.mp4'),
                0., 1., proxy_path=str(folder/'proxy.mp4'))], cuts=[Cut(0., 1)],
                video_stack_enabled=True)
            preview = StackPreview(ffmpeg, ffprobe, lambda *_: None, width=96, height=54)
            try:
                for request, (disabled, compound) in enumerate([(False, False), (True, False), (True, True)], 1):
                    project.clips[0].proxy_disabled = disabled
                    project.multicam_outer_order = [0] if compound else []
                    preview.request(deepcopy(project), .2, False, request, use_proxies=True)
                    deadline = time.monotonic()+12
                    frame = None
                    while time.monotonic()<deadline:
                        frame = preview.take_frame()
                        if frame is not None and frame['request_id'] == request: break
                        time.sleep(.01)
                    self.assertIsNotNone(frame)
                    import numpy as np
                    pixel = np.frombuffer(frame['rgba'], dtype=np.uint8).reshape(frame['height'], frame['width'], 4)[20, 30]
                    self.assertGreater(int(pixel[0 if disabled else 2]), 220)
                    self.assertLess(int(pixel[2 if disabled else 0]), 30)
            finally:
                preview.close()
