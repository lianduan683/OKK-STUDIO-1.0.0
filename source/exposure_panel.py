"""Exposure inspection UI. Local screening and external model reviews stay distinct."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from editor_core import format_time
from exposure_check import workspace_service
from transition_preview import transition_signature
from vision_review import CLASSES, PROTOCOLS, VisionConfig, load_profile, save_profile

BG, PANEL, INPUT, INK, MUTED, ACCENT = '#202124', '#292b2e', '#18191b', '#dedfe3', '#969ba3', '#58a6d6'
FONT = 'Microsoft YaHei UI'


class ExposurePanel(tk.Frame):
    def __init__(self, parent, owner):
        super().__init__(parent, bg=BG)
        self.owner, self.ws = owner, owner.ws
        self.scan_id = ''
        self.report = None
        self._generation = None
        self._photo = None
        self._original_photo = None
        self._closed = False
        self._after = None
        px = owner.px
        self.profile_path = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OKK STUDIO' / 'exposure-model.json'
        profile = load_profile(self.profile_path)
        self.scope_var = tk.StringVar(self, value='当前片段')
        self.view_var = tk.StringVar(self, value='当前调色效果')
        self.interval_var = tk.StringVar(self, value='5')
        self.limit_var = tk.StringVar(self, value='120')
        self.status_var = tk.StringVar(self, value='先做本地抽样筛查，再通过模型 API 或 MCP harness 复核。')
        self.details_var = tk.StringVar(self)
        self.protocol_var = tk.StringVar(self, value=PROTOCOLS[profile.protocol])
        self.base_var = tk.StringVar(self, value=profile.base_url)
        self.model_var = tk.StringVar(self, value=profile.model)
        self.key_var = tk.StringVar(self)
        self.timeout_var = tk.StringVar(self, value=str(profile.timeout))
        self.json_var = tk.BooleanVar(self, value=profile.json_mode)
        toolbar = tk.Frame(self, bg=PANEL)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        toolbar.grid(row=0, column=0, sticky='ew')
        for title, variable, choices, width in (
            ('范围', self.scope_var, ['当前片段', '整条成片', '所有机位'], 12),
            ('画面', self.view_var, ['当前调色效果', '原片'], 13),
            ('间隔（秒）', self.interval_var, ['0.5', '1', '2', '5', '10', '30'], 5),
            ('上限（帧）', self.limit_var, ['30', '60', '120', '240', '300'], 5)):
            tk.Label(toolbar, text=title, bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(side='left', padx=px(4))
            combo = ttk.Combobox(toolbar, textvariable=variable, values=choices, state='readonly', width=width)
            combo.pack(side='left', padx=px(3), pady=px(6))
        self.scan_button = ttk.Button(toolbar, text='开始本地筛查', command=self.start, style='Accent.TButton')
        self.scan_button.pack(side='left', padx=px(8))
        self.cancel_button = ttk.Button(toolbar, text='取消', command=self.cancel, style='Compact.TButton')
        self.cancel_button.pack(side='left', padx=px(3))
        self.tabs = ttk.Notebook(self, style='Multicam.TNotebook')
        self.tabs.grid(row=1, column=0, sticky='nsew')
        results, settings_host = tk.Frame(self.tabs, bg=BG), tk.Frame(self.tabs, bg=BG)
        self.tabs.add(results, text='检测结果'); self.tabs.add(settings_host, text='模型接入 / MCP')
        settings_canvas = tk.Canvas(settings_host, bg=BG, highlightthickness=0, height=1)
        settings_scroll = ttk.Scrollbar(settings_host, orient='vertical', command=settings_canvas.yview)
        settings_scroll.pack(side='right', fill='y')
        settings_canvas.pack(fill='both', expand=True)
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        settings = tk.Frame(settings_canvas, bg=BG)
        settings_window = settings_canvas.create_window(0, 0, window=settings, anchor='nw')
        settings.bind('<Configure>', lambda _e: settings_canvas.configure(scrollregion=settings_canvas.bbox('all')))
        settings_canvas.bind('<Configure>', lambda e: settings_canvas.itemconfigure(settings_window, width=e.width))
        results.columnconfigure(0, weight=3); results.columnconfigure(1, weight=2)
        results.rowconfigure(0, weight=1)
        left = tk.Frame(results, bg=BG)
        left.grid(row=0, column=0, sticky='nsew')
        self.tree = ttk.Treeview(left, columns=('camera', 'time', 'local', 'model'), show='headings',
                                 selectmode='extended', height=4, style='Multicam.Treeview')
        for name, title, width in (('camera', '机位', 65), ('time', '时间点', 115), ('local', '本地指标', 150), ('model', '模型复核', 150)):
            self.tree.heading(name, text=title); self.tree.column(name, width=px(width), minwidth=px(55))
        scrollbar = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        scrollbar.pack(side='right', fill='y'); self.tree.pack(fill='both', expand=True)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<<TreeviewSelect>>', self._selected)
        self.tree.bind('<Double-1>', lambda _e: self.locate())
        right = tk.Frame(results, bg=INPUT)
        right.grid(row=0, column=1, sticky='nsew', padx=px(6))
        right.columnconfigure(0, weight=1); right.rowconfigure(0, weight=1)
        preview_host = tk.Frame(right, bg=INPUT, width=1, height=1)
        preview_host.grid(row=0, column=0, sticky='nsew')
        preview_host.pack_propagate(False)
        self.preview = tk.Label(preview_host, text='选择抽样帧查看画面与判断依据', bg=INPUT, fg=MUTED, font=(FONT, 9))
        self.preview.pack(fill='both', expand=True)
        preview_host.bind('<Configure>', lambda e: self._fit_preview(e.width, e.height))
        self.details_label = tk.Label(right, textvariable=self.details_var, bg=INPUT, fg=INK,
                                      font=(FONT, 9), anchor='nw', justify='left')
        self.details_label.grid(row=1, column=0, sticky='ew', padx=px(8), pady=px(5))
        right.bind('<Configure>', lambda e: self.details_label.configure(wraplength=max(px(100), e.width - px(20))))
        actions = tk.Frame(results, bg=PANEL)
        actions.grid(row=1, column=0, columnspan=2, sticky='ew')
        for title, command in (('选择未复核疑点（最多20帧）', self.select_suspects),
                               ('发送所选帧至模型复核', self.review), ('定位时间线', self.locate), ('导出报告…', self.export)):
            ttk.Button(actions, text=title, command=command, style='Compact.TButton').pack(side='left', padx=px(4), pady=px(5))
        self._build_settings(settings)
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=ACCENT, anchor='w', justify='left',
                 font=(FONT, 9)).grid(row=2, column=0, sticky='ew', padx=px(8), pady=px(4))
        tk.Label(self, text='按间隔抽样，未采样时刻仍可能有问题。本地指标仅提示疑点；模型须结合主体及有意照明判断。',
                 bg=BG, fg=MUTED, font=(FONT, 8), anchor='w').grid(row=3, column=0, sticky='ew', padx=px(8), pady=(0, px(3)))
        self.bind('<Destroy>', self._destroy, add='+')
        self._after = self.after(250, self._poll)

    def _entry(self, parent, variable, row, title, secret=False):
        px = self.owner.px
        tk.Label(parent, text=title, bg=BG, fg=MUTED, font=(FONT, 9), anchor='w').grid(row=row, column=0, sticky='w', padx=px(8), pady=px(4))
        field = tk.Entry(parent, textvariable=variable, bg=INPUT, fg=INK, insertbackground=INK,
                         relief='flat', show='•' if secret else '', font=(FONT, 9))
        field.grid(row=row, column=1, sticky='ew', padx=px(8), pady=px(4), ipady=px(5))
        return field

    def _build_settings(self, parent):
        px = self.owner.px
        parent.columnconfigure(1, weight=1)
        tk.Label(parent, text='接口格式', bg=BG, fg=MUTED, font=(FONT, 9)).grid(row=0, column=0, padx=px(8))
        ttk.Combobox(parent, textvariable=self.protocol_var, values=list(PROTOCOLS.values()),
                     state='readonly', width=32).grid(row=0, column=1, sticky='ew', padx=px(8), pady=px(6))
        self._entry(parent, self.base_var, 1, 'Base URL（含版本路径）')
        self._entry(parent, self.model_var, 2, '模型名（须支持图片）')
        self._entry(parent, self.key_var, 3, 'API Key（仅本次会话）', True)
        line = tk.Frame(parent, bg=BG)
        line.grid(row=4, column=0, columnspan=2, sticky='ew', padx=px(8))
        tk.Label(line, text='超时（秒）', bg=BG, fg=MUTED, font=(FONT, 9)).pack(side='left')
        tk.Entry(line, textvariable=self.timeout_var, width=5, bg=INPUT, fg=INK, insertbackground=INK).pack(side='left', padx=px(6))
        tk.Checkbutton(line, text='JSON 模式（接口支持时开启）', variable=self.json_var, bg=BG, fg=INK,
                       selectcolor=INPUT, activebackground=BG, activeforeground=INK).pack(side='left', padx=px(8))
        ttk.Button(line, text='保存连接信息（不保存 Key）', command=self.save_connection, style='Compact.TButton').pack(side='left', padx=px(6))
        ttk.Button(line, text='清除 Key', command=lambda: self.key_var.set(''), style='Compact.TButton').pack(side='left')
        tk.Label(parent, text='OpenAI / Claude 的 Base URL 通常含 /v1；Gemini 通常含 /v1beta。也可填写完整请求端点。'
                 '\n只有主动点击“发送所选帧至模型复核”才向该接口发送所选抽样图；每次最多20帧，不自动重试。',
                 bg=BG, fg=MUTED, font=(FONT, 8), justify='left', anchor='w').grid(row=5, column=0, columnspan=2, sticky='ew', padx=px(8), pady=px(6))
        mcp = tk.Frame(parent, bg=PANEL)
        mcp.grid(row=6, column=0, columnspan=2, sticky='ew', padx=px(8), pady=px(5))
        ttk.Button(mcp, text='启用 / 关闭 MCP 连接', command=self.toggle_mcp, style='Compact.TButton').pack(side='left', padx=px(6), pady=px(6))
        tk.Label(mcp, text='外部 harness 可启动筛查、读取抽样图并回写复核结果；不需要把 Key 交给本软件。',
                 bg=PANEL, fg=MUTED, font=(FONT, 9), anchor='w').pack(side='left', padx=px(8))

    def config(self):
        protocol = next(key for key, value in PROTOCOLS.items() if value == self.protocol_var.get())
        return VisionConfig(protocol, self.base_var.get().strip(), self.model_var.get().strip(),
                            int(self.timeout_var.get()), self.json_var.get()).validate()

    def save_connection(self):
        try:
            save_profile(self.profile_path, self.config())
            self.status_var.set('连接信息已保存；API Key 只保留在本次会话。')
        except Exception as error: messagebox.showerror('模型接入', str(error), parent=self)

    def start(self):
        try:
            scope = self.scope_var.get()
            first, last = 0., self.ws.project.duration
            if scope == '当前片段':
                selected = self.ws.selected_program_range or ((span.start, span.end) if (span := self.ws._program_span_at(self.ws.current)) else None)
                if selected is None: raise ValueError('请先导入视频并选择检测片段。')
                first, last = selected
            options = dict(scope='cameras' if scope == '所有机位' else 'program', start=first, end=last,
                           view='source' if self.view_var.get() == '原片' else 'graded',
                           interval=float(self.interval_var.get()), max_frames=int(self.limit_var.get()))
            report = workspace_service(self.ws).start(self.ws.project, options)
            self.scan_id = report['scan_id']; self._generation = None
            self.tabs.select(0)
        except Exception as error: messagebox.showerror('曝光检测', str(error), parent=self)

    def _poll(self):
        self._after = None
        if self._closed: return
        service = vars(self.ws).get('_exposure_service')
        if service and service.latest_id:
            if not self.scan_id or self.scan_id != service.latest_id:
                self.scan_id = service.latest_id; self._generation = None
            try:
                report = service.get(self.scan_id)
                stamp = self.scan_id, report['generation']
                if stamp != self._generation:
                    self._generation = stamp; self.report = report
                    selected = self.tree.selection(); self.tree.delete(*self.tree.get_children())
                    for record in report['frames']:
                        local = CLASSES[record['local']['classification']] if record.get('local') else '读取失败'
                        review = record.get('review')
                        model = CLASSES[review['classification']] if review else '待复核'
                        self.tree.insert('', 'end', iid=record['frame_id'], values=(record['camera'], format_time(record['timeline_time']), local, model))
                    selected = tuple(item for item in selected if self.tree.exists(item))
                    if selected: self.tree.selection_set(selected)
                stale = transition_signature(self.ws.project) != report['scan_revision']
                self.status_var.set(('旧工程快照 · 请重新检测。 ' if stale else '') + report['message'] +
                    f' · {report["total_frames"]}/{report["planned_samples"]} 帧' + (' · 已按上限均匀抽样' if report['capped'] else ''))
                running = report['status'] == 'running' or report['review_status'] == 'running'
                self.scan_button.configure(state='disabled' if running else 'normal')
                self.cancel_button.configure(state='normal' if running else 'disabled')
            except (KeyError, ValueError): pass
        self._after = self.after(300, self._poll)

    def _selected(self, _event=None):
        selection = self.tree.selection()
        if not selection or not self.report: return
        record = next((item for item in self.report['frames'] if item['frame_id'] == selection[0]), None)
        if record is None: return
        review = record.get('review')
        text = record.get('read_error') or record['local']['reason']
        if review:
            text += f'\n模型：{CLASSES[review["classification"]]} · 自评 {review["confidence"]:.0%}\n{review["reason"]}'
            if review['regions']: text += '\n区域：' + '；'.join(review['regions'])
        self.details_var.set(text)
        try:
            item = workspace_service(self.ws).frame(self.scan_id, selection[0])
            self._original_photo = tk.PhotoImage(master=self, file=item['image_path'])
            host = self.preview.master
            self._fit_preview(host.winfo_width(), host.winfo_height())
        except (ValueError, tk.TclError):
            self._original_photo = None
            self.preview.configure(image='', text='无可用抽样图')

    def _fit_preview(self, width, height):
        photo = self._original_photo
        if photo is None: return
        factor = max(1, math.ceil(max(photo.width() / max(1, width - 4), photo.height() / max(1, height - 4))))
        self._photo = photo if factor == 1 else photo.subsample(factor)
        self.preview.configure(image=self._photo, text='')

    def select_suspects(self):
        if not self.report: return
        chosen = [f['frame_id'] for f in self.report['frames'] if f.get('local') and f['local']['classification'] != 'normal' and not f.get('review')][:20]
        self.tree.selection_set(chosen)

    def review(self):
        if not self.report: return
        try:
            workspace_service(self.ws).review_api(self.scan_id, list(self.tree.selection()), self.report['scan_revision'],
                                                self.config(), self.key_var.get(), self.ws.project)
        except Exception as error: messagebox.showerror('模型复核', str(error), parent=self)

    def cancel(self):
        if self.scan_id: workspace_service(self.ws).cancel(self.scan_id)

    def locate(self):
        selection = self.tree.selection()
        if selection and self.report:
            record = next(item for item in self.report['frames'] if item['frame_id'] == selection[0])
            self.ws.seek(record['timeline_time'])

    def export(self):
        if not self.scan_id: return
        path = filedialog.asksaveasfilename(parent=self, title='导出曝光检测报告', defaultextension='.json', filetypes=[('JSON', '*.json')])
        if path:
            Path(path).write_text(json.dumps(workspace_service(self.ws).get(self.scan_id), ensure_ascii=False, indent=2), encoding='utf-8')

    def toggle_mcp(self):
        callback = getattr(self.ws.app, '_toggle_mcp_bridge', None)
        if callable(callback): callback()

    def _destroy(self, event):
        if event.widget is self:
            self._closed = True; self.key_var.set('')
            if self._after is not None:
                self.after_cancel(self._after); self._after = None
