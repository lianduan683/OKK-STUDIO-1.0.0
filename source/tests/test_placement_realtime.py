"""Per-shot placement, real entry drags, and decoder-preserving updates."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock,patch

from clip_transform import ClipTransform,set_transform_on_range,transform_at
from multicam_core import CameraClip,Cut,MulticamProject
from program_edits import split_at
from video_placement import placement_at,set_program_placement
from realtime_placement import RealtimePlacement


class ShotPlacementTests(unittest.TestCase):
    def project(self):
        return MulticamProject(clips=[CameraClip('one',1,'one.mov',0,10)],cuts=[Cut(0,1)],fps=30)

    def test_cut_changes_only_selected_half_and_original_source_is_unchanged(self):
        p=self.project();split_at(p,4);original=deepcopy(p.clips)
        set_program_placement(p,4,10,dict(x=.2,scale=.5))
        self.assertEqual(placement_at(p,1,4-1/p.fps),(0,0,1))
        self.assertEqual(placement_at(p,1,4),(.2,0,.5))
        self.assertEqual(p.clips,original)
        self.assertEqual(p.to_payload(Path.cwd())['version'],24)
        restored=MulticamProject.from_payload(p.to_payload(Path.cwd()),Path.cwd())
        self.assertEqual(placement_at(restored,1,5),(.2,0,.5))

    def test_split_again_and_adjust_one_part_preserves_the_other_parts(self):
        p=self.project();set_program_placement(p,0,10,dict(x=.1,scale=.8));split_at(p,4)
        set_program_placement(p,4,7,dict(y=-.2,scale=1.5))
        self.assertEqual([placement_at(p,1,t) for t in (1,5,8)],[(.1,0,.8),(0,-.2,1.5),(.1,0,.8)])

    def test_local_reset_overrides_old_source_setting_without_resetting_source(self):
        p=self.project();p.clips[0].placement=dict(x=.3)
        set_program_placement(p,2,4,{})
        self.assertEqual([placement_at(p,1,t) for t in (1,3,5)],[(.3,0,1),(0,0,1),(.3,0,1)])

    def test_framing_and_placement_changes_retain_each_other(self):
        p=self.project();set_transform_on_range(p,0,5,ClipTransform(zoom=2))
        set_program_placement(p,2,8,dict(scale=.5))
        self.assertEqual(transform_at(p,3).zoom,2)
        self.assertEqual(transform_at(p,7).zoom,1)
        set_transform_on_range(p,0,10,ClipTransform(zoom=1.5))
        self.assertEqual(placement_at(p,1,3),(0,0,.5))
        self.assertEqual(placement_at(p,1,9),(0,0,1))

    def test_ordinary_old_project_keeps_old_serialized_transform(self):
        p=self.project();set_transform_on_range(p,0,4,ClipTransform(zoom=2))
        data=p.to_payload(Path.cwd())
        self.assertEqual(data['version'],18)
        self.assertNotIn('placement',data['clip_transforms'][0]['transform'])


class PlacementEntryDragTests(unittest.TestCase):
    from tests.test_clip_inspector import ClipInspectorTests as Fixture
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown

    def drag(self,index,delta,*,finish=True,state=0):
        p=self.panel;entry=p.placement_fields[index]
        var=(p.placement_x,p.placement_y,p.placement_scale)[index]
        p._start_placement_drag(SimpleNamespace(widget=entry,x_root=100),var,index)
        p._move_placement_drag(SimpleNamespace(x_root=100+delta,state=state))
        if finish:p._release_placement_drag()

    def test_apply_in_programme_affects_selected_shot_only_and_undo_restores(self):
        w,p=self.workspace,self.panel
        original=deepcopy(w.project)
        p.placement_x.set('192');p.placement_scale.set('50');self.assertTrue(p.apply_placement())
        self.assertEqual(w.project.clips,original.clips)
        self.assertEqual(placement_at(w.project,1,1),(.1,0,.5))
        self.assertEqual(placement_at(w.project,1,8),(0,0,1))
        self.assertEqual(len(w.undo_stack),1);w.undo();self.assertEqual(w.project,original)
        w.redo();self.assertEqual(placement_at(w.project,1,1),(.1,0,.5))

    def test_all_three_entries_commit_one_undo_per_drag(self):
        w,p=self.workspace,self.panel
        original=deepcopy(w.project.clips)
        for index in range(3):self.drag(index,40)
        self.assertEqual(len(w.undo_stack),3)
        self.assertEqual(placement_at(w.project,1,1),(40/1920,40/1080,1.1))
        self.assertEqual(w.project.clips,original)
        self.assertEqual(self.errors,[])

    def test_live_drag_only_publishes_draft_until_release_and_escape_discards_it(self):
        w,p=self.workspace,self.panel;original=deepcopy(w.project)
        player=Mock();w.player=player
        try:
            self.drag(0,100,finish=False)
            p._preview_placement_drag()
            self.assertEqual(w.project,original);self.assertEqual(len(w.undo_stack),0)
            self.assertTrue(any(c.args[0]=='placement_preview' for c in player.submit.call_args_list))
            p._cancel_placement_drag()
            self.assertEqual(w.project,original);self.assertEqual(p.placement_x.get(),'0')
            self.assertIsNone(player.submit.call_args_list[-1].kwargs['value'])
        finally:w.player=None

    def test_shift_is_precise_and_scale_is_clamped(self):
        self.drag(2,40,state=1)
        self.assertEqual(self.panel.placement_scale.get(),'101')
        self.drag(2,-100000)
        self.assertEqual(self.panel.placement_scale.get(),'1')
        self.drag(2,100000)
        self.assertEqual(self.panel.placement_scale.get(),'400')

    def test_click_keeps_text_entry_and_no_change_creates_no_history(self):
        self.drag(0,1)
        self.assertEqual(len(self.workspace.undo_stack),0)
        self.panel.placement_x.set('20');self.assertTrue(self.panel.apply_placement())

    def test_real_mouse_events_survive_focus_switch_and_release_outside_field(self):
        p=self.panel;self.workspace.show_inspector('clip')
        p.scroll_to('placement');self.root.update()
        p.placement_fields[0].focus_force();self.root.update()
        entry=p.placement_fields[1]
        entry.event_generate('<ButtonPress-1>',x=5,y=5)
        self.root.update()
        self.assertIsNotNone(p._placement_drag)
        entry.event_generate('<B1-Motion>',x=105,y=5)
        self.root.update()
        entry.event_generate('<ButtonRelease-1>',x=105,y=5)
        self.root.update()
        self.assertEqual(placement_at(self.workspace.project,1,1),(0,100/1080,1))
        self.assertEqual(len(self.workspace.undo_stack),1)

    def test_selection_switch_cancels_pending_drag_without_editing_either_shot(self):
        original=deepcopy(self.workspace.project)
        self.drag(0,80,finish=False)
        self.workspace.select_program_range(4,7)
        self.assertIsNone(self.panel._placement_drag)
        self.assertEqual(self.workspace.project,original)
        self.assertEqual(len(self.workspace.undo_stack),0)

    def test_disjoint_selected_shots_do_not_modify_middle_shot(self):
        w=self.workspace
        w._set_timeline_selection({('program',(0.,4.)),('program',(7.,10.))})
        self.panel.placement_scale.set('50');self.panel.apply_placement()
        self.assertEqual(placement_at(w.project,1,1),(0,0,.5))
        self.assertEqual(placement_at(w.project,2,5),(0,0,1))
        self.assertEqual(placement_at(w.project,1,8),(0,0,.5))


class RealtimePlacementTests(unittest.TestCase):
    def controller(self):
        c=Mock()
        c.command.side_effect=lambda cmd: ({'w':1920,'h':1080} if cmd==['get_property','osd-dimensions']
                                           else {'aspect':16/9} if cmd==['get_property','video-out-params'] else None)
        return c

    def test_uniform_updates_do_not_load_seek_pause_or_install_filters(self):
        c=self.controller();engine=RealtimePlacement()
        engine.apply(c,(.25,0,.5),1920,1080)
        commands=[v.args[0] for v in c.command.call_args_list]
        self.assertTrue(all(v[0] in ('get_property','set_property') for v in commands))
        values={v[1]:v[2] for v in commands if v[0]=='set_property'}
        self.assertEqual(values['video-zoom'],-1)
        self.assertEqual(values['video-pan-x'],.5)
        c.command.reset_mock();engine.apply(c,(.3,0,.5),1920,1080)
        self.assertEqual(c.command.call_args_list[0].args[0],['set_property','video-pan-x',.6])

    def test_identity_is_free_and_leaving_adjusted_shot_resets_the_gpu_rectangle(self):
        c=self.controller();engine=RealtimePlacement()
        engine.apply(c,(0,0,1),1920,1080);c.command.assert_not_called()
        engine.apply(c,(.2,-.1,2),1920,1080)
        c.command.reset_mock();engine.apply(c,(0,0,1),1920,1080)
        self.assertIn(['set_property','video-pan-x',0.],[v.args[0] for v in c.command.call_args_list])

    def test_portrait_source_moves_by_canvas_pixels_instead_of_source_width(self):
        c=self.controller();c.command.side_effect=lambda cmd:({'w':1920,'h':1080} if cmd[-1]=='osd-dimensions' else {'aspect':.5})
        RealtimePlacement().apply(c,(.1,0,1),1920,1080)
        commands=[v.args[0] for v in c.command.call_args_list]
        self.assertIn(['set_property','video-pan-x',192/540],commands)


class PlacementPlayerTests(unittest.TestCase):
    from tests.test_multicam_player import MulticamPlayerTests as Fixture
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    _wait=Fixture._wait
    _state=Fixture._state
    _configure=Fixture._configure

    def test_project_changes_and_live_drafts_keep_video_and_audio_decoders(self):
        from tests.test_multicam_player import MulticamPlayerTests
        p=MulticamPlayerTests._project()
        p.cuts=[Cut(0,1),Cut(4,1)]
        with patch('video_stack_player.StackPreview',side_effect=AssertionError('single picture must not compose')):
            self._configure(p);self.player.submit('seek',time=1)
            self._state(lambda state:state['time']==1)
            before=len(self.trace.snapshot())
            changed=deepcopy(p);set_program_placement(changed,0,4,dict(x=.2,scale=.5))
            self.player.submit('configure',project=changed)
            self._wait(lambda:self.player._project==changed)
            self.player.submit('placement_preview',scope=('program',((0,4),)),value=dict(x=.3,scale=.5))
            self._wait(lambda:self.player._placement_draft is not None)
            after=self.trace.snapshot()[before:]
            self.assertFalse([c for c in after if c[0]=='load'])
            commands=[c[2] for c in after if c[0]=='command']
            self.assertFalse([c for c in commands if c[0] in ('loadfile','seek','vf')])
            self.assertFalse([c for c in commands if c==('set_property','pause',True)])
            self.assertIn(('set_property','video-pan-x',.6),commands)
            self.assertFalse(self.player._stack_enabled)
            self.player.submit('placement_preview',value=None)
            self._wait(lambda:self.player._placement_draft is None)


class ShotPlacementExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_video_placement import PlacementMediaTests
        PlacementMediaTests.setUpClass()
        cls.fixture=PlacementMediaTests()

    @classmethod
    def tearDownClass(cls):
        from tests.test_video_placement import PlacementMediaTests
        PlacementMediaTests.tearDownClass()

    def test_exported_picture_changes_only_inside_selected_shot(self):
        import subprocess
        import numpy as np
        from exporter_core import ExportSettings,Capabilities
        from multicam_core import render_multicam
        f=self.fixture;p=f.project()
        p.multicam_outer_order=[];p.video_stack_enabled=False;p.clips=p.clips[:1];p.cuts=[Cut(0,1)]
        original=deepcopy(p.clips)
        set_program_placement(p,0,1,dict(x=.25,scale=.5))
        output=f.folder/'shot-only.mp4'
        settings=ExportSettings(codec='libx264',preset='ultrafast',quality=0,resolution='timeline',
                                audio='none',decode_mode='cpu',bit_depth='8-bit')
        render_multicam(p,output,f.ffmpeg,f.ffprobe,settings,
            Capabilities(ffmpeg_path=f.ffmpeg,ffprobe_path=f.ffprobe),lambda cmd,cb:f.run_encoder(cmd))
        for second,placed in ((.5,True),(1.5,False)):
            result=subprocess.run([f.ffmpeg,'-v','error','-ss',str(second),'-i',str(output),
                '-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],check=True,capture_output=True)
            image=np.frombuffer(result.stdout,np.uint8).reshape(54,96,3)
            self.assertGreater(image[27,72,2],230)
            self.assertLess(image[27,12,2],8) if placed else self.assertGreater(image[27,12,2],230)
        self.assertEqual(p.clips,original)
