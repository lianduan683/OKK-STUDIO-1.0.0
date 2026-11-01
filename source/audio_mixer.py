"""Versioned audio insert settings. Plugin code is only run by a child process."""
from __future__ import annotations
import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path

EQ_BANDS = (80, 200, 600, 1800, 5000, 12000)
DEFAULT_STRIP = dict(gain_db=0., pan=0., muted=False, solo=False, eq_enabled=False,
    eq=[0.] * 6, highpass=0., compressor=False, threshold=-18., ratio=3.,
    attack=10., release=120., gate=False, gate_threshold=-48.,
    reverb=False, reverb_mix=.12, limiter=False, ceiling=-1., plugins=[])

def strip_defaults():
    return copy.deepcopy(DEFAULT_STRIP)

def settings(project):
    return (project.get('audio_mixer', {}) if isinstance(project,dict) else getattr(project, 'audio_mixer', {})) or {}

def enabled(project):
    return bool(settings(project).get('enabled', False))

def strip(project, track='master'):
    value = strip_defaults()
    value.update(settings(project).get('strips', {}).get(str(track), {}))
    return copy.deepcopy(value)

def validate_settings(value):
    if not isinstance(value, dict) or set(value) - {'enabled', 'strips'}:
        raise ValueError('调音设置无效')
    if not value: return
    if type(value.get('enabled', False)) is not bool:
        raise ValueError('调音开关无效')
    strips = value.get('strips', {})
    if not isinstance(strips, dict) or len(strips) > 1000:
        raise ValueError('调音轨道设置无效')
    limits = dict(gain_db=(-60,24), pan=(-1,1), highpass=(0,1000), threshold=(-80,0),
                  ratio=(1,20), attack=(.1,200), release=(1,2000),
                  gate_threshold=(-90,0), reverb_mix=(0,1), ceiling=(-12,0))
    for key, config in strips.items():
        if not isinstance(key, str) or (key != 'master' and (not key.isascii() or not key.isdecimal())):
            raise ValueError('调音轨道编号无效')
        if not isinstance(config, dict) or set(config) - set(DEFAULT_STRIP):
            raise ValueError('音轨处理设置无效')
        for field in ('muted','solo','eq_enabled','compressor','gate','reverb','limiter'):
            if type(config.get(field, DEFAULT_STRIP[field])) is not bool:
                raise ValueError('音频模块开关无效')
        for field, (low, high) in limits.items():
            number = config.get(field, DEFAULT_STRIP[field])
            if type(number) not in (int,float) or not math.isfinite(number) or not low <= number <= high:
                raise ValueError('音频参数超出范围：' + field)
        bands = config.get('eq', DEFAULT_STRIP['eq'])
        if not isinstance(bands,list) or len(bands)!=6 or any(type(x) not in (int,float) or not math.isfinite(x) or not -18<=x<=18 for x in bands):
            raise ValueError('均衡器参数无效')
        plugins = config.get('plugins', [])
        if not isinstance(plugins,list) or len(plugins)>32:
            raise ValueError('每条音轨最多使用 32 个插件')
        for item in plugins:
            if not isinstance(item,dict) or set(item)-{'path','name','state','enabled'}:
                raise ValueError('插件设置无效')
            if not isinstance(item.get('path'),str) or not item['path'].lower().endswith('.vst3') or '\x00' in item['path']:
                raise ValueError('请选择 Windows VST3 插件')
            if type(item.get('enabled',True)) is not bool: raise ValueError('插件开关无效')
            if not isinstance(item.get('name',''),str) or len(item.get('name',''))>500: raise ValueError('插件名称无效')
            state=item.get('state','')
            if not isinstance(state,str) or len(state)>24_000_000: raise ValueError('插件预设过大')
            try: base64.b64decode(state,validate=True)
            except Exception as exc: raise ValueError('插件预设损坏') from exc

def discover_plugins(extra=()):
    roots = [Path(os.environ.get('CommonProgramFiles',r'C:\Program Files\Common Files'))/'VST3',
             Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Programs/Common/VST3', *map(Path,extra)]
    found={}
    for root in roots:
        if not root.exists(): continue
        if root.suffix.lower()=='.vst3': found[str(root.resolve()).casefold()]=str(root.resolve()); continue
        for parent, dirs, files in os.walk(root):
            for name in [*dirs,*files]:
                if name.lower().endswith('.vst3'):
                    path=Path(parent)/name
                    found[str(path.resolve()).casefold()]=str(path.resolve())
            dirs[:] = [name for name in dirs if not name.lower().endswith('.vst3')]
    return sorted(found.values(), key=lambda p: ('fabfilter' not in p.casefold(),Path(p).name.casefold()))

def project_sample_rate(project):
    prefs=project.get('project_settings',{}) if isinstance(project,dict) else getattr(project,'project_settings',{})
    return prefs.get('sample_rate',48000)

def mix_signature(project, sample_rate=None):
    sample_rate=sample_rate or project_sample_rate(project)
    from timeline_audio import audio_sources
    sources=audio_sources(project)
    files={s.path for s in sources}
    for config in settings(project).get('strips',{}).values():
        for plugin in config.get('plugins',[]):
            if plugin.get('enabled',True): files.add(plugin['path'])
    stamps=[]
    for path in sorted(files):
        file=Path(path)
        try:
            if file.is_dir():
                for child in sorted(file.rglob('*')):
                    if child.is_file():
                        st=child.stat(); stamps.append((str(child),st.st_size,st.st_mtime_ns))
            else:
                st=file.stat(); stamps.append((str(file.resolve()),st.st_size,st.st_mtime_ns))
        except OSError: stamps.append((path,None,None))
    payload=dict(engine=1, rate=sample_rate, duration=project.duration, settings=settings(project),
                 sources=[(s.track,s.path,s.start,s.duration,s.source_start) for s in sources],files=stamps)
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def mix_cache_path(project, sample_rate=None):
    from cache_paths import cache_dir
    return cache_dir('audio-mixer') / (mix_signature(project,sample_rate)+'.wav')

def ready_mix(project, sample_rate=None):
    if not enabled(project): return ''
    path=mix_cache_path(project,sample_rate)
    return str(path) if path.is_file() and path.stat().st_size>80 else ''

PRESETS = {
    '原声 / 全部重置': {},
    '对白清晰': dict(highpass=80.,eq_enabled=True,eq=[-2.,-1.5,0.,1.5,2.,0.],compressor=True,threshold=-20.,ratio=3.,limiter=True),
    '演唱会主音频': dict(highpass=30.,eq_enabled=True,eq=[1.,-1.,0.,.5,1.,.5],compressor=True,threshold=-14.,ratio=2.,attack=25.,limiter=True),
    '轻度降噪门': dict(highpass=60.,gate=True,gate_threshold=-48.),
    '小空间混响': dict(reverb=True,reverb_mix=.12),
    '防止峰值过载': dict(limiter=True,ceiling=-1.),
}
