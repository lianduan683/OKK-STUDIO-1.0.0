"""Dedicated mixer page sharing the editor's timeline, transport and project."""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from audio_mixer import (DEFAULT_STRIP, EQ_BANDS, PRESETS, discover_plugins, enabled,
                         mix_signature, ready_mix, strip, strip_defaults, validate_settings)
from audio_mix_render import prepare_mix, run_plugin
from cache_paths import cache_dir
from timeline_audio import audio_sources
from camera_display import camera_number

BG,PANEL,INPUT,INK,MUTED,ACCENT='#202124','#292b2e','#18191b','#dedfe3','#969ba3','#58a6d6'
FONT='Microsoft YaHei UI'

class AudioWorkspace(tk.Frame):
    def __init__(self,parent,workspace):
        super().__init__(parent,bg=BG)
        self.ws=workspace; self.target='master'; self.loading=False; self.task=None
        self.cancel=threading.Event();self.events=queue.Queue();self.alive=True
        self.plugins=[];self.catalog=[];self._after=None
        self.enabled_var=tk.BooleanVar(self,value=False)
        self.bypass=tk.BooleanVar(self,value=False)
        self.info=tk.StringVar(self,value='选择音轨或总线，调整后点击“处理并播放”。')
        self.meter=tk.StringVar(self,value='总线峰值 — dBFS  ·  平均电平 — dBFS')
        bar=tk.Frame(self,bg=PANEL);bar.pack(fill='x')
        ttk.Checkbutton(bar,text='启用调音',variable=self.enabled_var,command=self.apply).pack(side='left',padx=8)
        for text,command in [('保存参数',self.apply),('处理并播放',lambda:self.prepare(play=True)),
                             ('停止 / 取消',self.stop)]:
            ttk.Button(bar,text=text,command=command,style='Compact.TButton').pack(side='left',padx=3,pady=6)
        ttk.Checkbutton(bar,text='原声对比',variable=self.bypass,command=self.compare).pack(side='left',padx=8)
        tk.Label(bar,textvariable=self.meter,bg=PANEL,fg=ACCENT,font=(FONT,9)).pack(side='right',padx=10)
        px=workspace._px
        body=self.body=ttk.Panedwindow(self,orient='horizontal');body.pack(fill='both',expand=True)
        left=tk.Frame(body,bg=PANEL,width=px(430));body.add(left,weight=2)
        tk.Label(left,text='通道检查器',bg=PANEL,fg=INK,font=(FONT,10,'bold')).pack(anchor='w',padx=px(10),pady=px(7))
        self.tracks=tk.Listbox(left,bg=INPUT,fg=INK,selectbackground='#3c637c',exportselection=False,
                               relief='flat',height=5,width=24)
        self.tracks.bind('<<ListboxSelect>>',self.select_track)
        self.track_choice=ttk.Combobox(left,state='readonly',width=24)
        self.track_choice.pack(fill='x',padx=px(10),pady=(0,px(5)))
        self.track_choice.bind('<<ComboboxSelected>>',lambda e:self.select_target(self.track_ids[self.track_choice.current()]))
        self.preset=tk.StringVar(self,value='选择快速预设')
        presets=ttk.Combobox(left,textvariable=self.preset,values=list(PRESETS),state='readonly',width=18)
        presets.pack(fill='x',padx=px(10),pady=(0,px(5)));presets.bind('<<ComboboxSelected>>',self.use_preset)
        style=ttk.Style(self)
        style.configure('Audio.TNotebook',background=PANEL,borderwidth=0)
        style.configure('Audio.TNotebook.Tab',padding=(px(8),px(6)),foreground=MUTED,background=BG)
        style.map('Audio.TNotebook.Tab',background=[('selected','#3c414a')],foreground=[('selected',INK)])
        self.tabs=ttk.Notebook(left,style='Audio.TNotebook');self.tabs.pack(fill='both',expand=True,padx=px(5),pady=px(5))
        self.pages={}
        self.vars={key:(tk.BooleanVar(self,value=value) if isinstance(value,bool) else tk.DoubleVar(self,value=value))
                   for key,value in DEFAULT_STRIP.items() if key not in {'eq','plugins'}}
        self.eq_vars=[tk.DoubleVar(self,value=0) for _ in EQ_BANDS]
        self._build_levels();self._build_eq();self._build_dynamics();self._build_plugins()
        self.tabs.select(self.pages['均衡器'])
        from audio_console import AudioConsole
        self.console=AudioConsole(body,self);body.add(self.console,weight=3)
        self._body_width=None
        def resize_body(event):
            if event.widget is body and event.width!=self._body_width:
                self._body_width=event.width
                body.sashpos(0,min(px(500),round(event.width*.40)))
        body.bind('<Configure>',resize_body)
        tk.Label(self,textvariable=self.info,bg=INPUT,fg=MUTED,font=(FONT,9),anchor='w',
                 wraplength=1200).pack(fill='x',ipady=5)
        self.refresh();self._poll()

    def _page(self,title):
        outer=tk.Frame(self.tabs,bg=PANEL);self.tabs.add(outer,text=title);self.pages[title]=outer
        canvas=tk.Canvas(outer,bg=PANEL,highlightthickness=0,width=1,height=1)
        scroll=ttk.Scrollbar(outer,orient='vertical',command=canvas.yview)
        scroll.pack(side='right',fill='y');canvas.pack(fill='both',expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        page=tk.Frame(canvas,bg=PANEL,padx=self.ws._px(7),pady=self.ws._px(6))
        window=canvas.create_window(0,0,window=page,anchor='nw')
        page.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=max(1,e.width)))
        def wheel(event):
            canvas.yview_scroll(-1 if event.delta>0 else 1,'units');return 'break'
        canvas.bind('<MouseWheel>',wheel)
        return page

    def _slider(self,parent,label,key,low,high,row,resolution=.1):
        tk.Label(parent,text=label,bg=PANEL,fg=INK,font=(FONT,9),anchor='w').grid(row=row,column=0,sticky='w',padx=6,pady=4)
        scale=tk.Scale(parent,from_=low,to=high,variable=self.vars[key],resolution=resolution,
                       orient='horizontal',showvalue=False,bg=PANEL,fg=INK,troughcolor=INPUT,
                       highlightthickness=0,bd=0,length=self.ws._px(180),activebackground=ACCENT)
        scale.grid(row=row,column=1,sticky='ew')
        ttk.Spinbox(parent,from_=low,to=high,increment=resolution,textvariable=self.vars[key],width=8).grid(row=row,column=2,padx=6)
        parent.columnconfigure(1,weight=1)

    def _build_levels(self):
        page=self._page('电平 / 声像')
        self._slider(page,'音量 · dB','gain_db',-60,24,0)
        self._slider(page,'声像 · 左 / 右','pan',-1,1,1,.01)
        self._slider(page,'低切 · Hz（0 = 关闭）','highpass',0,1000,2,1)
        row=tk.Frame(page,bg=PANEL);row.grid(row=3,column=0,columnspan=3,sticky='w',pady=8)
        for label,key in [('静音','muted'),('独奏音轨','solo'),('峰值限制器','limiter')]:
            ttk.Checkbutton(row,text=label,variable=self.vars[key]).pack(side='left',padx=6)
        self._slider(page,'限制器上限 · dBFS','ceiling',-12,0,4)
        tk.Label(page,text='各音轨处理 → 汇总 → 总线处理。原声对比只影响监听，导出保留已保存的调音。',
                 bg=PANEL,fg=MUTED,font=(FONT,9),wraplength=750).grid(row=5,column=0,columnspan=3,sticky='w',pady=8)

    def _build_eq(self):
        page=self._page('均衡器')
        ttk.Checkbutton(page,text='启用六段均衡器',variable=self.vars['eq_enabled']).pack(anchor='w')
        self.curve=tk.Canvas(page,bg=INPUT,height=self.ws._px(145),width=1,highlightthickness=0);self.curve.pack(fill='x',pady=5)
        self.curve._transport_scope_excluded = True
        self.curve.bind('<Configure>',lambda _e:self.draw_eq())
        self._eq_drag=None
        self.curve.bind('<ButtonPress-1>',self._eq_press)
        self.curve.bind('<B1-Motion>',self._eq_motion)
        self.curve.bind('<ButtonRelease-1>',self._eq_release)
        self.curve.bind('<Escape>',lambda e:self._load() or 'break')
        row=tk.Frame(page,bg=PANEL);row.pack(fill='both',expand=True)
        for i,(freq,var) in enumerate(zip(EQ_BANDS,self.eq_vars)):
            row.columnconfigure(i,weight=1)
            tk.Label(row,text=f'{freq/1000:g} kHz' if freq>=1000 else f'{freq} Hz',bg=PANEL,fg=INK).grid(row=0,column=i)
            scale=tk.Scale(row,from_=18,to=-18,resolution=.5,variable=var,orient='vertical',length=self.ws._px(90),
                     bg=PANEL,fg=INK,troughcolor=INPUT,highlightthickness=0,command=lambda _v:self.draw_eq())
            scale.grid(row=1,column=i,sticky='ns')
            scale.bind('<ButtonRelease-1>',lambda e:self._save_eq())
        tk.Label(page,text='更精细的动态均衡可在“FabFilter / VST3”页加入 Pro-Q。',bg=PANEL,fg=MUTED).pack(anchor='w',pady=5)

    def draw_eq(self):
        c=self.curve;c.delete('all');w=max(1,c.winfo_width());h=max(1,c.winfo_height());px=self.ws._px
        left,right,top,bottom=px(30),w-px(10),px(12),h-px(24)
        if right<=left or bottom<=top:return
        for db in (-18,-9,0,9,18):
            y=top+(18-db)/36*(bottom-top)
            c.create_line(left,y,right,y,fill='#47515b' if db==0 else '#2a3037')
            c.create_text(left-px(4),y,text=str(db),anchor='e',fill=MUTED,font=('Cascadia Mono',7))
        for freq in (20,100,1000,10000,20000):
            x=left+math.log10(freq/20)/3*(right-left)
            c.create_line(x,top,x,bottom,fill='#293139')
            c.create_text(x,bottom+px(11),text=f'{freq//1000}k' if freq>=1000 else str(freq),fill=MUTED,font=('Cascadia Mono',7))
        points=[]
        self._eq_points=[]
        for i,(freq,v) in enumerate(zip(EQ_BANDS,self.eq_vars)):
            try:gain=float(v.get())
            except tk.TclError:gain=0.
            x=left+math.log10(freq/20)/3*(right-left);y=top+(18-gain)/36*(bottom-top)
            points.extend((x,y));self._eq_points.append((x,y))
            c.create_oval(x-px(4),y-px(4),x+px(4),y+px(4),fill='#78c9b0',outline='#d9f5e8')
            c.create_text(x,y-px(10),text=str(i+1),fill='#a6b6bc',font=('Cascadia Mono',7))
        c.create_line(*points,fill='#78c9b0',width=max(1,px(1)),smooth=True)

    def _eq_press(self,event):
        points=getattr(self,'_eq_points',[])
        if not points:return 'break'
        index=min(range(len(points)),key=lambda i:abs(points[i][0]-event.x))
        if abs(points[index][0]-event.x)>self.ws._px(18):return 'break'
        self.curve.focus_set();self._eq_drag=(index,self.target,self.ws.project,tuple(v.get() for v in self.eq_vars))
        self._eq_motion(event);return 'break'

    def _eq_motion(self,event):
        if self._eq_drag is None:return 'break'
        index,target,project,_original=self._eq_drag
        if target!=self.target or project is not self.ws.project:self._eq_drag=None;return 'break'
        top,bottom=self.ws._px(12),self.curve.winfo_height()-self.ws._px(24)
        gain=max(-18.,min(18.,18-(event.y-top)*36/max(1,bottom-top)))
        self.eq_vars[index].set(round(gain*2)/2);self.draw_eq();return 'break'

    def _save_eq(self):
        self.vars['eq_enabled'].set(True);self.enabled_var.set(True);self.apply()

    def _eq_release(self,event=None):
        draft=self._eq_drag;self._eq_drag=None
        if (draft is not None and draft[1]==self.target and draft[2] is self.ws.project
                and tuple(v.get() for v in self.eq_vars)!=draft[3]):self._save_eq()
        return 'break'

    def _build_dynamics(self):
        page=self._page('压缩 / 空间')
        row=tk.Frame(page,bg=PANEL);row.grid(row=0,column=0,columnspan=3,sticky='w')
        for label,key in [('压缩器','compressor'),('噪声门','gate'),('混响','reverb')]:
            ttk.Checkbutton(row,text=label,variable=self.vars[key]).pack(side='left',padx=8)
        for row,item in enumerate([('压缩阈值 · dB','threshold',-80,0,.5),('压缩比','ratio',1,20,.1),
              ('启动 · ms','attack',.1,200,.1),('释放 · ms','release',1,2000,1),
              ('噪声门阈值 · dB','gate_threshold',-90,0,.5),('混响湿声比例','reverb_mix',0,1,.01)],1):
            self._slider(page,*item[:4],row,item[4])

    def _build_plugins(self):
        page=self._page('FabFilter / VST3')
        top=tk.Frame(page,bg=PANEL);top.pack(fill='x')
        ttk.Button(top,text='扫描已安装插件',command=self.scan,style='Compact.TButton').pack(side='left')
        ttk.Button(top,text='选择 VST3 文件…',command=self.browse,style='Compact.TButton').pack(side='left',padx=4)
        choices=tk.Frame(page,bg=PANEL);choices.pack(fill='x',pady=self.ws._px(5))
        self.add_plugin_button=ttk.Button(choices,text='加入音轨',command=self.add_selected,style='Compact.TButton')
        self.add_plugin_button.pack(side='right',padx=(self.ws._px(5),0))
        self.plugin_choice=ttk.Combobox(choices,state='readonly',width=12)
        self.plugin_choice.pack(side='left',fill='x',expand=True)
        middle=tk.Frame(page,bg=PANEL);middle.pack(fill='both',expand=True,pady=6)
        self.rack=tk.Listbox(middle,bg=INPUT,fg=INK,selectbackground='#3c637c',exportselection=False,height=5)
        self.rack.pack(side='left',fill='both',expand=True)
        buttons=tk.Frame(middle,bg=PANEL);buttons.pack(side='right',fill='y',padx=5)
        for label,command in [('打开调音界面',self.edit_plugin),('启用 / 旁通',self.bypass_plugin),
                              ('上移',lambda:self.move_plugin(-1)),('下移',lambda:self.move_plugin(1)),('移除',self.remove_plugin)]:
            ttk.Button(buttons,text=label,command=command,style='Compact.TButton').pack(fill='x',pady=2)
        tk.Label(page,text='支持已安装的 Windows VST3 音频效果，包括 Pro-Q / Pro-C / Pro-DS / Pro-L / Pro-R / Pro-MB。\n'
                  '关闭插件窗口后自动保存设置，再处理试听。插件授权与试用限制由 FabFilter 管理。',
                  bg=PANEL,fg=MUTED,font=(FONT,9),justify='left',wraplength=850).pack(anchor='w')

    def refresh(self):
        current=self.target
        from timeline_audio import audio_track_enabled
        ids=sorted({s.track for s in audio_sources(self.ws.project,enabled_only=False)},
                   key=lambda track: camera_number(self.ws.project, track))
        self.track_ids=['master',*map(str,ids)]
        if current not in self.track_ids: self.target='master'
        self.tracks.delete(0,'end');self.tracks.insert('end','总线 · 最终声音')
        for track in ids:
            number=camera_number(self.ws.project,track)
            name='主音频' if track==0 else self.ws.project.camera_names.get(track,f'机位 {number}')
            state='出声' if audio_track_enabled(self.ws.project,track) else '关闭'
            self.tracks.insert('end',f'A{number} · {name} · {state}')
        self.tracks.selection_set(self.track_ids.index(self.target));self._load()
        self.track_choice.configure(values=self.tracks.get(0,'end'))
        self.track_choice.current(self.track_ids.index(self.target))

    def _load(self):
        self._eq_drag=None
        self.loading=True;value=strip(self.ws.project,self.target)
        self.enabled_var.set(enabled(self.ws.project))
        for key,var in self.vars.items(): var.set(value[key])
        for var,gain in zip(self.eq_vars,value['eq']): var.set(gain)
        self.plugins=value['plugins'];self.refresh_rack();self.draw_eq();self.loading=False
        if hasattr(self,'console'):self.console.refresh()

    def select_track(self,_event=None):
        selected=self.tracks.curselection()
        if not selected: return
        return self.select_target(self.track_ids[selected[0]])

    def select_target(self,target):
        if target not in self.track_ids:return False
        if target!=self.target:
            if not self.apply():return False
            self.target=target;self._load()
        self.tracks.selection_clear(0,'end');self.tracks.selection_set(self.track_ids.index(target))
        self.track_choice.current(self.track_ids.index(target))
        self.console.refresh()
        return True

    def commit_console(self,target,changes):
        if not self.select_target(target):return False
        original=strip(self.ws.project,target)
        if all(original[name]==value for name,value in changes.items()):return True
        for name,value in changes.items():self.vars[name].set(value)
        self.enabled_var.set(True)
        return self.apply()

    def toggle_console_track(self,target):
        if target=='master' or not self.select_target(target):return
        from timeline_audio import audio_track_enabled
        track=int(target)
        self.ws.pause();self.ws._remember()
        self.ws.project.audio_track_enabled[track]=not audio_track_enabled(self.ws.project,track)
        self.ws._refresh();self.refresh()

    def value(self):
        return {**{key:var.get() for key,var in self.vars.items()},'eq':[v.get() for v in self.eq_vars],
                'plugins':copy.deepcopy(self.plugins)}

    def apply(self):
        if self.loading: return True
        try:
            if self.value()==strip(self.ws.project,self.target) and self.enabled_var.get()==enabled(self.ws.project):
                return True
            value=copy.deepcopy(self.ws.project.audio_mixer)
            value['enabled']=self.enabled_var.get();value.setdefault('strips',{})[self.target]=self.value()
            validate_settings(value)
            if value==self.ws.project.audio_mixer: return True
            self.ws.pause();self.ws._remember();self.ws.project.audio_mixer=value
            self.ws._configure_player();self.bypass.set(False)
            self.info.set('参数已保存。点击“处理并播放”更新试听，导出会自动应用这些设置。')
            self.meter.set('总线参数已更改 · 处理后更新电平统计')
            if hasattr(self,'console'):self.console.refresh()
            return True
        except (ValueError,tk.TclError) as exc:
            messagebox.showerror('调音参数',str(exc),parent=self);return False

    def use_preset(self,_event=None):
        value=strip_defaults();value.update(PRESETS[self.preset.get()])
        for key,var in self.vars.items(): var.set(value[key])
        for var,gain in zip(self.eq_vars,value['eq']): var.set(gain)
        # Presets only change the built-in controls; preserve the user's inserts.
        self.enabled_var.set(True);self.apply();self.draw_eq()

    def enable_track(self):
        if self.target=='master': return
        self.ws._remember();self.ws.project.audio_track_enabled[int(self.target)]=True
        self.ws._refresh();self.refresh()

    def scan(self):
        self.catalog=discover_plugins()
        self.plugin_choice.configure(values=[Path(p).stem for p in self.catalog])
        if self.catalog: self.plugin_choice.current(0)
        self.info.set(f'找到 {len(self.catalog)} 个 VST3 插件。' if self.catalog else
                      '尚未安装 VST3 插件。安装 FabFilter 的 Windows VST3 组件后点击扫描；内置调音模块可以直接使用。')

    def browse(self):
        path=filedialog.askopenfilename(parent=self,title='选择 Windows VST3 音频效果',filetypes=[('VST3 插件','*.vst3')])
        if not path: return
        self.add_path(path)

    def add_selected(self):
        i=self.plugin_choice.current()
        if 0<=i<len(self.catalog): self.add_path(self.catalog[i])

    def _job(self,work,finish):
        if self.task is not None:
            self.info.set('当前音频任务还在运行，可先停止 / 取消。');return False
        self.cancel=threading.Event();cancel=self.cancel;self.task=finish
        def run():
            try: self.events.put(('done',work(cancel)))
            except Exception as exc: self.events.put(('error',str(exc)))
        self._thread=threading.Thread(target=run,daemon=True,name='okk-audio-mixer')
        self._thread.start();return True

    def _plugin_job(self,action,item,finish):
        self.ws.pause()
        def work(cancel):
            with tempfile.TemporaryDirectory(prefix='plugin-',dir=cache_dir('audio-mixer')) as folder:
                return run_plugin({'action':action,'plugin':item},folder,cancel)
        self.info.set('正在加载插件…' if action=='probe' else '插件窗口已打开；关闭后保存设置，编辑界面可继续使用。')
        return self._job(work,finish)

    def add_path(self,path):
        if len(self.plugins)>=32: return
        if not self.apply(): return
        target=self.target;project=self.ws.project
        item=dict(path=str(Path(path).resolve()),name=Path(path).stem,state='',enabled=True)
        def finish(data):
            if self.ws.project is not project: return
            item.update(name=data['name'],state=data['state'])
            self.ws._remember();config=strip(project,target);config['plugins'].append(item)
            project.audio_mixer.setdefault('strips',{})[target]=config;project.audio_mixer['enabled']=True
            self.ws._configure_player();self.refresh();self.info.set('已加入 '+data['name']+'，选中后可打开原生调音界面。')
        self._plugin_job('probe',item,finish)

    def refresh_rack(self,selected=None):
        self.rack.delete(0,'end')
        for i,item in enumerate(self.plugins): self.rack.insert('end',f'{i+1:02d}  '+('●  ' if item.get('enabled',True) else '○  ')+item['name'])
        if selected is not None and self.plugins:self.rack.selection_set(min(selected,len(self.plugins)-1))

    def selected_plugin(self):
        return self.rack.curselection()[0] if self.rack.curselection() else None

    def edit_plugin(self):
        i=self.selected_plugin()
        if i is None or not self.apply(): return
        target=self.target;project=self.ws.project;item=copy.deepcopy(self.plugins[i]);old=copy.deepcopy(item)
        def finish(data):
            if self.ws.project is not project: return
            config=strip(project,target)
            if i>=len(config['plugins']) or config['plugins'][i]!=old:
                self.info.set('音轨插件已更改，本次窗口设置未覆盖新编辑。');return
            self.ws._remember();config['plugins'][i]['state']=data['state']
            project.audio_mixer['strips'][target]=config
            self.ws._configure_player();self.refresh();self.info.set('插件设置已保存，可以处理并试听。')
        self._plugin_job('editor',item,finish)

    def bypass_plugin(self):
        i=self.selected_plugin()
        if i is not None:self.plugins[i]['enabled']=not self.plugins[i].get('enabled',True);self.apply();self.refresh_rack(i)

    def move_plugin(self,delta):
        i=self.selected_plugin()
        if i is None or not 0<=i+delta<len(self.plugins):return
        self.plugins[i],self.plugins[i+delta]=self.plugins[i+delta],self.plugins[i]
        self.apply();self.refresh_rack(i+delta)

    def remove_plugin(self):
        i=self.selected_plugin()
        if i is not None: self.plugins.pop(i);self.apply();self.refresh_rack(i)

    def prepare(self,play=False):
        if not self.apply(): return
        if not enabled(self.ws.project):
            self.enabled_var.set(True)
            if not self.apply(): return
        if self.task is not None: self.info.set('正在处理，请稍候或取消。');return
        caps=self.ws.app.caps
        if not caps.ffmpeg_path or not caps.ffprobe_path:
            self.info.set('请等待 FFmpeg 检测完成。');return
        project=copy.deepcopy(self.ws.project);position=self.ws.current
        signature=mix_signature(project);self.ws.pause();self.bypass.set(False)
        def work(cancel):
            return prepare_mix(project,caps.ffmpeg_path,caps.ffprobe_path,cancel=cancel,
                progress=lambda p,s:self.events.put(('progress',f'{p:.0f}% · {s}')))
        def finish(path):
            if signature!=mix_signature(self.ws.project):
                self.info.set('调音已完成，但时间线已更改；重新处理即可使用最新编辑。');return
            self.ws._configure_player()
            info=Path(path).with_suffix('.json')
            if info.exists():
                stats=json.loads(info.read_text(encoding='utf-8'))
                self.meter.set(f"总线峰值 {stats['peak_db']:.1f} dBFS  ·  平均电平 {stats['rms_db']:.1f} dBFS")
            self.info.set('调音就绪 · 监听与导出使用同一份处理结果。')
            if play:
                if not self.ws.player:self.ws.start_monitors()
                if self.ws.player:
                    self.ws.player.submit('seek',time=position);self.ws.player.submit('play')
        self._job(work,finish)

    def ensure_playback(self):
        if self.task is not None:
            self.stop();return False
        if enabled(self.ws.project) and not ready_mix(self.ws.project):
            self.prepare(play=True);return False
        return True

    def compare(self):
        if not ready_mix(self.ws.project):
            self.bypass.set(False);self.info.set('先处理调音，即可对比原声。');return
        if self.ws.player:self.ws.player.submit('audio_bypass',enabled=self.bypass.get())
        self.info.set('正在监听原声（导出仍保留调音）。' if self.bypass.get() else '正在监听调音后的声音。')

    def stop(self):
        self.cancel.set();self.ws.pause()

    def _poll(self):
        if not self.alive:return
        try:
            while True:
                kind,payload=self.events.get_nowait()
                if kind=='progress':self.info.set(payload)
                else:
                    callback=self.task;self.task=None
                    if kind=='done' and not self.cancel.is_set():
                        if callback:callback(payload)
                    else:self.info.set('已取消调音。' if self.cancel.is_set() else '调音失败：'+payload)
        except queue.Empty:pass
        self._after=self.after(100,self._poll)

    def close(self):
        self.alive=False;self.cancel.set()
        worker=getattr(self,'_thread',None)
        if worker is not None and worker.is_alive(): worker.join(timeout=1.)
        if self._after is not None:
            self.after_cancel(self._after);self._after=None
