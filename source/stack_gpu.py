"""D3D11 straight-alpha layer composition; no normal-playback GPU readback.

FFmpeg supplies independently treated RGBA layers in a bounded atlas. The
pixel shader blends them bottom to top. Window scaling and final programme
grading also run on the GPU, without a viewport-sized Python/Tk image.
"""
from __future__ import annotations
import ctypes as C
import math

from gpu_d3d11 import (D3D11Compositor, VideoTexture, Com, TexDesc, Sample,
    BufferDesc, Mapped, Viewport, P, U, F, HR, call, check)


SHADER = r'''
Texture2D<float4> Atlas : register(t0);
SamplerState VideoSampler : register(s0);
cbuffer LayerState : register(b0) { float4 Grid; float4 Cell; };
struct VertexOutput { float4 position : SV_Position; float2 uv : TEXCOORD0; };
float4 PSMain(VertexOutput input) : SV_Target {
    float3 colour = float3(0, 0, 0);
    // Each cell is a straight-alpha, full-canvas layer. Sample at pixel
    // centres and clamp within its cell to prevent atlas-edge colour leaks.
    float2 pixel = clamp(input.uv * Cell.xy, 0.5, Cell.xy - 0.5);
    [loop] for (int i = 0; i < (int)Grid.x; ++i) {
        float2 origin = float2(i % (int)Grid.y, i / (int)Grid.y) * Cell.xy;
        float4 layer = Atlas.SampleLevel(VideoSampler, (origin + pixel) / Cell.zw, 0);
        colour = layer.rgb * layer.a + colour * (1 - layer.a);
    }
    return float4(colour, 1);
}
'''


def compile_pixel(source):
    """Compile our own small shader with the Windows system compiler."""
    compiler = C.WinDLL('d3dcompiler_47.dll').D3DCompile
    compiler.argtypes = [P, C.c_size_t, C.c_char_p, P, P, C.c_char_p, C.c_char_p,
                         U, U, C.POINTER(P), C.POINTER(P)]
    compiler.restype = HR
    code, errors = Com(), Com()
    raw = source.encode('ascii')
    try:
        result = compiler(raw, len(raw), b'okk_stack_alpha', None, None,
                          b'PSMain', b'ps_5_0', 1 << 15, 0,
                          C.byref(code.value), C.byref(errors.value))
        if result < 0:
            message = C.string_at(call(errors.value, 3, P, []),
                call(errors.value, 4, C.c_size_t, [])).decode('utf-8', 'replace') if errors.value else ''
            raise RuntimeError('GPU Alpha shader: ' + message)
        return C.string_at(call(code.value, 3, P, []), call(code.value, 4, C.c_size_t, []))
    finally:
        code.release(); errors.release()


def atlas_layout(count, width, height):
    if type(count) is not int or not 1 <= count <= 36:
        raise ValueError('GPU 图层数量无效。')
    columns = min(count, math.ceil(math.sqrt(count)))
    rows = math.ceil(count/columns)
    return dict(count=count, columns=columns, rows=rows,
                cell_width=width, cell_height=height)


