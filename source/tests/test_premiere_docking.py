"""Real Tk regressions for tab insertion, drag feedback and caption docking."""
from __future__ import annotations

import copy
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_docking as fixture
from dock_feedback import tab_insertion


class PremiereDockingTests(unittest.TestCase):
    setUp=fixture.DockingTests.setUp
    tearDown=fixture.DockingTests.tearDown
    update=fixture.DockingTests.update
    stable_ids=fixture.DockingTests.stable_ids
    change=fixture.DockingTests.change

    def drag(self,key,x,y,whole=False):
        header=self.headers[key]
        self.manager._press(SimpleNamespace(x_root=header.winfo_rootx()+10,
                            y_root=header.winfo_rooty()+5,widget=header),key,whole_group=whole)
        self.manager._motion(SimpleNamespace(x_root=x,y_root=y))

    def release(self,x,y):
        self.manager._release(SimpleNamespace(x_root=x,y_root=y));self.update()

    def test_detached_preview_tracks_pointer_then_escape_from_focused_content_cancels(self):
        before=copy.deepcopy(self.manager.serialize())
        self.drag('source',7000,7000)
        self.update()
        preview=self.manager._drag_preview.window
        self.assertTrue(preview.winfo_viewable())
        first=(preview.winfo_rootx(),preview.winfo_rooty())
        self.manager._motion(SimpleNamespace(x_root=7100,y_root=7040));self.update()
        self.assertEqual((preview.winfo_rootx()-first[0],preview.winfo_rooty()-first[1]),(100,40))
        self.videos['source'].focus_force();self.update()
        self.videos['source'].event_generate('<Escape>');self.update()
        self.assertIsNone(self.manager._drag)
        self.assertIsNone(self.manager._drag_preview)
        self.assertEqual(before,self.manager.serialize());self.stable_ids()

    def test_tab_strip_is_insertion_target_while_content_top_is_split_target(self):
        book=self.notebook;x,y=book.winfo_rootx(),book.winfo_rooty()
        target=self.manager.drop_target(x+12,y+10)
        self.assertTrue(target[1].startswith('tab:'))
        target=self.manager.drop_target(x+book.winfo_width()//2,y+45)
        self.assertEqual(target[1],'top')
        self.assertIsNone(tab_insertion(book,x+12,y+book.winfo_height()//2,self.manager._px))

    def test_drag_tab_to_first_position_reorders_without_losing_selection_or_handles(self):
        self.manager.dock('source','media');self.update()
        node=self.manager._find('media')[1];book=self.manager._groups[node['id']][1]
        x,y=book.winfo_rootx()+4,book.winfo_rooty()+10
        self.drag('cuts',x,y);self.release(x,y)
        self.assertEqual(self.manager._find('cuts')[1]['tabs'],['cuts','media','source'])
        self.assertEqual(self.manager._find('cuts')[1]['selected'],'cuts');self.stable_ids()
        saved=self.manager.serialize();self.manager.restore(saved);self.update()
        self.assertEqual(self.manager._find('cuts')[1]['tabs'],['cuts','media','source'])

    def test_drag_first_tab_past_last_tab_appends_to_same_group(self):
        book=self.notebook
        x,y=book.winfo_rootx()+book.winfo_width()-10,book.winfo_rooty()+10
        self.drag('media',x,y);self.release(x,y)
        self.assertEqual(self.manager._find('media')[1]['tabs'],['cuts','media']);self.stable_ids()

    def test_floating_window_does_not_obscure_dock_targets_beneath_it(self):
        target=self.widgets['program']
        x,y=target.winfo_rootx()+target.winfo_width()//2,target.winfo_rooty()+target.winfo_height()//2
        self.manager.float_panel('source',f'400x300+{x-120}+{y-100}');self.update()
        target=self.widgets['program']
        x,y=target.winfo_rootx()+target.winfo_width()//2,target.winfo_rooty()+target.winfo_height()//2
        self.drag('source',x,y)
        self.assertEqual(self.manager._drag_target(x,y,'source')[:2],('program','center'))
        self.release(x,y)
        self.assertEqual(self.manager.group_members('source'),('program','source'));self.stable_ids()

    def test_empty_monitor_home_can_receive_its_own_floating_panel(self):
        self.manager.float_panel('source','400x300+7800+7800');self.update()
        target=next(target for target in self.manager._empty_targets() if target[0]=='source')
        left,top,width,height=target[2];x,y=left+width//2,top+height//2
        self.drag('source',x,y);self.release(x,y)
        self.assertEqual(self.manager.focus_home('source'),'source')
        self.assertTrue(self.manager.home_has_content('source'))
        self.assertNotEqual(self.widgets['source'].winfo_manager(),'wm');self.stable_ids()

    def test_whole_group_can_split_beside_another_panel(self):
        self.manager.float_group('media','500x360+7800+7800');self.update()
        target=self.widgets['program'];x,y=target.winfo_rootx()+target.winfo_width()-3,target.winfo_rooty()+target.winfo_height()//2
        self.drag('media',x,y,whole=True);self.release(x,y)
        node=self.manager.serialize()['roots']['program']
        self.assertEqual(node['orient'],'horizontal')
        self.assertEqual(node['children'][1]['tabs'],['media','cuts']);self.stable_ids()

    def test_dragging_an_existing_float_to_free_space_moves_it(self):
        self.manager.float_panel('source','400x300+7800+7800');self.update()
        self.drag('source',7200,7100);self.release(7200,7100)
        self.assertEqual(self.manager._float_geometry('source'),'400x300+7170+7088');self.stable_ids()

    def test_negative_screen_coordinates_survive_float_and_layout_restore(self):
        self.manager.float_panel('source','400x300+-900+100');self.update()
        geometry=self.manager._float_geometry('source')
        self.assertIn('+-900+',geometry)
        with patch.object(self.manager,'_screen_bounds',return_value=(-2560,0,7680,1440)):
            self.assertEqual(self.manager._restore_geometry('source',geometry,1),'400x300+-900+100')
        state=self.manager.serialize();self.manager.restore_defaults()
        with patch.object(self.manager,'_screen_bounds',return_value=(-2560,0,7680,1440)):
            self.manager.restore(state)
        self.update();self.assertEqual(self.manager._float_geometry('source'),geometry);self.stable_ids()

    @unittest.skipUnless(os.name=='nt','Windows caption observer')
    def test_native_caption_observer_queues_motion_and_release_into_docking(self):
        self.manager.float_panel('source','400x300+7800+7800');self.update()
        observer=self.manager._native_drag
        hwnd=int(self.widgets['source'].tk.call('wm','frame',self.widgets['source']),0)
        self.assertEqual(observer.windows[0][2],hwnd)
        target=self.widgets['program'];x,y=target.winfo_rootx()+target.winfo_width()-3,target.winfo_rooty()+target.winfo_height()//2
        observer.events.put(('motion',observer.generation,'source',False,x,y))
        observer.events.put(('release',observer.generation,'source',False,x,y))
        if observer.after is not None:self.workspace.after_cancel(observer.after);observer.after=None
        observer._drain();self.update()
        self.assertEqual(self.manager.serialize()['roots']['program']['children'][1]['tabs'],['source'])
        self.assertIsNone(self.manager._drag);self.stable_ids()

    @unittest.skipUnless(os.name=='nt','Windows caption observer')
    def test_native_caption_cancellation_and_stale_events_leave_layout_unchanged(self):
        self.manager.float_panel('source','400x300+7800+7800');self.update()
        observer=self.manager._native_drag;before=self.manager.serialize()
        observer.events.put(('motion',observer.generation,'source',False,8200,8200))
        observer.events.put(('cancel',observer.generation,'source',False,8200,8200))
        observer.events.put(('release',observer.generation-1,'source',False,8200,8200))
        if observer.after is not None:self.workspace.after_cancel(observer.after);observer.after=None
        observer._drain();self.update()
        self.assertIsNone(self.manager._drag);self.assertEqual(before,self.manager.serialize())

    def test_workspace_unmap_cancels_active_drag_and_destroys_feedback(self):
        self.drag('source',7000,7000)
        self.workspace.pack_forget();self.update()
        self.assertIsNone(self.manager._drag);self.assertIsNone(self.manager._drag_preview)


if __name__=='__main__':unittest.main(verbosity=2)
