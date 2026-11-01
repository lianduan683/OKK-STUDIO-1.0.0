"""Channel-strip UI for the existing OKK audio processing engine."""
from __future__ import annotations
import math
import tkinter as tk
from tkinter import ttk
from audio_mixer import strip
from camera_display import camera_number
from timeline_audio import audio_track_enabled

BG,PANEL,INPUT,INK,MUTED='#25272c','#303239','#17191d','#e0e3e6','#9299a3'
COLORS=('#67aece','#aa96d6','#82b7a0','#d8ad71','#cd8897','#7ba4db')
FONT='Microsoft YaHei UI'


class ChannelStrip(tk.Frame):
    def __init__(self,parent,console,target):
        self.console,self.owner,self.target=console,console.owner,target
        self.px=self.owner.ws._px
        super().__init__(parent,bg=PANEL,width=self.px(106),highlightthickness=1,highlightbackground='#3c4048')
        self._drag=None
        self.color='#72c6ae' if target=='master' else COLORS[int(target)%len(COLORS)]
        self.name=tk.StringVar(self)
        self.gain=tk.StringVar(self,value='0.0')
        self.pan=tk.DoubleVar(self,value=0.)
        self.pan_text=tk.StringVar(self,value='C')
        tk.Frame(self,bg=self.color,height=self.px(3)).pack(fill='x')
        self.title=tk.Button(self,textvariable=self.name,command=self.select,bg=PANEL,fg=INK,
            activebackground='#40464f',activeforeground=INK,relief='flat',bd=0,
            font=(FONT,9,'bold'),wraplength=self.px(100),height=2)
        self.title.pack(fill='x')
        self.route=tk.Button(self,text='',command=self.toggle_route,relief='flat',bd=0,
            bg=INPUT,fg=MUTED,font=(FONT,8),activebackground=PANEL,activeforeground=INK)
        self.route.pack(fill='x',padx=self.px(5),pady=(0,self.px(5)))
        self.fx=tk.Button(self,text='插入',command=lambda:self.editor('FabFilter / VST3'),relief='flat',bd=0,
            bg=INPUT,fg=MUTED,font=(FONT,8),activebackground='#3c444c',activeforeground=INK)
        self.fx.pack(fill='x',padx=self.px(5),pady=self.px(2))
        row=tk.Frame(self,bg=PANEL);row.pack(fill='x',padx=self.px(5))
        self.eq=self.button(row,'EQ',lambda:self.editor('均衡器'))
        self.dynamics=self.button(row,'DYN',lambda:self.editor('压缩 / 空间'))
        panrow=tk.Frame(self,bg=PANEL);panrow.pack(fill='x',padx=self.px(5),pady=self.px(3))
        tk.Label(panrow,text='PAN',bg=PANEL,fg=MUTED,font=('Cascadia Mono',7)).pack(side='left')
        tk.Label(panrow,textvariable=self.pan_text,bg=PANEL,fg=INK,font=('Cascadia Mono',8)).pack(side='right')
        self.pan_scale=tk.Scale(self,from_=-1,to=1,resolution=.01,showvalue=False,variable=self.pan,
            orient='horizontal',bg=PANEL,fg=INK,troughcolor=INPUT,highlightthickness=0,bd=0,
            length=self.px(85),sliderlength=self.px(12),activebackground=self.color,command=self._pan_display)
        self.pan_scale.pack(fill='x',padx=self.px(5))
        self.pan_scale.bind('<ButtonPress-1>',self._begin_pan)
        self.pan_scale.bind('<ButtonRelease-1>',self._commit_pan)
        self.pan_scale.bind('<KeyPress>',self._begin_pan)
        self.pan_scale.bind('<KeyRelease>',self._commit_pan)
        row=tk.Frame(self,bg=PANEL);row.pack(fill='x',padx=self.px(5),pady=self.px(4))
        self.mute=self.button(row,'M',lambda:self.flag('muted'))
        self.solo=self.button(row,'S',lambda:self.flag('solo'))
        if target=='master':self.solo.configure(state='disabled')
        self.fader=tk.Canvas(self,bg=PANEL,width=self.px(100),height=self.px(150),
            highlightthickness=0,takefocus=True,cursor='sb_v_double_arrow')
        self.fader._transport_scope_excluded = True
        self.fader.pack(fill='both',expand=True,padx=self.px(3))
        self.fader.bind('<Configure>',lambda e:self.draw())
        self.fader.bind('<ButtonPress-1>',self.press)
        self.fader.bind('<B1-Motion>',self.motion)
        self.fader.bind('<ButtonRelease-1>',self.release)
        self.fader.bind('<Double-Button-1>',lambda e:self.commit(0.))
        self.fader.bind('<Escape>',self.cancel)
        self.fader.bind('<Up>',lambda e:self.step(.1))
        self.fader.bind('<Down>',lambda e:self.step(-.1))
        row=tk.Frame(self,bg=INPUT);row.pack(side='bottom',before=self.fader,fill='x',padx=self.px(5),pady=(self.px(3),self.px(6)))
        entry=tk.Entry(row,textvariable=self.gain,justify='center',width=6,bg=INPUT,fg=INK,
            insertbackground=INK,relief='flat',font=('Cascadia Mono',10))
        entry.pack(side='left',fill='x',expand=True,ipady=self.px(4))
        entry.bind('<Return>',lambda e:self.commit_entry())
        entry.bind('<FocusIn>',lambda e:setattr(self,'_entry_project',self.owner.ws.project))
        tk.Label(row,text='dB',bg=INPUT,fg=MUTED,font=('Cascadia Mono',8)).pack(side='right',padx=self.px(4))
        self.refresh()

    def button(self,parent,text,command):
        b=tk.Button(parent,text=text,command=command,bg=INPUT,fg=MUTED,relief='flat',bd=0,
            activebackground='#46505a',activeforeground=INK,font=('Cascadia Mono',9))
        b.pack(side='left',fill='x',expand=True,padx=self.px(1),ipady=self.px(2));return b

    def select(self):return self.owner.select_target(self.target)

    def editor(self,page):
        if self.select():self.owner.tabs.select(self.owner.pages[page])

    def toggle_route(self):
        if self.target!='master':self.owner.toggle_console_track(self.target)

    def flag(self,name):
        value=strip(self.owner.ws.project,self.target)
        self.owner.commit_console(self.target,{name:not value[name]})

    def _pan_display(self,_value=None):
        value=self.pan.get()
        self.pan_text.set('C' if abs(value)<.005 else ('L ' if value<0 else 'R ')+str(round(abs(value)*100)))

    def _begin_pan(self,_event=None):self._pan_project=self.owner.ws.project

    def _commit_pan(self,_event=None):
        if getattr(self,'_pan_project',None) is self.owner.ws.project:
            self.owner.commit_console(self.target,{'pan':self.pan.get()})
        self._pan_project=None
        self.refresh()

    def refresh(self):
        p=self.owner.ws.project
        value=strip(p,self.target)
        selected=self.owner.target==self.target
        self.configure(highlightbackground=self.color if selected else '#3c4048')
        if self.target=='master':
            name='BUS 1\n主输出';route='最终输出'
        else:
            track=int(self.target)
            number=camera_number(p,track)
            label='主音频' if track==0 else p.camera_names.get(track,f'机位 {number}')
            name=f'A{number}\n{label}'
            route='●  已启用' if audio_track_enabled(p,track) else '○  未启用'
        self.name.set(name)
        self.route.configure(text=route,fg=self.color if self.target=='master' or audio_track_enabled(p,int(self.target)) else MUTED)
        self.title.configure(fg=self.color if selected else INK)
        self.fx.configure(text='插入 · '+str(len(value['plugins'])),fg=self.color if value['plugins'] else MUTED)
        self.eq.configure(fg=self.color if value['eq_enabled'] else MUTED)
        self.dynamics.configure(fg=self.color if any(value[k] for k in ('compressor','gate','limiter','reverb')) else MUTED)
        self.mute.configure(bg='#9d6044' if value['muted'] else INPUT,fg=INK if value['muted'] else MUTED)
        self.solo.configure(bg='#a98a3e' if value['solo'] else INPUT,fg=INK if value['solo'] else MUTED)
        if self._drag is None:
            self.gain.set(f"{value['gain_db']:.1f}")
            self.pan.set(value['pan']);self._pan_display()
        self.draw()

    def draw(self):
        c=self.fader;c.delete('all')
        w,h=c.winfo_width(),c.winfo_height();px=self.px
        if w<3 or h<3:return
        top,bottom=px(10),max(px(20),h-px(10));x=w*.57
        c.create_line(x,top,x,bottom,fill=INPUT,width=px(8),capstyle='round')
        for db in (24,12,0,-12,-24,-36,-48,-60):
            y=top+(24-db)/84*(bottom-top)
            c.create_text(x-px(13),y,anchor='e',text=str(db),fill='#b2b7c0' if db==0 else '#6d747e',font=('Cascadia Mono',7))
            c.create_line(x-px(6),y,x+px(6),y,fill='#69727c' if db==0 else '#424851')
        try:value=float(self.gain.get())
        except (ValueError,tk.TclError):return
        if not math.isfinite(value):return
        y=top+(24-max(-60,min(24,value)))/84*(bottom-top)
        c.create_rectangle(x-px(12),y-px(7),x+px(12),y+px(7),fill='#d0d4d7',outline='#14171b')
        c.create_line(x-px(9),y,x+px(9),y,fill='#525b66',width=max(1,px(1)))
        c.create_line(x+px(20),top,x+px(20),bottom,fill='#1b1f23',width=px(3))

    def press(self,event):
        if not self.select():return 'break'
        self.fader.focus_set()
        self._drag=(event.y,float(self.gain.get()),self.owner.ws.project)
        return 'break'

    def motion(self,event):
        if self._drag is None:return 'break'
        y,db,project=self._drag
        if project is not self.owner.ws.project:return self.cancel()
        delta=(y-event.y)*84/max(self.px(80),self.fader.winfo_height()-self.px(20))
        value=max(-60.,min(24.,db+delta*(.1 if event.state & 1 else 1.)))
        self.gain.set(f'{value:.1f}');self.draw()
        return 'break'

    def release(self,event=None):
        old=self._drag;self._drag=None
        if old is not None and old[2] is self.owner.ws.project and float(self.gain.get())!=old[1]:self.commit_entry()
        else:self.refresh()
        return 'break'

    def cancel(self,event=None):self._drag=None;self.refresh();return 'break'

    def step(self,delta):
        return self.commit(max(-60.,min(24.,strip(self.owner.ws.project,self.target)['gain_db']+delta)))

    def commit_entry(self):
        try:
            value=float(self.gain.get())
            if not math.isfinite(value) or not -60<=value<=24:raise ValueError()
        except (ValueError,tk.TclError):
            self.owner.info.set('音量范围为 -60 至 +24 dB。');self.refresh();return 'break'
        return self.commit(value)

    def commit(self,value):
        self.owner.commit_console(self.target,{'gain_db':value})
        self.refresh();return 'break'


