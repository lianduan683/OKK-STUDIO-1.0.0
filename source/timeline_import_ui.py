"""Reviewable interchange import and reversible multicamera timeline views."""
from __future__ import annotations

from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from timeline_import import FILE_TYPES, build_project, missing_media, read_timeline, relink_media, sequence_names


class TimelineImportMixin:
    def import_timeline(self, path=None):
        if not self._available(): return False
        if not path:
            path = filedialog.askopenfilename(parent=self, title='导入时间线', filetypes=FILE_TYPES)
        if not path: return False
        window = tk.Toplevel(self)
        window.title('导入时间线')
        window.transient(self.winfo_toplevel())
        window.resizable(False, False)
        body = ttk.Frame(window, padding=18)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=Path(path).name, font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(body, text='导入为新的编辑时间线，当前时间线可用 Ctrl+Z 恢复。').pack(anchor='w', pady=(6, 12))
        try: names = sequence_names(path)
        except Exception as error:
            window.destroy()
            messagebox.showerror('导入时间线', str(error), parent=self)
            return False
        if not names:
            window.destroy(); messagebox.showerror('导入时间线', '文件中没有可选择的时间线。', parent=self); return False
        ttk.Label(body, text='选择时间线').pack(anchor='w')
        choice = ttk.Combobox(body, values=[f'{i+1}. {name}' for i, name in enumerate(names)], state='readonly', width=58)
        choice.pack(fill='x', pady=(4, 12)); choice.current(0)
        mode = tk.StringVar(window, 'collapsed')
        ttk.Radiobutton(body, text='合并为一条多机位时间线（保留机位和切换点）', variable=mode, value='collapsed').pack(anchor='w')
        ttk.Radiobutton(body, text='展开各机位轨道（保留原始位置、入点和音轨）', variable=mode, value='expanded').pack(anchor='w', pady=(6, 12))
        ttk.Label(body, text='导入后可右键节目轨 → 在时间线上打开多机位 / 合并显示为多机位时间线。').pack(anchor='w')
        fps = tk.StringVar(window, f'{self.project.fps:.8g}')
        if Path(path).suffix.lower() == '.edl':
            line = ttk.Frame(body); line.pack(fill='x', pady=12)
            ttk.Label(line, text='EDL 帧率（文件不携带帧率）').pack(side='left')
            ttk.Combobox(line, textvariable=fps, values=['23.976023976', '24', '25', '29.97002997', '30', '50', '59.94005994', '60'], width=16).pack(side='left', padx=8)
        buttons = ttk.Frame(body); buttons.pack(fill='x', pady=(18, 0))
        ttk.Button(buttons, text='取消', command=window.destroy).pack(side='right')

        def start():
            try:
                value = float(fps.get())
                if not 0 < value <= 1000: raise ValueError()
            except ValueError:
                messagebox.showerror('导入时间线', '请输入有效帧率。', parent=window); return
            settings = (choice.current(), mode.get() == 'collapsed', value)
            window.destroy()
            self._start_interchange_import(path, *settings)

        ttk.Button(buttons, text='读取时间线…', command=start).pack(side='right', padx=8)
        window.grab_set()
        return True

    def _start_interchange_import(self, path, index, collapsed, fps):
        if not self._available(): return False
        self.pause()
        project = self.project
        self.busy = True
        self.app.cancel_event.clear()
        self.app._set_working(True)
        self.status.set('正在读取时间线剪辑结构…')

        def worker():
            try:
                result = read_timeline(path, fps=fps, sequence_index=index)
                self.jobs.put(('interchange_ready', (project, result, collapsed)))
            except Exception as error:
                self.jobs.put(('interchange_error', str(error)))
        threading.Thread(target=worker, name='timeline-interchange-import', daemon=True).start()
        return True

    def _handle_interchange_job(self, kind, payload):
        if kind not in ('interchange_ready', 'interchange_error'): return False
        self.busy = False
        self.app._set_working(False)
        if self.app.cancel_event.is_set():
            self.status.set('已取消时间线导入。'); return True
        if kind == 'interchange_error':
            self.status.set('时间线导入失败。')
            messagebox.showerror('导入时间线', payload, parent=self); return True
        original, timeline, collapsed = payload
        if self.project is not original:
            self.status.set('当前工程已改变，未应用导入结果。'); return True
        self._review_interchange(timeline, collapsed)
        return True

    def _review_interchange(self, timeline, collapsed):
        window = tk.Toplevel(self)
        window.title('确认导入时间线')
        window.transient(self.winfo_toplevel())
        window.geometry('740x510')
        body = ttk.Frame(window, padding=16); body.pack(fill='both', expand=True)
        summary = ttk.Label(body, text=f'{timeline.name}  ·  {timeline.fps:.6g} fps  ·  {timeline.duration:.3f} 秒\n'
                            f'{len(timeline.clips)} 个音视频片段  ·  '+('单条多机位视图' if collapsed else '展开机位视图'))
        summary.pack(anchor='w', pady=(0, 12))
        text = tk.Text(body, height=16, wrap='word', font=('Microsoft YaHei UI', 10))
        text.pack(fill='both', expand=True)

        def refresh():
            missing = missing_media(timeline)
            content = '兼容性说明\n'+('\n'.join('• '+w for w in timeline.warnings) or '剪辑结构已读取。')
            content += f'\n\n离线素材：{len(missing)} 个\n'
            content += '\n'.join(missing) if missing else '全部素材已找到。'
            text.configure(state='normal'); text.delete('1.0', 'end'); text.insert('1.0', content); text.configure(state='disabled')

        def relink():
            folder = filedialog.askdirectory(parent=window, title='选择素材根目录（递归查找，重名文件保留待确认）')
            if not folder: return
            link.configure(state='disabled'); manual_link.configure(state='disabled')
            self.status.set('正在查找并重连时间线素材…')
            def worker():
                try: relink_media(timeline, folder); outcome = None
                except Exception as error: outcome = str(error)
                results.append(outcome)
            results = []
            threading.Thread(target=worker, name='timeline-media-relink', daemon=True).start()
            apply.configure(state='disabled')
            def poll():
                if not window.winfo_exists(): return
                if not results: window.after(100, poll); return
                link.configure(state='normal'); manual_link.configure(state='normal'); apply.configure(state='normal')
                if results[0]: messagebox.showerror('重连素材', results[0], parent=window)
                refresh()
            window.after(100, poll)

        def relink_individually():
            for old in missing_media(timeline):
                path = filedialog.askopenfilename(parent=window, title='指定素材：'+Path(old).name,
                    initialfile=Path(old).name, filetypes=[('音视频素材', '*.*')])
                if not path: break
                for clip in timeline.clips:
                    if clip.path == old: clip.path = path
            refresh()

        def publish():
            if not self._available(): return
            if self.project is not original:
                messagebox.showerror('导入时间线', '当前工程已改变，请重新导入。', parent=window); return
            try: candidate = build_project(timeline, collapsed=collapsed)
            except ValueError as error:
                messagebox.showerror('导入时间线', str(error), parent=window); return
            window.destroy()
            self._remember_timeline_change()
            self.stop_monitors()
            self.project = candidate
            self._reset_quick_proxy()
            self._camera_metadata_attempted.clear()
            self.camera_page = 0
            self.grade_target = None
            self._grade_target_chosen = False
            self.selected_program_range = None
            self.sync_results.clear()
            self._restore_timeline_range_fields()
            self._refresh(); self.fit_timeline(); self.seek(0.)
            self.status.set(f'已导入「{timeline.name}」；Ctrl+Z 撤销。离线素材 {len(missing_media(timeline))} 个。')

        buttons = ttk.Frame(body); buttons.pack(fill='x', pady=(12, 0))
        link = ttk.Button(buttons, text='重连素材文件夹…', command=relink); link.pack(side='left')
        manual_link = ttk.Button(buttons, text='逐个指定素材…', command=relink_individually); manual_link.pack(side='left', padx=6)
        ttk.Button(buttons, text='取消', command=window.destroy).pack(side='right')
        apply = ttk.Button(buttons, text='导入时间线', command=publish); apply.pack(side='right', padx=8)
        original = self.project
        refresh(); window.grab_set()

    def set_multicam_timeline_view(self, collapsed):
        if not self._available() or self.project.timeline_multicam_collapsed == collapsed: return False
        self._remember_timeline_change()
        self.project.timeline_multicam_collapsed = bool(collapsed)
        self._set_timeline_selection(set(), redraw=False)
        self.draw_timeline()
        self.status.set('已合并显示为多机位时间线；右键节目轨可展开。' if collapsed else '已在时间线上打开多机位，显示各机位源轨。')
        return True

    def _append_multicam_view_menu(self, menu):
        collapsed = self.project.timeline_multicam_collapsed
        menu.add_command(label='在时间线上打开多机位' if collapsed else '合并显示为多机位时间线',
                         command=lambda: self.set_multicam_timeline_view(not collapsed))
