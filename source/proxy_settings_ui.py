"""Per-file proxy badges, video context commands and proxy setup dialog."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from configured_proxy import ProxyOptions, FORMATS, SIZES


class ProxySettingsMixin:
    def _clip_proxy_state(self, clip, resolver):
        resolved = resolver(clip.path, clip.proxy_path)
        if str(Path(resolved).resolve()).casefold() != str(Path(clip.path).resolve()).casefold():
            return 'ready'
        return 'missing' if clip.proxy_path and not Path(clip.proxy_path).is_file() else 'none'

    def _proxy_icon(self, state):
        icons = vars(self).setdefault('_proxy_file_icons', {})
        if state not in icons:
            size = max(12, self._px(14))
            icon = tk.PhotoImage(master=self, width=size, height=size)
            color = {'ready': '#70bf9a', 'none': '#69717b', 'missing': '#edab64'}[state]
            icon.put(color, to=(1, 1, size-1, size-1))
            # A crisp P or ! drawn with Tk pixels scales with the tree font.
            glyph = ('1110', '1001', '1110', '1000', '1000') if state != 'missing' else ('010', '010', '010', '000', '010')
            unit = max(1, size//8)
            x0, y0 = (size-len(glyph[0])*unit)//2, (size-5*unit)//2
            for y, row in enumerate(glyph):
                for x, pixel in enumerate(row):
                    if pixel == '1': icon.put('#182220' if state == 'ready' else '#edf0f2',
                        to=(x0+x*unit, y0+y*unit, x0+(x+1)*unit, y0+(y+1)*unit))
            icons[state] = icon
        return icons[state]

    def _refresh_clip_proxy_icons(self, resolver):
        tree = vars(self).get('media_tree')
        if tree is None: return
        for clip in self.project.clips:
            if tree.exists(clip.id):
                tree.item(clip.id, image=self._proxy_icon(self._clip_proxy_state(clip, resolver)))

    def _clip_proxy_context(self, event, row):
        clip = next((clip for clip in self.project.clips if clip.id == row), None)
        if clip is None: return
        tree = self.media_tree
        if row not in tree.selection(): tree.selection_set(row)
        tree.focus(row)
        identifiers = tuple(value for value in tree.selection() if not value.startswith('camera_'))
        menu = tk.Menu(self, tearoff=False, bg='#292b2e', fg='#dedfe3', activebackground='#3c5263')
        from proxy_media import PreviewProxyResolver
        state = self._clip_proxy_state(clip, PreviewProxyResolver())
        menu.add_command(label={'ready': '● 已代理', 'none': '○ 未代理', 'missing': '⚠ 代理丢失'}[state], state='disabled')
        menu.add_command(label='转代理…', command=lambda: self.show_proxy_settings(identifiers),
                         state='disabled' if self._quick_proxy_running or self.busy or self.app.working else 'normal')
        menu.add_command(label='链接已有代理…', command=self.link_existing_proxy,
                         state='disabled' if self._proxy_link_running or self.busy or self.app.working else 'normal')
        previous = getattr(self, 'camera_context_menu', None)
        if previous is not None: previous.destroy()
        self.camera_context_menu = menu
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()
        return 'break'

    def show_proxy_settings(self, identifiers=None):
        if self._quick_proxy_running: return False
        if identifiers is None: identifiers = self.media_tree.selection()
        clips = [clip for clip in self.project.clips if clip.id in identifiers]
        if not clips: return False
        previous = vars(self).get('_proxy_settings_window')
        if previous is not None and previous.winfo_exists(): previous.destroy()
        project = self.project
        window = self._proxy_settings_window = tk.Toplevel(self)
        window.title('转代理 · 设置')
        window.configure(bg='#242629')
        window.transient(self.winfo_toplevel())
        window.resizable(False, False)
        body = ttk.Frame(window, padding=18)
        body.pack(fill='both', expand=True)
        count = len({str(Path(clip.path).resolve()).casefold() for clip in clips})
        ttk.Label(body, text=f'为所选 {len(clips)} 个视频生成代理（{count} 份原片）').grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 12))
        fmt = tk.StringVar(window, value=next(iter(FORMATS)))
        size = tk.StringVar(window, value=next(iter(SIZES)))
        fps = tk.StringVar(window, value='最高 30 fps')
        folder = tk.StringVar(window, value=str(Path(clips[0].path).resolve().parent/'Proxy'))
        for row, label, variable, values in ((1, '代理格式', fmt, tuple(FORMATS)),
                (2, '分辨率上限', size, tuple(SIZES)),
                (3, '代理帧率', fps, ('最高 30 fps', '保留原始帧率'))):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', padx=(0, 14), pady=6)
            ttk.Combobox(body, textvariable=variable, values=values, state='readonly', width=31).grid(row=row, column=1, columnspan=2, sticky='ew', pady=6)
        ttk.Label(body, text='代理存放位置').grid(row=4, column=0, sticky='w', padx=(0, 14), pady=6)
        ttk.Entry(body, textvariable=folder, width=44).grid(row=4, column=1, sticky='ew', pady=6)
        def browse():
            path = filedialog.askdirectory(parent=window, title='选择代理存放位置', initialdir=folder.get(), mustexist=False)
            if path: folder.set(path)
        ttk.Button(body, text='浏览…', command=browse).grid(row=4, column=2, padx=(8, 0))
        ttk.Label(body, text='保持画面比例；音频随代理保存。完成后自动挂载，导出使用原片。').grid(row=5, column=0, columnspan=3, sticky='w', pady=(12, 6))
        hint = tk.StringVar(window, value='')
        ttk.Label(body, textvariable=hint).grid(row=6, column=0, columnspan=3, sticky='w')
        self._proxy_settings_vars = dict(format=fmt, size=size, fps=fps, folder=folder)
        def start():
            if self.project is not project:
                hint.set('工程已改变，请关闭后重新选择视频。'); return
            width, height = SIZES[size.get()]
            options = ProxyOptions(folder.get().strip(), FORMATS[fmt.get()], width, height,
                                   0 if fps.get() == '保留原始帧率' else 30)
            try:
                options.validate()
                destination = Path(options.folder).expanduser().resolve()
                if destination.exists() and not destination.is_dir(): raise ValueError('代理存放位置必须是文件夹。')
            except (ValueError, OSError) as error:
                hint.set(str(error)); return
            current = [clip for clip in self.project.clips if clip.id in identifiers]
            if self.start_quick_proxy(clips=current, options=options): window.destroy()
            else: hint.set(self.status.get())
        buttons = ttk.Frame(body)
        buttons.grid(row=7, column=0, columnspan=3, sticky='e', pady=(12, 0))
        ttk.Button(buttons, text='取消', command=window.destroy).pack(side='right', padx=(8, 0))
        ttk.Button(buttons, text='开始转代理', command=start).pack(side='right')
        self._proxy_settings_start = start
        window.bind('<Escape>', lambda _event: window.destroy())
        window.update_idletasks()
        window.geometry(f'+{self.winfo_rootx()+40}+{self.winfo_rooty()+80}')
        return True
