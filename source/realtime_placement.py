"""Move/zoom the existing GPU video rectangle without a filter or seek."""
from __future__ import annotations
import math
import time
from video_placement import placement_values


class RealtimePlacement:
    def __init__(self): self._states={}

    def apply(self,controller,value,width,height,*,bypass=False,key=None,force_geometry=False,framed=False):
        x,y,scale=(0.,0.,1.) if bypass else placement_values(dict(zip(('x','y','scale'),value)))
        key=(key,width,height,framed)
        previous=self._states.get(id(controller))
        if (x,y,scale)==(0.,0.,1.) and previous is None: return False
        now=time.monotonic()
        geometry=previous['geometry'] if previous else None
        if (force_geometry or geometry is None or previous['key']!=key or now-previous['checked']>.25):
            def prop(name):
                try:
                    result=controller.command(['get_property',name])
                    return result if isinstance(result,dict) else {}
                except (RuntimeError,OSError): return {}
            osd=prop('osd-dimensions');video=prop('video-dec-params') or prop('video-out-params')
            ow,oh=max(1,float(osd.get('w') or width)),max(1,float(osd.get('h') or height))
            ratio=min(ow/width,oh/height);cw,ch=width*ratio,height*ratio
            dar=float(video.get('aspect') or ((video.get('dw') or video.get('w') or width)/
                                            (video.get('dh') or video.get('h') or height)))
            if framed: dar=width/height
            vw,vh=(ch*dar,ch) if dar<cw/ch else (cw,cw/dar)
            geometry=(ow,oh,cw,ch,max(1,vw),max(1,vh))
            checked=now
        else: checked=previous['checked']
        ow,oh,cw,ch,vw,vh=geometry
        outside=(abs(x*cw)>=cw/2+vw*scale/2 or abs(y*ch)>=ch/2+vh*scale/2)
        values={'video-zoom':math.log2(scale),'video-pan-x':x*cw/(vw*scale),
                'video-pan-y':y*ch/(vh*scale),
                # MPV retains a minimum one-pixel rectangle even completely
                # offscreen. Keep that guard pixel black without disabling
                # or restarting the video track.
                'brightness':-100. if outside else 0.,
                'video-margin-ratio-left':(ow-cw)/(2*ow),
                'video-margin-ratio-right':(ow-cw)/(2*ow),
                'video-margin-ratio-top':(oh-ch)/(2*oh),
                'video-margin-ratio-bottom':(oh-ch)/(2*oh)}
        if (x,y,scale)==(0.,0.,1.): values={name:0. for name in values}
        old=previous['values'] if previous else {}
        changed=False
        for name,number in values.items():
            if name not in old or abs(old[name]-number)>1e-9:
                controller.command(['set_property',name,number]);changed=True
        if (x,y,scale)==(0.,0.,1.): self._states.pop(id(controller),None)
        else:
            self._states[id(controller)]=dict(controller=controller,values=values,geometry=geometry,
                                              checked=checked,key=key)
        return changed

    def forget(self,controller): self._states.pop(id(controller),None)
    def close(self): self._states.clear()
