"""Actual D3D11 alpha pixels and route/clock regression coverage."""
from dataclasses import replace
from pathlib import Path
import os
import queue
import subprocess
import time
import unittest
from unittest.mock import patch

import numpy as np
from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_outer import place_outer
from stack_preview_policy import needs_stack_at
from stack_gpu import StackGpuPresenter, atlas_layout
from stack_gpu_plan import layer_atlas_command
from video_stack_player import VideoStackPlayerMixin
from video_stack_preview import StackPreview, preview_command, preview_segments
from exporter_core import probe_media, CREATE_NO_WINDOW
from tests import test_video_stack_render as fixtures


class StackPolicyTests(unittest.TestCase):
    def project(self):
        p=MulticamProject(fps=30000/1001, clips=[CameraClip('b',1,'base',0,100),
            CameraClip('a',2,'alpha',10.01,2.002,has_alpha=True,placement=dict(x=.2))],cuts=[Cut(0,1)])
        place_outer(p,2,('above',0))
        return p

    def test_outer_only_selects_composition_on_half_open_frame_range(self):
        p=self.project()
        for t,needed in ((0,False),(10.01-1/p.fps,False),(10.01,True),
                         (12.012-1/p.fps,True),(12.012,False),(90,False),(10.01,True)):
            self.assertEqual(needs_stack_at(p,t),needed,(t,needed))

    def test_hidden_disabled_and_empty_outer_tracks_do_not_force_compositor(self):
        p=self.project();p.video_stack_hidden=[2]
        self.assertFalse(needs_stack_at(p,11))
        p.video_stack_hidden=[];p.clips[1].enabled=False
        self.assertFalse(needs_stack_at(p,11))
        p.clips=p.clips[:1];p.multicam_outer_order=[2,32,0]
        self.assertFalse(needs_stack_at(p,11))

    def test_plain_placement_follows_selected_camera_and_time(self):
        p=self.project();p.video_stack_enabled=False;p.multicam_outer_order=[]
        self.assertFalse(needs_stack_at(p,11))
        p.cuts=[Cut(0,1),Cut(10.01,2),Cut(12.012,1)]
        self.assertTrue(needs_stack_at(p,11))
        self.assertFalse(needs_stack_at(p,13))

    def test_tick_reuses_compositor_until_route_actually_changes(self):
        class Player(VideoStackPlayerMixin):
            def _emit(self,*args): self.events.append(args)
        player=Player();player._init_stack_preview();player.events=[]
        player._project=self.project();player._position=0
        player._program=player._program_standby=player._comparison=player._comparison_standby=None
        from unittest.mock import Mock
        player._stack_preview=Mock()
        player._configure_stack_mode()
        player._stack_preview.reset_mock();rev=player._stack_revision
        player._configure_stack_mode(invalidate=False)
        self.assertEqual(player._stack_revision,rev)
        player._stack_preview.cancel.assert_not_called()
        player._position=11;self.assertTrue(player._configure_stack_mode(invalidate=False))
        player._stack_preview.reset_mock();rev=player._stack_revision
        player._position=11.5;self.assertFalse(player._configure_stack_mode(invalidate=False))
        self.assertEqual(player._stack_revision,rev)
        player._stack_preview.cancel.assert_not_called()
        player._position=13;self.assertTrue(player._configure_stack_mode(invalidate=False))
        self.assertEqual(player.events,[('stack_mode',True),('stack_mode',False)])

    def test_direct_compound_preview_has_no_artificial_four_second_restart(self):
        p=self.project();p.clips[1].start=0;p.clips[1].duration=80
        p.cuts=[Cut(0,1),Cut(50,1)]
        segments=list(preview_segments(p,20))
        self.assertEqual([round(s.start*p.fps) for s in segments],[round(20*p.fps),round(50*p.fps),round(80*p.fps)])
        # Export/cache planning retains the original bounded cache blocks.
        from multicam_outer import plan_outer_segments
        self.assertGreater(len(plan_outer_segments(p)),len(segments))

    def test_complex_compound_still_bounds_cache_work(self):
        p=self.project();p.multicam_inner_stack=True
        self.assertLessEqual(max(s.duration for s in preview_segments(p,0)),4.01)


