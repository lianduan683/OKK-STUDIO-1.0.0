"""Entry scrubbing: live preview, one release commit, Escape cancellation."""
import math
from project_settings import timeline_dimensions


class InspectorPlacementDragMixin:
    def _bind_placement_drag(self,entry,variable,index):
        if not hasattr(self,'_placement_drag'):
            self._placement_drag=None
            self._placement_drag_after=None
            self.bind('<Destroy>',self._destroy_placement_drag,add='+')
        entry.configure(cursor='sb_h_double_arrow')
        entry.bind('<ButtonPress-1>',lambda event:self._start_placement_drag(event,variable,index),add='+')
        entry.bind('<B1-Motion>',self._move_placement_drag)
        entry.bind('<ButtonRelease-1>',self._release_placement_drag,add='+')
        entry.bind('<Escape>',self._cancel_placement_drag,add='+')
        entry.bind('<FocusOut>',self._focus_out_placement_drag,add='+')

    def _placement_input_value(self):
        width,height=timeline_dimensions(self.ws.project,(1920,1080))
        return dict(x=self._number(self.placement_x,'水平位置')/width,
                    y=self._number(self.placement_y,'垂直位置')/height,
                    scale=self._number(self.placement_scale,'缩放')/100)

    def _start_placement_drag(self,event,variable,index):
        if str(event.widget.cget('state'))=='disabled' or not self.ws._available(): return
        scope=self.ws.video_placement_scope()
        try: initial=float(variable.get())
        except ValueError: return
        if not scope or not math.isfinite(initial): return
        self._placement_drag=dict(entry=event.widget,variable=variable,index=index,
            x=event.x_root,initial=initial,project=self.ws.project,scope=scope,active=False)

    def _move_placement_drag(self,event):
        drag=self._placement_drag
        if not drag: return
        if drag['project'] is not self.ws.project or drag['scope']!=self.ws.video_placement_scope():
            self._finish_placement_drag(cancel=True);return 'break'
        delta=event.x_root-drag['x']
        if not drag['active'] and abs(delta)<self.px(4): return 'break'
        if not drag['active']:
            drag['active']=True
            drag['entry'].selection_clear()
            drag['entry'].grab_set()
        width,height=timeline_dimensions(self.ws.project,(1920,1080))
        index=drag['index']
        speed=(.25 if index==2 else 1.)*(.1 if event.state&1 else 1.)
        low,high=(1.,400.) if index==2 else (-2*(width if index==0 else height),2*(width if index==0 else height))
        value=max(low,min(high,drag['initial']+delta*speed))
        drag['variable'].set(f'{value:.4f}'.rstrip('0').rstrip('.'))
        if self._placement_drag_after is None:
            self._placement_drag_after=self.after(25,self._preview_placement_drag)
        return 'break'

    def _preview_placement_drag(self):
        self._placement_drag_after=None
        drag=self._placement_drag
        if not drag or not drag['active']: return
        if drag['project'] is not self.ws.project or drag['scope']!=self.ws.video_placement_scope():
            self._finish_placement_drag(cancel=True);return
        self.ws.preview_video_placement(self._placement_input_value(),drag['scope'])

    def _finish_placement_drag(self,*,cancel=False,refresh=True):
        drag=self._placement_drag
        if not drag: return False
        self._placement_drag=None
        if self._placement_drag_after is not None:
            self.after_cancel(self._placement_drag_after);self._placement_drag_after=None
        if self.grab_current() is drag['entry']: drag['entry'].grab_release()
        if not drag['active']: return False
        try:
            if not cancel and drag['project'] is self.ws.project and drag['scope']==self.ws.video_placement_scope():
                self.ws.apply_video_placement(self._placement_input_value(),scope=drag['scope'])
        finally:
            self.ws.preview_video_placement(None)
            if refresh: self._refresh_placement(force=True)
        return True

    def _release_placement_drag(self,event=None):
        if self._finish_placement_drag(): return 'break'

    def _cancel_placement_drag(self,event=None):
        if self._finish_placement_drag(cancel=True): return 'break'

    def _focus_out_placement_drag(self,event=None):
        drag=self._placement_drag
        if drag and (event is None or event.widget is drag['entry']): self._finish_placement_drag()

    def _destroy_placement_drag(self,event):
        if event.widget is self: self._finish_placement_drag(cancel=True,refresh=False)
