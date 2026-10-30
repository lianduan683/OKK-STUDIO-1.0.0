"""Resolve-inspired project settings with staged changes and explicit apply."""
import copy
import math
import tkinter as tk
from tkinter import ttk

from multicam_frame_clock import canonical_frame_rate
from project_settings import DEFAULT_SETTINGS, RESOLUTIONS, FRAME_RATES, SAMPLE_RATES, BIT_DEPTHS, validate_project_settings

BG, PANEL, INPUT, INK, MUTED, ACCENT = '#202124', '#292b2e', '#18191b', '#dedfe3', '#969ba3', '#58a6d6'
FONT = 'Microsoft YaHei UI'


class ProjectSettingsMixin:
    def show_project_settings(self):
        if not self._available(): return False
        previous = vars(self).get('_project_settings_window')
        if previous is not None and previous.winfo_exists():
            previous.lift(); previous.focus_set(); return True
        self.pause()
        project = self.project
        settings = project.project_settings or DEFAULT_SETTINGS
        window = self._project_settings_window = tk.Toplevel(self)
        window.title('工程设置 · OKK STUDIO')
        window.configure(bg=BG)
        window.transient(self.winfo_toplevel())
        window.geometry(f'{self._px(830)}x{self._px(730)}')
        window.minsize(self._px(730), self._px(690))
        window.grid_columnconfigure(1, weight=1)
        window.grid_rowconfigure(1, weight=1)
        header = tk.Frame(window, bg=PANEL)
        header.grid(row=0, column=0, columnspan=2, sticky='ew')
        tk.Label(header, text='工程设置', bg=PANEL, fg=INK, font=(FONT, 15, 'bold')).pack(side='left', padx=24, pady=18)
        tk.Label(header, text='OKK STUDIO', bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(side='right', padx=24)
        sidebar = tk.Frame(window, bg=INPUT, width=self._px(174))
        sidebar.grid(row=1, column=0, sticky='nsew')
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        content = tk.Frame(window, bg=BG, padx=self._px(30), pady=self._px(22))
        content.grid(row=1, column=1, sticky='nsew')
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)
        values = self._project_settings_vars = {name: tk.StringVar(window, value=str(value)) for name, value in settings.items()}
        values['fps'] = tk.StringVar(window, value=self.fps_var.get())
        preset = next((name for name, size in RESOLUTIONS.items() if size == (settings['width'], settings['height'])), '自定义')
        values['preset'] = tk.StringVar(window, value=preset)
        pages, navigation = {}, {}

        def label(parent, text, size=10, color=INK):
            return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color, font=(FONT, size), anchor='w')

        def page(key, title, subtitle):
            panel = pages[key] = tk.Frame(content, bg=BG)
            panel.grid(row=0, column=0, sticky='nsew')
            label(panel, title, 13).pack(fill='x', pady=(0, 6))
            label(panel, subtitle, 9, MUTED).pack(fill='x', pady=(0, 22))
            return panel

        def field(parent, title, variable, choices=None):
            row = tk.Frame(parent, bg=BG)
            row.pack(fill='x', pady=8)
            label(row, title).pack(side='left')
            widget = (ttk.Combobox(row, textvariable=variable, values=choices, state='readonly', width=25)
                      if choices else tk.Entry(row, textvariable=variable, width=26, bg=INPUT, fg=INK,
                          readonlybackground=INPUT, insertbackground=INK, relief='flat',
                          highlightthickness=1, highlightbackground='#3b3e43', highlightcolor=ACCENT,
                          font=(FONT, 10)))
            widget.pack(side='right', ipady=5 if choices is None else 0)
            return widget

        video = page('video', '时间线格式', '设置当前工程的画幅和帧率')
        combo = field(video, '时间线分辨率', values['preset'], tuple(RESOLUTIONS))
        width_entry = field(video, '宽度（像素）', values['width'])
        height_entry = field(video, '高度（像素）', values['height'])
        field(video, '时间线帧率（fps）', values['fps'], FRAME_RATES)
        # Existing arbitrary rates remain visible and can be retained exactly.
        fps_combo = video.winfo_children()[-1].winfo_children()[-1]
        fps_combo.configure(state='normal')
        label(video, '修改帧率会更新剪辑帧网格；已有片段保持原有时间位置。', 9, MUTED).pack(fill='x', pady=(22, 0))
        audio = page('audio', '音频格式', '设置工程音频采样率与位深')
        sample_labels = {f'{rate / 1000:g} kHz': rate for rate in SAMPLE_RATES}
        sample_var = tk.StringVar(window, value=f'{settings["sample_rate"] / 1000:g} kHz')
        values['sample_label'] = sample_var
        field(audio, '音频采样率', sample_var, tuple(sample_labels))
        field(audio, '音频位深', values['bit_depth'], tuple(BIT_DEPTHS))
        label(audio, '支持 24bit、32bit 整数及 32bit 浮点音频。', 9, MUTED).pack(fill='x', pady=(22, 6))
        label(audio, '监看输出受音频设备支持格式限制。', 9, MUTED).pack(fill='x')

        prerender = page('prerender', '预渲染', '自动准备复杂画面，改善时间线回放')
        validate_prerender = self._build_prerender_settings(prerender, window)

        def select(key):
            pages[key].tkraise()
            for name, button in navigation.items():
                button.configure(bg='#34434f' if name == key else INPUT, fg=INK if name == key else MUTED)
        for index, (key, title) in enumerate((('video', '主设置'), ('audio', '音频'), ('prerender', '预渲染'))):
            button = navigation[key] = tk.Button(sidebar, text=title, command=lambda key=key: select(key),
                relief='flat', bd=0, anchor='w', padx=24, pady=14, bg=INPUT, fg=MUTED, font=(FONT, 10),
                activebackground='#34434f', activeforeground=INK)
            button.grid(row=index, column=0, sticky='ew', pady=(14 if index == 0 else 0, 0))

        def resolution_changed(_event=None):
            size = RESOLUTIONS[values['preset'].get()]
            if size:
                values['width'].set(str(size[0])); values['height'].set(str(size[1]))
            for entry in (width_entry, height_entry): entry.configure(state='normal' if size is None else 'readonly')
        combo.bind('<<ComboboxSelected>>', resolution_changed)
        resolution_changed(); select('video')
        footer = tk.Frame(window, bg=PANEL, padx=18, pady=14)
        footer.grid(row=2, column=0, columnspan=2, sticky='ew')
        hint = tk.StringVar(window, value='工程格式随工程保存；预渲染开关、分辨率和保存位置在本机记住。')
        tk.Label(footer, textvariable=hint, bg=PANEL, fg=MUTED, font=(FONT, 9), wraplength=self._px(550), anchor='w').pack(fill='x', pady=(0, 12))
        buttons = tk.Frame(footer, bg=PANEL)
        buttons.pack(fill='x')

        def apply():
            if self.project is not project:
                hint.set('工程已切换，请重新打开设置。'); return
            try:
                settings = dict(width=int(values['width'].get()), height=int(values['height'].get()),
                    sample_rate=sample_labels[sample_var.get()], bit_depth=values['bit_depth'].get())
                validate_project_settings(settings)
                fps = float(values['fps'].get())
                if not math.isfinite(fps) or not 1 <= fps <= 240: raise ValueError('时间线帧率须在 1–240 fps 之间。')
                # Preserve full precision if the existing rate was left alone.
                if values['fps'].get() != f'{project.fps:.12g}':
                    fps = {'23.976': 24000/1001, '29.97': 30000/1001, '59.94': 60000/1001,
                           '119.88': 120000/1001}.get(values['fps'].get().strip(), float(canonical_frame_rate(fps)))
                candidate = copy.deepcopy(project)
                candidate.project_settings, candidate.fps = settings, fps
                candidate.validate()
                prerender_options = validate_prerender()
            except (ValueError, KeyError, OverflowError, OSError) as error:
                hint.set(str(error) or '请输入有效的工程格式。'); return
            try:
                self._apply_prerender_settings(*prerender_options)
            except (OSError, ValueError) as error:
                hint.set('预渲染设置无法保存：'+str(error)); return
            if settings != project.project_settings or fps != project.fps:
                self._remember()
                project.project_settings, project.fps = settings, fps
                self._loading_fields = True
                try: self.fps_var.set(f'{fps:.12g}')
                finally: self._loading_fields = False
                self._use_project_delivery_defaults()
                self.stop_monitors()
                self._refresh()
                self.status.set(f'工程设置已应用 · {settings["width"]}×{settings["height"]} · {fps:g} fps · {settings["sample_rate"]/1000:g} kHz · {settings["bit_depth"]}')
            window.destroy()
        self._project_settings_apply = apply
        self._project_settings_select = select
        ttk.Button(buttons, text='取消', command=window.destroy, style='Compact.TButton').pack(side='right', padx=(8, 0))
        ttk.Button(buttons, text='保存设置', command=apply, style='Compact.TButton').pack(side='right')
        window.bind('<Escape>', lambda _event: window.destroy())
        window.grab_set()
        window.update_idletasks()
        window.geometry(f'+{max(0, self.winfo_rootx() + (self.winfo_width()-window.winfo_width())//2)}+{max(0, self.winfo_rooty()+40)}')
        return True

    def _use_project_delivery_defaults(self):
        if not self.project.project_settings: return
        self.app.resolution_var.set('时间线分辨率')
        self.delivery_audio_format_var.set('工程音频位深')
        self.delivery_audio_sample_rate_var.set('工程采样率')
        self.delivery_container_var.set('MOV')

    def _capture_monitor_layout(self):
        return dict(rows=self.camera_rows, columns=self.camera_columns, page=self.camera_page)

    def _restore_monitor_layout(self):
        data = self.project.monitor_layout
        if not data:
            self.camera_page = vars(self).get('_saved_camera_page', self.camera_page)
            self._build_camera_frames()
            return
        self.set_camera_grid(data.get('rows', self.camera_rows), data.get('columns', self.camera_columns), persist=False)
        self.camera_page = data.get('page', 0)
        self._build_camera_frames()
        self._refresh_camera_labels()

    def _update_program_canvas(self, _event=None):
        viewport = vars(self).get('program_viewport')
        if viewport is None: return
        settings = self.project.project_settings
        if not settings:
            self.program_surface.place(x=0, y=0, relx=0, rely=0, relwidth=1, relheight=1, width=0, height=0, anchor='nw')
            return
        width, height = max(1, viewport.winfo_width()), max(1, viewport.winfo_height())
        ratio = min(width/settings['width'], height/settings['height'])
        self.program_surface.place(relx=.5, rely=.5, x=0, y=0, relwidth=0, relheight=0, anchor='center',
            width=max(1, round(settings['width']*ratio)), height=max(1, round(settings['height']*ratio)))
