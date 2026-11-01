"""Bounded-memory audio processing and atomic, content-addressed preview caches."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from audio_mixer import enabled, strip, mix_cache_path, mix_signature, validate_settings, project_sample_rate
from timeline_audio import AudioSource, audio_sources, probe_audio_sources, build_audio_mix_args
from exporter_core import ExportCancelled

FLAGS=0x08000000 if os.name=='nt' else 0

def worker_command(request,result):
    if getattr(sys,'frozen',False): return [sys.executable,'--audio-plugin-worker',str(request),str(result)]
    return [sys.executable,str(Path(__file__).with_name('audio_plugin_worker.py')),str(request),str(result)]

def checked_run(command,cancel=None,timeout=3600):
    # stderr is a file so a broken plug-in/decoder cannot fill a pipe and deadlock.
    with tempfile.TemporaryFile() as error:
        process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=error,creationflags=FLAGS)
        deadline=time.monotonic()+timeout
        while process.poll() is None:
            if (cancel is not None and cancel.is_set()) or time.monotonic()>deadline:
                process.kill();process.wait()
                if cancel is not None and cancel.is_set(): raise ExportCancelled('调音已取消')
                raise ValueError('音频插件响应超时，已停止本次处理。')
            time.sleep(.05)
        if process.returncode:
            error.seek(0);details=error.read()[-2500:].decode('utf-8','replace')
            raise ValueError('音频处理失败：'+details)

def run_plugin(request,folder,cancel=None):
    req=Path(folder)/'request.json';result=Path(folder)/'result.json'
    req.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
    if result.exists(): result.unlink()
    try: checked_run(worker_command(req,result),cancel)
    except ValueError:
        if result.exists():
            data=json.loads(result.read_text(encoding='utf-8'))
            if data.get('error'): raise ValueError(data['error']) from None
        raise
    data=json.loads(result.read_text(encoding='utf-8'))
    if not data.get('ok'): raise ValueError(data.get('error','插件处理失败'))
    return data

def render_sources(sources,start,duration,output,ffmpeg,rate,cancel):
    inputs,graph,label=build_audio_mix_args(sources,start,duration,sample_rate=rate,clip_sum=False)
    command=[str(ffmpeg),'-hide_banner','-nostdin','-y','-v','error',*inputs,
             '-filter_complex',graph,'-map',label,'-vn','-ar',str(rate),'-ac','2',
             '-c:a','pcm_f32le','-rf64','auto','-t',f'{duration:.9f}',str(output)]
    checked_run(command,cancel)

def prepare_mix(project,ffmpeg,ffprobe,*,cancel=None,progress=None,sample_rate=None):
    sample_rate=sample_rate or project_sample_rate(project)
    validate_settings(project.audio_mixer)
    if not enabled(project): raise ValueError('尚未启用调音')
    if project.duration<=0: raise ValueError('请先导入音频或视频素材。')
    destination=mix_cache_path(project,sample_rate)
    if destination.is_file() and destination.stat().st_size>80: return str(destination)
    signature=mix_signature(project,sample_rate)
    report=progress or (lambda _p,_s:None)
    sources,_=probe_audio_sources(ffprobe,audio_sources(project),cancel)
    tracks=sorted({source.track for source in sources})
    solo=any(strip(project,t)['solo'] for t in tracks)
    tracks=[t for t in tracks if not strip(project,t)['muted'] and (not solo or strip(project,t)['solo'])]
    with tempfile.TemporaryDirectory(prefix='mix-',dir=destination.parent) as folder:
        root=Path(folder);processed=[]
        for index,track in enumerate(tracks):
            report(index/max(1,len(tracks)+1)*100,f'正在处理 A{track} 音轨')
            dry=root/f'track-{track}-dry.wav';wet=root/f'track-{track}.wav'
            render_sources([s for s in sources if s.track==track],0,project.duration,dry,ffmpeg,sample_rate,cancel)
            run_plugin(dict(action='render',input=str(dry),output=str(wet),config=strip(project,track)),root,cancel)
            dry.unlink()
            processed.append(AudioSource(track,str(wet),0.,project.duration))
        report(85.,'正在处理总线与峰值')
        dry=root/'master-dry.wav';wet=root/'master.wav'
        render_sources(processed,0,project.duration,dry,ffmpeg,sample_rate,cancel)
        stats=run_plugin(dict(action='render',input=str(dry),output=str(wet),config=strip(project)),root,cancel)
        if cancel is not None and cancel.is_set(): raise ExportCancelled('调音已取消')
        if signature!=mix_signature(project,sample_rate): raise ValueError('处理期间原始素材或插件发生变化，请重试。')
        # Another worker may have published the same immutable result in parallel.
        os.replace(wet,destination)
        destination.with_suffix('.json').write_text(json.dumps(stats),encoding='utf-8')
    report(100.,'调音完成，试听与导出已使用相同音频')
    return str(destination)