class StackRoutePlaybackTests(unittest.TestCase):
    from tests.test_multicam_player import MulticamPlayerTests as Fixture
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    _wait=Fixture._wait
    _state=Fixture._state
    _configure=Fixture._configure
    _commands=Fixture._commands
    _loads=Fixture._loads

    def test_play_across_outer_clip_keeps_master_audio_running_and_returns_native(self):
        from multicam_player import MulticamPlayer
        from tests.test_multicam_player import MulticamPlayerTests
        p=MulticamPlayerTests._project()
        p.clips[1].start=.3;p.clips[1].duration=.3;p.clips[1].has_alpha=True
        p.cuts=[Cut(0,1)];place_outer(p,2,('above',0))
        with patch.object(MulticamPlayer,'_sync_stack_preview'):
            self._configure(p)
            self.assertFalse(self.player._stack_enabled)
            self.player.submit('play')
            self._state(lambda s:s['playing'] and s['time']>.02)
            before=len(self.trace.snapshot())
            self._wait(lambda:self.player._position>.85)
            self.assertFalse(self.player._stack_enabled)
            self.assertEqual([value for kind,value in self.received if kind=='stack_mode'],[True,False])
            audio=[x for x in self.trace.snapshot()[before:] if x[0]=='command' and x[1]==300]
            self.assertFalse([x for x in audio if x[2][:1] in (('loadfile',),('seek',))])
            self.assertFalse([x for x in audio if x[2]==('set_property','pause',True)])


