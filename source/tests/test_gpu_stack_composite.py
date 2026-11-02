"""GPU stack acceptance gates, exact geometry and source-clock invariants."""
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clip_transform import ClipTransform
from creative_core import ColorGrade
from exporter_core import Capabilities, ExportSettings, MediaInfo
from gpu_export import GpuExportUnsupported
from gpu_stack_composite import build_gpu_stack_composite_command, framing_geometry
from multicam_core import CameraClip, Cut, MulticamProject
from video_stack_render import StackLayer, StackSegment


class GpuCompositeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='gpu_composite_plan_')
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.project=MulticamProject(clips=[CameraClip('a',1,'a.mp4',0,2),CameraClip('b',2,'b.mp4',0,2)],
                                    cuts=[Cut(0,1)],fps=30,master_duration=2,
                                    video_stack_enabled=True,video_stack_order=[1,2])
        self.segment=StackSegment(7/30,8/30,8,tuple(StackLayer(i,p,p,7/30,0) for i,p in ((1,'a.mp4'),(2,'b.mp4'))))
        self.media={p:MediaInfo(p,width=3840,height=2160,video_codec='hevc',pixel_format='yuv420p10le',
                              bit_depth=10,color_primaries='bt709',color_transfer='bt709',color_space='bt709',color_range='tv')
                    for p in ('a.mp4','b.mp4')}
        self.settings=ExportSettings(codec='hevc_nvenc',decode_mode='cuda')
        self.caps=Capabilities(cuda_decode=True)

    def build(self,**kwargs):
        return build_gpu_stack_composite_command(self.segment,self.project,['ffmpeg','-y'],self.settings,self.caps,
            1920,1080,'yuv420p10le','30',['-c:v','hevc_nvenc'],self.folder/'out.mkv',
            media_by_path=self.media,source_frame_rates={p:Fraction(25) for p in self.media},
            gpu_ffmpeg='ffmpeg',shader_dir=self.folder,**kwargs)

    def test_failed_runtime_gate_never_claims_gpu_support(self):
        with patch('gpu_resident.supported',return_value=False),self.assertRaises(GpuExportUnsupported):self.build()

    def test_unknown_hdr_or_mixed_ranges_require_reference_path(self):
        with patch('gpu_resident.supported',return_value=True):
            for change in ({'color_range':'pc'},{'color_transfer':'smpte2084'},{'color_primaries':None}):
                with self.subTest(change=change):
                    original=self.media['a.mp4'];self.media['a.mp4']=replace(original,**change)
                    with self.assertRaises(GpuExportUnsupported):self.build()
                    self.media['a.mp4']=original

    def test_subtitle_and_unverified_nested_alpha_are_not_dropped(self):
        with patch('gpu_resident.supported',return_value=True):
            with self.assertRaises(GpuExportUnsupported):self.build(ass_path='captions.ass')
            layer=replace(self.segment.layers[0],has_alpha=True,programme=True)
            self.segment=replace(self.segment,layers=(layer,self.segment.layers[1]))
            with self.assertRaises(GpuExportUnsupported):self.build()

    def test_frame_clock_and_hardware_frame_ancestry(self):
        with patch('gpu_resident.supported',return_value=True):command,backend,_,prefix=self.build()
        self.assertEqual(backend,'gpu_resident_composite')
        self.assertEqual(prefix,1)
        self.assertEqual([command[i+1] for i,v in enumerate(command) if v=='-ss'],['0.200000000','0.200000000'])
        graph=command[command.index('-filter_complex')+1]
        self.assertNotIn('hwdownload',graph)
        self.assertIn('trim=start_frame=1:end_frame=9',graph)
        self.assertIn('scale_cuda=w=1920:h=1080',graph)
        self.assertIn('vulkan=okk_vk@okk_cu',command)
        self.assertNotIn('-pix_fmt',command)

    def test_fit_identity_rounds_before_divisibility_but_active_fit_truncates(self):
        info=replace(self.media['a.mp4'],width=800,height=534)
        self.assertEqual(framing_geometry(info,ClipTransform(),320,180)['pos_w'],270)
        self.assertEqual(framing_geometry(info,ClipTransform(x=.2),320,180)['pos_w'],268)

    def test_non_square_pixels_cannot_stretch_silently(self):
        info=replace(self.media['a.mp4'],sample_aspect_ratio='4:3')
        with self.assertRaises(GpuExportUnsupported):framing_geometry(info,ClipTransform(),1920,1080)

    def test_unsupported_lut_falls_back_before_export(self):
        self.project.global_grade=ColorGrade(lut_path=str(self.folder/'missing.cube'))
        with patch('gpu_resident.supported',return_value=True),self.assertRaises(GpuExportUnsupported):self.build()


if __name__=='__main__':unittest.main()
