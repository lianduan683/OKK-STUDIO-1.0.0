from __future__ import annotations
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
from pedalboard.io import AudioFile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from audio_mixer import strip_defaults, mix_signature, validate_settings, discover_plugins
from audio_mix_render import prepare_mix, run_plugin
from multicam_core import MulticamProject, CameraClip, Cut, render_multicam
from exporter_core import Capabilities, ExportSettings, ExportCancelled, find_binary
from multicam_player import MulticamPlayer

FFMPEG=find_binary('ffmpeg', Path(__file__).resolve().parents[1])
FFPROBE=find_binary('ffprobe', Path(__file__).resolve().parents[1])

class AudioMixerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='okk-audio-test-')
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch('cache_paths.cache_dir',return_value=self.root).start()
        self.samples=np.zeros((2,96000),dtype=np.float32)
        self.samples[:,24000]=.2;self.samples[:,70000]=.3
        self.source=self.root/'source.wav'
        with AudioFile(str(self.source),'w',samplerate=48000,num_channels=2,bit_depth=32) as f:f.write(self.samples)
        self.project=MulticamProject(master_audio=str(self.source),master_duration=2.,
             audio_mixer={'enabled':True,'strips':{'master':strip_defaults()}})

    def samples_at(self,path):
        with AudioFile(str(path)) as f:return f.read(f.frames)

    def render(self):return prepare_mix(self.project,FFMPEG,FFPROBE)

    def test_roundtrip_legacy_validation_and_plugin_state(self):
        config=self.project.audio_mixer['strips']['master'];config['gain_db']=-7.5
        config['plugins']=[dict(path=r'C:\test\Pro-Q.vst3',name='Pro-Q',state='YWJj',enabled=False)]
        payload=self.project.to_payload(self.root)
        self.assertEqual(payload['version'],20)
        restored=MulticamProject.from_payload(payload,self.root)
        self.assertEqual(restored.audio_mixer,self.project.audio_mixer)
        self.project.audio_mixer={};self.assertNotIn('audio_mixer',self.project.to_payload(self.root))
        for wrong in (float('nan'),100,True):
            with self.assertRaises(ValueError):validate_settings({'strips':{'master':{'gain_db':wrong}}})

    def test_gain_pan_exact_alignment_and_cache_invalidation(self):
        self.project.audio_mixer['strips']['master'].update(gain_db=-6.,pan=1.)
        path=self.render();audio=self.samples_at(path)
        self.assertEqual(audio.shape,self.samples.shape)
        np.testing.assert_array_equal(audio[0],0.)
        np.testing.assert_allclose(audio[1],self.samples[1]*10**(-6/20),atol=1e-7)
        old=mix_signature(self.project);mtime=Path(path).stat().st_mtime_ns
        self.assertEqual(self.render(),path);self.assertEqual(Path(path).stat().st_mtime_ns,mtime)
        self.project.audio_mixer['strips']['master']['gain_db']=-9.
        self.assertNotEqual(old,mix_signature(self.project));self.assertNotEqual(self.render(),path)

    def test_solo_and_muted_bus_do_not_double_audio(self):
        self.project.clips=[CameraClip('a',1,str(self.source),0,2,has_audio=True)]
        self.project.audio_track_enabled={0:True,1:True}
        self.project.audio_mixer['strips']['1']={**strip_defaults(),'solo':True}
        np.testing.assert_allclose(self.samples_at(self.render()),self.samples,atol=1e-7)
        self.project.audio_mixer['strips']['master']['muted']=True
        np.testing.assert_array_equal(self.samples_at(self.render()),0.)

    def test_missing_plugin_fails_without_publishing_wet_cache(self):
        self.project.audio_mixer['strips']['master']['plugins']=[dict(path=str(self.root/'missing.vst3'),name='Missing',state='',enabled=True)]
        with self.assertRaisesRegex(ValueError,'插件已丢失'):self.render()
        self.assertFalse(list(self.root.glob('????????????????????????????????????????????????????????????????.wav')))

    def test_cancel_does_not_publish_partial_audio(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(ExportCancelled):prepare_mix(self.project,FFMPEG,FFPROBE,cancel=cancel)

    def test_real_fabfilter_limiter_latency_state_and_output(self):
        path=Path(r'C:\Program Files\Common Files\VST3\FabFilter Pro-L 2.vst3')
        if not path.exists():self.skipTest('FabFilter not installed')
        item=dict(path=str(path),name=path.stem,state='',enabled=True)
        probe=run_plugin({'action':'probe','plugin':item},self.root)
        self.assertGreater(len(probe['state']),20);item['state']=probe['state']
        self.project.audio_mixer['strips']['master']['plugins']=[item]
        audio=self.samples_at(self.render())
        self.assertEqual(audio.shape,self.samples.shape)
        self.assertLessEqual(abs(int(np.argmax(np.abs(audio[0])))-70000),1)
        self.assertTrue(np.isfinite(audio).all())

    def test_preview_uses_processed_bus_once_and_bypass_restores_original(self):
        path=self.render();player=object.__new__(MulticamPlayer)
        player._project=self.project;player._audio_mix_path=path;player._audio_mix_bypass=False
        self.assertEqual(player._audio_spec(.5)[1:],(path,.5))
        self.assertIsNone(player._camera_audio_spec(1,.5))
        self.assertEqual(player._audio_offset,0.)
        player._audio_mix_bypass=True;player._audio_duration_path='';player._audio_duration=None
        self.assertEqual(player._audio_spec(.5)[1:],(str(self.source),.5))

    def test_real_export_pcm_equals_preview_and_copy_refuses_effects(self):
        video=self.root/'camera.mp4'
        subprocess.run([FFMPEG,'-v','error','-y','-f','lavfi','-i','color=c=blue:s=96x54:r=30:d=2',
                        '-c:v','libx264','-preset','ultrafast','-an',str(video)],check=True,capture_output=True)
        self.project.clips=[CameraClip('a',1,str(video),0,2,has_audio=False)];self.project.cuts=[Cut(0,1)]
        self.project.audio_mixer['strips']['master']['gain_db']=-6.
        preview=self.samples_at(self.render())
        settings=ExportSettings(codec='libx264',preset='ultrafast',decode_mode='cpu',audio='pcm_s24le',bit_depth='8-bit')
        output=self.root/'delivered.mov'
        def runner(command,callback):
            result=subprocess.run(command,capture_output=True,timeout=60)
            return result.returncode,result.stderr.decode('utf-8','replace')
        caps=Capabilities(ffmpeg_path=FFMPEG,ffprobe_path=FFPROBE)
        with patch('multicam_core.cache_dir',return_value=self.root):
            render_multicam(self.project,output,FFMPEG,FFPROBE,settings,caps,runner)
        decoded=self.root/'decoded.wav'
        subprocess.run([FFMPEG,'-v','error','-y','-i',str(output),'-vn','-c:a','pcm_f32le',str(decoded)],check=True,capture_output=True)
        actual=self.samples_at(decoded);self.assertEqual(actual.shape,preview.shape)
        np.testing.assert_allclose(actual,preview,atol=1.3e-7)
        settings.audio='copy'
        with self.assertRaisesRegex(ValueError,'调音已启用'):
            render_multicam(self.project,self.root/'copy.mov',FFMPEG,FFPROBE,settings,caps,runner)

if __name__=='__main__':unittest.main()