class StackGpuPresenter:
    """Tk-thread-owned native child; one retained atlas and output texture."""
    def __init__(self, parent, runtime=None, *, width=640, height=360):
        from gpu_live_session import runtime_directory
        self.gfx = None
        self.atlas = Com(); self.atlas_srv = Com(); self.pixel = Com(); self.constant = Com()
        self.atlas_size = None
        self.presented = 0
        self.readbacks = 0
        try:
            self.gfx = D3D11Compositor(runtime_directory(runtime), parent, width, height, source_count=1)
            code = compile_pixel(SHADER)
            check(call(self.gfx.device.value, 15, HR, [P,C.c_size_t,P,C.POINTER(P)],
                       code,len(code),None,C.byref(self.pixel.value)), 'Create alpha shader')
            desc = BufferDesc(32,2,4,0x10000,0,0)
            check(call(self.gfx.device.value,3,HR,[C.POINTER(BufferDesc),P,C.POINTER(P)],
                       C.byref(desc),None,C.byref(self.constant.value)), 'Create layer constants')
            # Compile the driver's first draw before the master clock starts,
            # rather than stalling on the first arriving video frame.
            self.present(bytes((0,0,0,255))*4,2,2)
            self.presented = 0
            self.set_visible(False)
        except Exception:
            self.close(); raise

    @property
    def adapter(self): return self.gfx.adapter_name

    def set_visible(self, visible):
        if self.gfx: self.gfx.set_visible(visible)

    def _upload(self, width, height, rgba):
        gfx = self.gfx
        if self.atlas_size != (width,height):
            self.atlas_srv.release(); self.atlas.release()
            desc=TexDesc(width,height,1,1,28,Sample(1,0),0,8,0,0)  # RGBA8_UNORM
            check(call(gfx.device.value,5,HR,[C.POINTER(TexDesc),P,C.POINTER(P)],
                       C.byref(desc),None,C.byref(self.atlas.value)), 'Create layer atlas')
            check(call(gfx.device.value,7,HR,[P,P,C.POINTER(P)],self.atlas.value,None,
                       C.byref(self.atlas_srv.value)), 'Create atlas SRV')
            self.atlas_size=(width,height)
        # bytes stays alive through UpdateSubresource, which copies it before
        # returning. No ndarray copy or CPU window-size expansion is made.
        call(gfx.context.value,48,None,[P,U,P,P,U,U],self.atlas.value,0,None,rgba,width*4,0)

    def present(self, rgba, width, height, layout=None, grades=None):
        layout = layout or atlas_layout(1,width,height)
        count, columns, rows = (layout[key] for key in ('count','columns','rows'))
        cw,ch = (layout[key] for key in ('cell_width','cell_height'))
        if (not all(type(v) is int and v > 0 for v in (count,columns,rows,cw,ch,width,height))
                or count>36 or columns*rows<count or (columns*cw,rows*ch)!=(width,height)
                or max(width,height)>16384 or not isinstance(rgba,bytes) or len(rgba)!=width*height*4):
            raise ValueError('GPU 图层画面结构无效。')
        gfx=self.gfx
        gfx.pump_messages()
        gfx.resize(*gfx.client_size())
        self._upload(width,height,rgba)
        context=gfx.context.value
        state=(F*8)(count,columns,rows,0,cw,ch,width,height)
        mapped=Mapped()
        check(call(context,14,HR,[P,U,U,U,C.POINTER(Mapped)],self.constant.value,0,4,0,C.byref(mapped)), 'Map layer constants')
        C.memmove(mapped.data,C.byref(state),32)
        call(context,15,None,[P,U],self.constant.value,0)
        call(context,110,None,[])
        target=(P*1)(gfx.videos[0].rtv.value)
        call(context,33,None,[U,C.POINTER(P),P],1,target,None)
        # Fit the source canvas into the actual monitor, including pillarbox.
        gfx.clear_texture(gfx.videos[0],(0,0,0,1))
        scale=min(gfx.width/cw,gfx.height/ch)
        w,h=cw*scale,ch*scale
        viewport=Viewport((gfx.width-w)/2,(gfx.height-h)/2,w,h,0,1)
        call(context,44,None,[U,C.POINTER(Viewport)],1,C.byref(viewport))
        call(context,43,None,[P],gfx.raster.value)
        call(context,17,None,[P],None)
        call(context,24,None,[U],4)
        call(context,11,None,[P,P,U],gfx.vertex.value,None,0)
        call(context,9,None,[P,P,U],self.pixel.value,None,0)
        textures=(P*1)(self.atlas_srv.value)
        call(context,8,None,[U,U,C.POINTER(P)],0,1,textures)
        call(context,10,None,[U,U,C.POINTER(P)],0,1,(P*1)(gfx.sampler.value))
        call(context,16,None,[U,U,C.POINTER(P)],0,1,(P*1)(self.constant.value))
        call(context,13,None,[U,U],3,0)
        call(context,8,None,[U,U,C.POINTER(P)],0,1,(P*1)())
        gfx.draw(0,0,-1,0,grades,grades)
        gfx.present()
        self.set_visible(True)
        self.presented+=1

    def close(self):
        for name in ('atlas_srv','atlas','pixel','constant'):
            value=getattr(self,name,None)
            if value: value.release()
        if self.gfx: self.gfx.close(); self.gfx=None
