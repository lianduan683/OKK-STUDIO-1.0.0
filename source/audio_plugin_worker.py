"""Isolated, main-thread VST3 host; no third-party plugin executes in Tk."""
from __future__ import annotations
import base64
import json
import math
from pathlib import Path
import sys
import traceback

def load_insert(item):
    from pedalboard import load_plugin
    path=Path(item['path'])
    if not path.exists(): raise ValueError('插件已丢失，请重新链接：'+str(path))
    plugin=load_plugin(str(path),initialization_timeout=15.)
    if not plugin.is_effect: raise ValueError('请选择调音效果插件，不能使用合成器乐器。')
    if item.get('state'): plugin.raw_state=base64.b64decode(item['state'],validate=True)
    return plugin

def make_board(config):
    import pedalboard as p
    from audio_mixer import EQ_BANDS
    plugins=[]
    if config['highpass']>0: plugins.append(p.HighpassFilter(cutoff_frequency_hz=config['highpass']))
    if config['gate']: plugins.append(p.NoiseGate(threshold_db=config['gate_threshold'],ratio=4.,attack_ms=5.,release_ms=150.))
    if config['eq_enabled']:
        plugins.extend(p.PeakFilter(cutoff_frequency_hz=f,gain_db=g,q=.8) for f,g in zip(EQ_BANDS,config['eq']) if g)
    if config['compressor']: plugins.append(p.Compressor(threshold_db=config['threshold'],ratio=config['ratio'],attack_ms=config['attack'],release_ms=config['release']))
    if config['reverb']: plugins.append(p.Reverb(room_size=.4,damping=.5,wet_level=config['reverb_mix'],dry_level=1-config['reverb_mix']))
    for item in config['plugins']:
        if item.get('enabled',True): plugins.append(load_insert(item))
    plugins.append(p.Gain(gain_db=config['gain_db']))
    if config['limiter']: plugins.append(p.Limiter(threshold_db=config['ceiling'],release_ms=config['release']))
    return p.Pedalboard(plugins)

def process_file(request):
    import numpy as np
    from pedalboard.io import AudioFile
    from audio_mixer import strip_defaults, validate_settings
    config=strip_defaults();config.update(request['config'])
    validate_settings({'strips':{'master':config}})
    board=make_board(config)
    written=0; peak=0.; squares=0.; count=0
    with AudioFile(request['input']) as src:
        target=src.frames; rate=src.samplerate
        # VST3 effects (notably Pro-L) change their reported latency when first
        # prepared at the project's sample rate. Initialise with silence, then
        # reset so compensation sees the final latency before real samples.
        board(np.zeros((2,65536),dtype=np.float32),rate,buffer_size=2048,reset=True)
        board.reset()
        with AudioFile(request['output'],'w',samplerate=rate,num_channels=2,bit_depth=32) as dest:
            def emit(chunk):
                nonlocal written,peak,squares,count
                chunk=chunk[:,:max(0,target-written)]
                if not chunk.size: return
                if chunk.shape[0]==1: chunk=np.repeat(chunk,2,axis=0)
                if config['muted']: chunk[:]=0
                pan=config['pan']
                chunk[0]*=min(1.,1.-pan);chunk[1]*=min(1.,1.+pan)
                if not np.isfinite(chunk).all(): raise ValueError('插件输出无效音频，已停止处理。')
                peak=max(peak,float(np.max(np.abs(chunk))))
                squares+=float(np.sum(chunk.astype(np.float64)**2));count+=chunk.size
                dest.write(chunk);written+=chunk.shape[1]
            while src.tell()<target:
                emit(board(src.read(65536),rate,buffer_size=2048,reset=False))
            # Pedalboard compensates plugin lookahead by withholding its startup
            # samples. Flush without reset, then trim to the exact timeline length.
            for _ in range(128):
                if written>=target: break
                emit(board(np.zeros((2,65536),dtype=np.float32),rate,buffer_size=2048,reset=False))
            if written!=target: raise ValueError('插件延迟无法补偿，未保存不完整的音频。')
    return dict(frames=written,sample_rate=rate,peak_db=20*math.log10(max(peak,1e-12)),
                rms_db=10*math.log10(max(squares/max(1,count),1e-24)))

def main(argv=None):
    args=sys.argv[1:] if argv is None else argv
    request_path,result_path=map(Path,args[-2:])
    try:
        request=json.loads(request_path.read_text(encoding='utf-8'))
        if request['action'] in {'editor','probe'}:
            plugin=load_insert(request['plugin'])
            if request['action']=='editor': plugin.show_editor()
            result=dict(name=plugin.name,version=plugin.version,latency=plugin.reported_latency_samples,
                        state=base64.b64encode(plugin.raw_state).decode('ascii'))
        elif request['action']=='render': result=process_file(request)
        else: raise ValueError('未知音频任务')
        result_path.write_text(json.dumps({'ok':True,**result},ensure_ascii=False),encoding='utf-8')
        return 0
    except Exception as exc:
        result_path.write_text(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False),encoding='utf-8')
        return 1

if __name__=='__main__': raise SystemExit(main())