class StackGpuUiTests(unittest.TestCase):
    from tests.test_timeline_drag_merge import TimelineDropTests as Fixture
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown

    def test_ordinary_placement_can_show_gpu_frame_and_hide_after_route_exit(self):
        from types import SimpleNamespace
        ws=self.workspace;ws.player=SimpleNamespace()
        ws.project.video_stack_enabled=False;ws.project.clips[0].placement=dict(x=.2)
        from gpu_live_session import runtime_directory
        renderer=StackGpuPresenter(ws.stack_preview_label.winfo_id(),runtime_directory())
        ws._stack_gpu_presenter=renderer
        try:
            ws._accept_stack_preview_frame=lambda value:True
            ws._set_stack_preview_mode(True);self.root.update_idletasks()
            frame=dict(width=128,height=72,rgba=bytes((255,0,0,255))*128*72,time=1,request_id=0)
            with patch('stack_preview_ui.tk.PhotoImage',side_effect=AssertionError('CPU image expansion')):
                self.assertTrue(ws._present_stack_frame(frame))
            self.assertEqual(renderer.presented,1)
            ws._set_stack_preview_mode(False)
            self.assertFalse(ws._stack_preview_active)
            self.assertIsNone(ws._stack_preview_last_payload)
            self.assertEqual(self.errors,[])
        finally:
            ws.player=None

    def test_gpu_presentation_failure_reconfigures_cpu_without_showing_atlas(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        ws=self.workspace;ws.player=SimpleNamespace()
        renderer=Mock();renderer.present.side_effect=RuntimeError('lost device')
        ws._stack_gpu_presenter=renderer
        ws._accept_stack_preview_frame=lambda value:True
        ws._set_stack_preview_mode(True)
        layout=atlas_layout(2,4,2)
        with patch.object(ws,'_configure_player') as configure:
            self.assertFalse(ws._present_stack_frame(dict(width=8,height=2,rgba=bytes(64),
                gpu_layers=layout,time=1,request_id=0)))
            configure.assert_called_once()
        self.assertTrue(ws._stack_gpu_failed)
        self.assertIsNone(ws._stack_gpu_presenter)
        ws.player=None


class StackGpuMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.StackMediaTests.setUpClass();cls.fixture=fixtures.StackMediaTests()
        cls.runtime=Path(os.environ.get('OKK_GPU_RUNTIME',str(Path(__file__).resolve().parents[1]/'bin/gpu_live')))
        if not cls.runtime.is_dir(): raise unittest.SkipTest('GPU runtime required')

    @classmethod
    def tearDownClass(cls): fixtures.StackMediaTests.tearDownClass()

    def render(self,cmd):
        r=subprocess.run(cmd,capture_output=True,timeout=15,creationflags=CREATE_NO_WINDOW)
        self.assertEqual(r.returncode,0,r.stderr.decode('utf-8','replace'))
        return r.stdout

    def compare(self, project):
        f=self.fixture; segment=next(preview_segments(project,0,single_frame=True))
        if project.multicam_outer_order:
            from multicam_outer import resolve_compound
            segment=resolve_compound(segment,project,f.ffmpeg,f.ffprobe,128,72,preview=True)
        info={c.path:probe_media(f.ffprobe,c.path) for c in project.clips}
        command,fps=preview_command(segment,project,f.ffmpeg,info,128,72,single_frame=True)
        expected=np.frombuffer(self.render(command),np.uint8).reshape(72,128,4)
        atlas,layout=layer_atlas_command(command,fps,segment,project,128,72)
        self.assertIsNotNone(layout)
        self.assertNotIn('overlay=',atlas[atlas.index('-filter_complex')+1])
        raw=self.render(atlas)
        renderer=StackGpuPresenter(0,self.runtime,width=128,height=72)
        try:
            renderer.present(raw,128*layout['columns'],72*layout['rows'],layout,layout['grades'])
            actual=np.frombuffer(renderer.gfx.diagnostic_pixels(),np.uint8).reshape(72,128,4)[:,:,[2,1,0,3]]
            error=np.abs(actual.astype(int)-expected.astype(int))
            self.assertLessEqual(int(error.max()),4)
            self.assertEqual(renderer.readbacks,0)
        finally: renderer.close()

    def test_real_transparency_and_layer_order_match_cpu_reference(self):
        self.compare(self.fixture.project())

    def test_three_layers_padded_atlas_and_placement_match(self):
        p=self.fixture.project()
        p.clips[0].placement=dict(x=.2,y=-.1,scale=.5)
        p.clips.append(CameraClip('p',3,str(self.fixture.portrait),0,2,has_alpha=True))
        p.video_stack_order=[1,3,2]
        self.compare(p)

    def test_post_composition_global_grade_stays_after_alpha_blend(self):
        p=self.fixture.project();p.global_grade=ColorGrade(exposure=.2,saturation=1.12)
        self.compare(p)

    def test_36_alpha_layers_use_grid_without_dropping_any_layer(self):
        p=self.fixture.project()
        p.clips=[CameraClip(str(i),i,str(self.fixture.top),0,2,has_alpha=True) for i in range(1,37)]
        p.video_stack_order=list(range(1,37));p.cuts=[Cut(0,1)]
        self.compare(p)

    def test_outer_alpha_and_programme_placement_preserve_multicam_picture(self):
        p=self.fixture.project()
        p.video_stack_enabled=False;p.video_stack_order=[]
        place_outer(p,1,('above',0))
        p.clips[0].placement=dict(x=.25,scale=.5)
        self.compare(p)

    def test_lut_and_subtitles_keep_full_effect_cpu_fallback(self):
        p=self.fixture.project();segment=next(preview_segments(p,0,single_frame=True))
        info={c.path:probe_media(self.fixture.ffprobe,c.path) for c in p.clips}
        command,fps=preview_command(segment,p,self.fixture.ffmpeg,info,128,72,single_frame=True)
        actual,layout=layer_atlas_command(command,fps,segment,p,128,72,'subtitle.ass')
        self.assertIs(actual,command);self.assertIsNone(layout)
        p.global_grade=ColorGrade(lut_path='final.cube')
        actual,layout=layer_atlas_command(command,fps,segment,p,128,72)
        self.assertIs(actual,command);self.assertIsNone(layout)

    def test_worker_atlas_mailbox_shape_and_cancel_cleanup(self):
        events=queue.Queue();f=self.fixture
        preview=StackPreview(f.ffmpeg,f.ffprobe,lambda *x:events.put(x),width=128,height=72,gpu_layers=True)
        try:
            preview.request(f.project(),0,False,7,use_proxies=False)
            deadline=time.monotonic()+5;frame=None
            while time.monotonic()<deadline and frame is None:
                frame=preview.take_frame();time.sleep(.005)
            self.assertIsNotNone(frame,list(events.queue))
            self.assertEqual(frame['gpu_layers']['count'],2)
            self.assertEqual((frame['width'],frame['height']),(256,72))
            self.assertEqual(len(frame['rgba']),256*72*4)
            preview.cancel();self.assertIsNone(preview.take_frame())
        finally: preview.close()
        self.assertFalse(preview._processes)

    def test_invalid_atlas_is_rejected_before_native_upload(self):
        r=StackGpuPresenter(0,self.runtime,width=128,height=72)
        try:
            with self.assertRaises(ValueError): r.present(b'bad',128,72)
            self.assertEqual(r.presented,0)
        finally: r.close()