class AudioConsole(tk.Frame):
    def __init__(self,parent,owner):
        super().__init__(parent,bg=BG)
        self.owner=owner;self.channels={};self._ids=None
        px=owner.ws._px
        heading=tk.Frame(self,bg='#202227');heading.pack(fill='x')
        tk.Label(heading,text='混音台  /  MIXER',bg='#202227',fg=INK,font=(FONT,10,'bold')).pack(side='left',padx=px(10),pady=px(9))
        tk.Label(heading,text='M 静音  ·  S 独奏',bg='#202227',fg=MUTED,font=(FONT,8)).pack(side='right',padx=px(10))
        body=tk.Frame(self,bg=BG);body.pack(fill='both',expand=True)
        self.master_strip=ChannelStrip(body,self,'master')
        self.master_strip.pack(side='right',fill='y',padx=(px(7),px(5)),pady=px(5))
        scroller=tk.Frame(body,bg=BG);scroller.pack(side='left',fill='both',expand=True)
        self.canvas=tk.Canvas(scroller,bg=BG,highlightthickness=0,width=1,height=1)
        scroll=ttk.Scrollbar(scroller,orient='horizontal',command=self.canvas.xview)
        scroll.pack(side='bottom',fill='x');self.canvas.pack(fill='both',expand=True)
        self.canvas.configure(xscrollcommand=scroll.set)
        self.content=tk.Frame(self.canvas,bg=BG)
        self.window=self.canvas.create_window(0,0,window=self.content,anchor='nw')
        self.canvas.bind('<Configure>',self.resize)
        self.content.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.empty=tk.Label(self.content,text='导入带声音的素材后\n音轨通道会显示在这里',bg=BG,fg=MUTED,font=(FONT,10))
        self.note=tk.StringVar(self,value='拖动推子后自动保存 · 处理并播放后试听')
        tk.Label(self,textvariable=self.note,bg=INPUT,fg=MUTED,anchor='w',font=(FONT,8),
                 padx=px(8),pady=px(5),wraplength=px(650)).pack(fill='x')

    def resize(self,event):
        self.canvas.itemconfigure(self.window,height=max(1,event.height))

    def refresh(self):
        ids=tuple(t for t in self.owner.track_ids if t!='master')
        if ids!=self._ids:
            for channel in self.channels.values():channel.destroy()
            self.channels={}
            self.empty.pack_forget()
            for target in ids:
                channel=ChannelStrip(self.content,self,target)
                channel.pack(side='left',fill='y',padx=self.owner.ws._px(3),pady=self.owner.ws._px(5))
                self.channels[target]=channel
            if not ids:self.empty.pack(padx=self.owner.ws._px(20),pady=self.owner.ws._px(40))
            self._ids=ids
        for channel in (*self.channels.values(),self.master_strip):channel.refresh()
