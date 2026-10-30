"""Standard-camera controls and real sampled before/after previews."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from camera_match import METHODS, SAMPLE_WIDTH, SAMPLE_HEIGHT
from editor_core import format_time, parse_timecode

BG, PANEL, INPUT = '#202124', '#292b2e', '#18191b'
INK, MUTED, ACCENT = '#dedfe3', '#969ba3', '#58a6d6'
FONT = 'Microsoft YaHei UI'


class CameraMatchPanel(tk.Frame):
    def __init__(self, parent, owner):
        super().__init__(parent, bg=BG)
        self.owner, self.ws = owner, owner.ws
        self.reference_var = tk.StringVar(self)
        self.reference_time_var = tk.StringVar(self, value=format_time(getattr(self.ws, 'current', 0.)))
        self.range_var = tk.StringVar(self, value='当前片段（同场景）')
        self.method_var = tk.StringVar(self, value=METHODS['anchors'])
        self.strength_var = tk.DoubleVar(self, value=100.)
        self.status_var = getattr(self.ws, 'camera_match_status', None) or tk.StringVar(self, value='选择标准机位，再勾选需要匹配的其他机位。')
        self._running = False
        self._reference_choices = {}
        self._checked = set()
        self._cameras = ()
        self._reference = None
        self._results = {}
        self._photos = []
        self.details_var = tk.StringVar(self)
        self.options = []
        px = owner.px
        style = ttk.Style(self)
        style.configure('CameraMatch.TEntry', fieldbackground=INPUT, foreground=INK,
                        insertcolor=INK, bordercolor='#41454b')
        style.map('CameraMatch.TEntry', fieldbackground=[('disabled', PANEL)],
                  foreground=[('disabled', MUTED)])
        style.configure('CameraMatch.Horizontal.TScale', background=BG, troughcolor=INPUT)
        footer = tk.Frame(self, bg=PANEL)
        footer.pack(side='bottom', fill='x')
        self.apply_button = ttk.Button(footer, text='匹配所选机位', command=self.apply, style='Accent.TButton')
        self.apply_button.pack(side='left', padx=px(8), pady=px(6))
        self.cancel_button = ttk.Button(footer, text='取消', command=self.cancel, state='disabled', style='Compact.TButton')
        self.cancel_button.pack(side='left', padx=px(4))
        self.view_button = ttk.Button(footer, text='监看所选机位', command=self.view_selected, style='Compact.TButton')
        self.view_button.pack(side='right', padx=px(8))
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, width=1, height=1)
        scrollbar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        body = tk.Frame(self.canvas, bg=BG)
        window = self.canvas.create_window(0, 0, anchor='nw', window=body)
        body.bind('<Configure>', lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(window, width=e.width))
        reference = tk.Frame(body, bg=BG)
        reference.pack(fill='x', padx=px(10), pady=(px(8), px(4)))
        tk.Label(reference, text='标准机位', bg=BG, fg=INK, font=(FONT, 9)).pack(side='left')
        self.reference_combo = ttk.Combobox(reference, textvariable=self.reference_var, state='readonly', width=25)
        self.reference_combo.pack(side='left', fill='x', expand=True, padx=px(7))
        self.reference_combo.bind('<<ComboboxSelected>>', self._reference_changed)
        self.options.append(self.reference_combo)
        current = ttk.Button(reference, text='当前机位设为标准', command=self.use_current, style='Compact.TButton')
        current.pack(side='right'); self.options.append(current)
        settings = tk.Frame(body, bg=BG)
        settings.pack(fill='x', padx=px(10), pady=px(3))
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)
        for column, title, variable, choices in (
            (0, '取样范围', self.range_var, ['当前片段（同场景）', '整个工程']),
            (2, '匹配方式', self.method_var, list(METHODS.values()))):
            tk.Label(settings, text=title, bg=BG, fg=MUTED, font=(FONT, 9)).grid(row=0, column=column, padx=px(4))
            combo = ttk.Combobox(settings, textvariable=variable, values=choices, state='readonly', width=18)
            combo.grid(row=0, column=column + 1, sticky='ew', padx=px(4)); self.options.append(combo)
        timing = tk.Frame(body, bg=BG)
        timing.pack(fill='x', padx=px(10), pady=px(4))
        tk.Label(timing, text='参考调色时刻', bg=BG, fg=MUTED, font=(FONT, 9)).pack(side='left')
        entry = ttk.Entry(timing, textvariable=self.reference_time_var, width=15, style='CameraMatch.TEntry')
        entry.pack(side='left', padx=px(6)); self.options.append(entry)
        time_button = ttk.Button(timing, text='用当前播放头', command=self.use_playhead, style='Compact.TButton')
        time_button.pack(side='left'); self.options.append(time_button)
        tk.Label(timing, text='匹配强度', bg=BG, fg=MUTED, font=(FONT, 9)).pack(side='left', padx=(px(16), px(5)))
        strength = ttk.Scale(timing, from_=0, to=100, variable=self.strength_var, style='CameraMatch.Horizontal.TScale')
        strength.pack(side='left', fill='x', expand=True); self.options.append(strength)
        self.strength_label = tk.Label(timing, text='100%', bg=BG, fg=ACCENT, width=5, font=(FONT, 9))
        self.strength_label.pack(side='left')
        self.strength_var.trace_add('write', lambda *_: self.strength_label.configure(text=f'{self.strength_var.get():.0f}%'))
        selection = tk.Frame(body, bg=BG)
        selection.pack(fill='x', padx=px(10), pady=(px(5), 0))
        tk.Label(selection, text='目标机位 · 勾选可多选', bg=BG, fg=INK, font=(FONT, 9)).pack(side='left')
        for title, command in (('全选其他', self.select_all), ('清空', self.clear_selection)):
            button = ttk.Button(selection, text=title, command=command, style='Compact.TButton')
            button.pack(side='right', padx=px(3)); self.options.append(button)
        self.targets = ttk.Treeview(body, columns=('camera', 'result'), show='tree headings',
                                   style='Multicam.Treeview', height=3, selectmode='browse')
        self.targets.heading('#0', text='选择'); self.targets.column('#0', width=px(42), stretch=False)
        self.targets.heading('camera', text='目标机位'); self.targets.column('camera', width=px(150), minwidth=px(80))
        self.targets.heading('result', text='匹配结果'); self.targets.column('result', width=px(250), minwidth=px(100))
        self.targets.pack(fill='x', padx=px(10), pady=px(4))
        self.targets.bind('<Button-1>', self._toggle)
        self.targets.bind('<<TreeviewSelect>>', self._show_selected)
        self.targets.bind('<Double-1>', lambda _e: self.view_selected())
        note = tk.Label(body, text='使用标准机位已调色的效果，优先比较同时录制的中性色和肤色。'
                        '结果应用到目标整机位，保留色轮、曲线和 LUT；标准机位保持原样。'
                        '构图差异较大时请使用默认方式，灯光变化时建议分场景取样。',
                        bg=BG, fg=MUTED, font=(FONT, 8), justify='left', anchor='w')
        note.pack(fill='x', padx=px(10), pady=px(4))
        self.status_label = tk.Label(body, textvariable=self.status_var, bg=BG, fg=INK,
                                     font=(FONT, 9), justify='left', anchor='w')
        self.status_label.pack(fill='x', padx=px(10), pady=px(4))
        self.details_label = tk.Label(body, textvariable=self.details_var, bg=BG, fg=MUTED,
                                      font=(FONT, 8), justify='left', anchor='w')
        self.details_label.pack(fill='x', padx=px(10))
        previews = tk.Frame(body, bg=BG)
        previews.pack(fill='x', padx=px(10), pady=px(5))
        self.preview_labels = []
        for index, title in enumerate(('标准效果', '目标原效果', '匹配后')):
            cell = tk.Frame(previews, bg=INPUT)
            cell.grid(row=0, column=index, sticky='nsew', padx=px(3))
            previews.columnconfigure(index, weight=1)
            tk.Label(cell, text=title, bg=INPUT, fg=MUTED, font=(FONT, 8)).pack(fill='x')
            label = tk.Label(cell, text='分析后显示', bg=INPUT, fg=MUTED, font=(FONT, 8))
            label.pack(pady=px(4)); self.preview_labels.append(label)
        body.bind('<Configure>', lambda e: [label.configure(wraplength=max(px(160), e.width - px(24)))
                  for label in (note, self.status_label, self.details_label)], add='+')
        self.refresh_choices()

    def refresh_choices(self):
        selected = self.targets.selection()
        cameras = tuple(self.ws.project.cameras)
        reference = getattr(self.ws.project, 'color_reference_camera', None)
        self._reference_choices = {getattr(self.ws, '_label_camera', lambda c: f'机位 {c}')(c): c for c in cameras}
        self.reference_combo.configure(values=list(self._reference_choices))
        self.reference_var.set(next((label for label, camera in self._reference_choices.items() if camera == reference), ''))
        if cameras != self._cameras or reference != self._reference:
            self._checked = set(cameras) - {reference}
            self._cameras, self._reference = cameras, reference
            self._results.clear()
            self.details_var.set('')
            self._photos = []
            for label in self.preview_labels:
                label.configure(image='', text='分析后显示')
        for camera, result in tuple(self._results.items()):
            if result.grade is not None and self.ws.project.camera_grades.get(camera) != result.grade:
                del self._results[camera]
        self.targets.delete(*self.targets.get_children())
        for label, camera in self._reference_choices.items():
            if camera != reference:
                result = self._results.get(camera)
                text = self._result_text(result) if result else '待匹配'
                self.targets.insert('', 'end', iid=str(camera), text='☑' if camera in self._checked else '☐', values=(label, text))
        selected = tuple(item for item in selected if self.targets.exists(item))
        if selected:
            self.targets.selection_set(selected)
        self.apply_button.configure(state='disabled' if self._running or len(cameras) < 2 or reference not in cameras else 'normal')
        self.reference_combo.configure(state='disabled' if self._running else 'readonly')

    @staticmethod
    def _result_text(result):
        return f'已匹配 · 抽样差异降低 {result.improvement:.0f}%' if result.grade is not None else result.message

    def _reference_changed(self, _event=None):
        callback = getattr(self.ws, 'set_color_reference_camera', None)
        if callable(callback):
            callback(self._reference_choices.get(self.reference_var.get()))
        self.use_playhead()
        self.refresh_choices()

    def use_playhead(self):
        self.reference_time_var.set(format_time(self.ws.current))

    def use_current(self):
        camera = getattr(self.ws, '_displayed_camera', None) or self.ws.project.camera_at(self.ws.current)
        if camera in self.ws.project.cameras:
            self.ws.set_color_reference_camera(camera)
            self.use_playhead()
            self.refresh_choices()

    def select_all(self):
        if not self._running:
            self._checked = set(self._cameras) - {self._reference}
            self.refresh_choices()

    def clear_selection(self):
        if not self._running:
            self._checked.clear()
            self.refresh_choices()

    def _toggle(self, event):
        item = self.targets.identify_row(event.y)
        if item and not self._running and self.targets.identify_column(event.x) == '#0':
            camera = int(item)
            self._checked.symmetric_difference_update({camera})
            self.targets.item(item, text='☑' if camera in self._checked else '☐')

    def apply(self):
        try:
            reference = self._reference_choices.get(self.reference_var.get())
            method = next(key for key, label in METHODS.items() if label == self.method_var.get())
            time = parse_timecode(self.reference_time_var.get())
            callback = getattr(self.ws, 'start_camera_match', None)
            if callable(callback):
                callback(reference, sorted(self._checked), range_mode='project' if self.range_var.get() == '整个工程' else 'selection',
                         method=method, strength=self.strength_var.get() / 100, reference_time=time)
        except (ValueError, StopIteration) as error:
            messagebox.showerror('标准机位匹配', str(error), parent=self)

    def cancel(self):
        self.ws.cancel_camera_match()

    def set_running(self, value):
        self._running = bool(value)
        for widget in self.options:
            widget.state(['disabled'] if value else ['!disabled'])
        self.cancel_button.configure(state='normal' if value and getattr(self.ws, '_camera_match_running', False) else 'disabled')
        self.refresh_choices()

    def show_results(self, results):
        self._results = {item.camera: item for item in results}
        self.refresh_choices()
        first = next((item for item in results if item.grade is not None), next(iter(results), None))
        if first and self.targets.exists(str(first.camera)):
            self.targets.selection_set(str(first.camera))
            self._show_selected()

    def _show_selected(self, _event=None):
        selected = self.targets.selection()
        result = self._results.get(int(selected[0])) if selected else None
        if result is None:
            self.details_var.set('')
            self._photos = []
            for label in self.preview_labels:
                label.configure(image='', text='分析后显示')
            return
        self.details_var.set(f'机位 {result.camera} · {result.samples} 帧 · {result.message}')
        self._photos = []
        for label, pixels in zip(self.preview_labels, (result.reference_pixels, result.target_pixels, result.matched_pixels)):
            if pixels:
                photo = tk.PhotoImage(master=self, data=f'P6\n{SAMPLE_WIDTH} {SAMPLE_HEIGHT}\n255\n'.encode() + pixels, format='PPM')
                if self.canvas.winfo_width() < SAMPLE_WIDTH * 3 + self.owner.px(60):
                    photo = photo.subsample(2)
                self._photos.append(photo)
                label.configure(image=photo, text='')
            else:
                label.configure(image='', text='无可用样本')

    def view_selected(self):
        selected = self.targets.selection()
        if selected:
            camera = int(selected[0])
            self.ws.set_grade_target(camera)
            self.ws._audition_grade_target()
