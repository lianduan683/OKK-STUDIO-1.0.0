"""Dedicated Color and Deliver pages sharing the editor's project and HWNDs."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import os
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from editor_core import format_time
from program_edits import program_spans
from hardware_encoders import codec_label

BG, PANEL, INPUT = '#202124', '#292b2e', '#18191b'
INK, MUTED, ACCENT = '#dedfe3', '#969ba3', '#58a6d6'
FONT = 'Microsoft YaHei UI'


@dataclass
class RenderJob:
    identifier: str
    project: object
    settings: object
    output: str
    state: str = '等待渲染'
    progress: float = 0.0
    detail: str = ''


class FinishingWorkspace(tk.Frame):
    """Temporarily mount existing panels, restoring the complete editing layout."""

    def __init__(self, parent, workspace):
        super().__init__(parent, bg=BG)
        self.ws = workspace
        self.mode = 'edit'
        self.saved_layout = None
        self.saved_chrome = None
        self.saved_suspended = False
        self.saved_color_mode = False
        self.jobs: list[RenderJob] = []
        self.active_job = None
        self.running = False
        self.serial = 0
        self._clip_signature = None
        self._last_size = None
        px = workspace._px
        self.title_var = tk.StringVar(self)
        self.summary_var = tk.StringVar(self)
        header = tk.Frame(self, bg=INPUT)
        header.pack(fill='x')
        tk.Label(header, textvariable=self.title_var, bg=INPUT, fg=INK,
                 font=(FONT, 11, 'bold')).pack(side='left', padx=px(13), pady=px(8))
        tk.Label(header, textvariable=self.summary_var, bg=INPUT, fg=MUTED,
                 font=('Cascadia Mono', 9)).pack(side='right', padx=px(13))
        self.split = ttk.Panedwindow(self, orient='vertical', style='Multicam.TPanedwindow')
        self.split.pack(fill='both', expand=True)
        self.upper = ttk.Panedwindow(self.split, orient='horizontal', style='Multicam.TPanedwindow')
        self.settings_host = tk.Frame(self.upper, bg=PANEL, width=px(340))
        self.settings_host.grid_columnconfigure(0, weight=1)
        self.settings_host.grid_rowconfigure(2, weight=1)
        self._heading(self.settings_host, '渲染设置 · Custom Export').grid(row=0, column=0, sticky='ew')
        output_row = tk.Frame(self.settings_host, bg=PANEL, padx=px(10), pady=px(8))
        output_row.grid(row=1, column=0, sticky='ew')
        tk.Label(output_row, text='输出文件', bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(anchor='w')
        path_row = tk.Frame(output_row, bg=PANEL)
        path_row.pack(fill='x', pady=(px(5), 0))
        ttk.Button(path_row, text='浏览…', command=workspace.choose_output, width=6,
                   style='Compact.TButton').pack(side='right', padx=(px(5), 0))
        tk.Entry(path_row, textvariable=workspace.output_var, bg=INPUT, fg=INK,
                 insertbackground=INK, relief='flat', font=(FONT, 9), width=12).pack(
                     side='left', fill='x', expand=True, ipady=px(5))
        self.viewer_host = tk.Frame(self.upper, bg=INPUT)
        self.viewer_host.grid_columnconfigure(0, weight=1)
        self.viewer_host.grid_rowconfigure(0, weight=1)
        transport = tk.Frame(self.viewer_host, bg=PANEL)
        transport.grid(row=1, column=0, sticky='ew')
        for title, command in (('|◀', lambda: workspace.seek(0)),
                               ('◀', lambda: workspace.step_frame(-1)),
                               ('▶ / Ⅱ', workspace.toggle),
                               ('▶|', lambda: workspace.step_frame(1)),
                               ('监看', workspace.start_monitors)):
            ttk.Button(transport, text=title, command=command, width=6,
                       style='Compact.TButton').pack(side='left', padx=px(2), pady=px(5))
        tk.Label(transport, textvariable=workspace.time_var, bg=PANEL, fg=INK,
                 font=('Cascadia Mono', 10)).pack(side='right', padx=px(10))
        self.timeline_host = tk.Frame(self.split, bg=BG)
        self.timeline_host.grid_columnconfigure(0, weight=1)
        self.timeline_host.grid_rowconfigure(0, weight=1)
        self.color_host = tk.Frame(self.split, bg=BG)
        self.color_host.grid_columnconfigure(0, weight=1)
        self.color_host.grid_rowconfigure(0, weight=1)
        self.queue_host = tk.Frame(self.upper, bg=PANEL, width=px(280))
        self._build_queue()
        self.clips_host = tk.Frame(self.upper, bg=PANEL, width=px(260))
        self._heading(self.clips_host, '片段 · CLIPS').pack(fill='x')
        self.clips = ttk.Treeview(self.clips_host, columns=('camera', 'time'), show='headings',
                                 style='Multicam.Treeview', selectmode='browse', height=5)
        self.clips.heading('camera', text='机位')
        self.clips.heading('time', text='片段起点')
        self.clips.column('camera', width=px(75), minwidth=px(45), stretch=False)
        self.clips.column('time', width=px(140), minwidth=px(80))
        self.clips.pack(fill='both', expand=True, padx=px(8), pady=px(6))
        self.clips.bind('<<TreeviewSelect>>', self._select_clip)
        tk.Label(self.clips_host, text='选择片段后调整下方色轮或曲线。\n调色范围、机位和全片设置随工程保存。',
                 bg=PANEL, fg=MUTED, font=(FONT, 9), justify='left', wraplength=px(230)).pack(
                     fill='x', padx=px(12), pady=px(12))
        self.bind('<Destroy>', self._destroy, add='+')
        self.bind('<Configure>', self._resize, add='+')

    def _heading(self, parent, title):
        return tk.Label(parent, text=title, bg=INPUT, fg=INK, anchor='w',
                        padx=self.ws._px(12), pady=self.ws._px(9), font=(FONT, 10, 'bold'))

    def _build_queue(self):
        px = self.ws._px
        self._heading(self.queue_host, '渲染队列 · RENDER QUEUE').pack(fill='x')
        self.queue_status = tk.StringVar(self, value='添加任务后开始渲染')
        actions = tk.Frame(self.queue_host, bg=PANEL)
        actions.pack(side='bottom', fill='x', padx=px(10), pady=px(10))
        self.render_button = ttk.Button(actions, text='渲染所有', command=self.start_queue,
                                        style='Accent.TButton')
        self.render_button.pack(fill='x', pady=px(4))
        line = tk.Frame(actions, bg=PANEL)
        line.pack(fill='x')
        self.remove_button = ttk.Button(line, text='移除任务', command=self.remove_selected,
                                        style='Compact.TButton')
        self.remove_button.pack(side='left', fill='x', expand=True)
        self.retry_button = ttk.Button(line, text='重新排队', command=self.retry_selected,
                                       style='Compact.TButton')
        self.retry_button.pack(side='left', fill='x', expand=True)
        self.stop_button = ttk.Button(actions, text='停止渲染', command=self.stop_queue,
                                      state='disabled', style='Compact.TButton')
        self.stop_button.pack(fill='x', pady=px(4))
        tk.Label(actions, textvariable=self.queue_status, bg=PANEL, fg=MUTED,
                 font=(FONT, 9), wraplength=px(245), justify='left').pack(fill='x', pady=px(5))
        self.queue_canvas = tk.Canvas(self.queue_host, bg=PANEL, highlightthickness=0, width=1, height=1)
        scroll = ttk.Scrollbar(self.queue_host, orient='vertical', command=self.queue_canvas.yview)
        scroll.pack(side='right', fill='y')
        self.queue_canvas.pack(fill='both', expand=True)
        self.queue_canvas.configure(yscrollcommand=scroll.set)
        self.cards = tk.Frame(self.queue_canvas, bg=PANEL)
        self._cards_window = self.queue_canvas.create_window(0, 0, anchor='nw', window=self.cards)
        self.cards.bind('<Configure>', lambda _e: self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox('all')))
        self.queue_canvas.bind('<Configure>', lambda e: self.queue_canvas.itemconfigure(self._cards_window, width=e.width))
        self.selected_job = tk.StringVar(self)
        self.job_widgets = {}
        self.empty_queue = tk.Label(self.cards, text='队列为空\n\n在左侧设置成片参数，\n点击“添加到渲染队列”。',
                                    bg=PANEL, fg=MUTED, justify='center', font=(FONT, 10))
        self.empty_queue.pack(fill='x', pady=px(38))

    def show(self, mode):
        if mode == self.mode:
            return
        self._leave()
        if mode not in {'color', 'delivery'}:
            return
        ws = self.ws
        # Run the editor's pending initial layout restoration before capturing
        # it; an after-idle restore must never unmount the Deliver settings.
        ws.update_idletasks()
        ws.pause()
        ws._cancel_grade_drafts()
        self.saved_suspended = ws._dock_saving_suspended
        self.saved_color_mode = ws._color_mode
        self.saved_layout = copy.deepcopy(ws.dock_manager.serialize())
        self.saved_chrome = ws._workspace_chrome_state()
        ws._dock_saving_suspended = True
        if ws._dock_save_after is not None:
            ws.after_cancel(ws._dock_save_after)
            ws._dock_save_after = None
        # Detach through the docking controller so floating and tabbed panels
        # keep their HWNDs and can return to their exact original locations.
        for key in ('program', 'timeline', 'grading', 'delivery'):
            ws.dock_manager.hide(key)
        ws._color_mode = False
        self.mode = mode
        ws.workspace_split.grid_remove()
        ws.workspace_bar.grid_remove()
        self.grid(row=1, column=0, sticky='nsew')
        for child in tuple(self.upper.panes()):
            self.upper.forget(child)
        for child in tuple(self.split.panes()):
            self.split.forget(child)
        self.split.add(self.upper, weight=5)
        if mode == 'delivery':
            self.title_var.set('交付 · DELIVER')
            self.upper.add(self.settings_host, weight=0)
            self.upper.add(self.viewer_host, weight=1)
            self.upper.add(self.queue_host, weight=0)
            ws.delivery_tab.grid(in_=self.settings_host, row=2, column=0, sticky='nsew')
            ws.delivery_export_button.configure(text='添加到渲染队列', command=self.add_job)
            self.split.add(self.timeline_host, weight=2)
            ws.timeline_section.grid(in_=self.timeline_host, row=0, column=0, sticky='nsew')
        else:
            self.title_var.set('调色 · COLOR')
            self.upper.add(self.viewer_host, weight=1)
            self.upper.add(self.clips_host, weight=0)
            self.split.add(self.color_host, weight=4)
            ws.color_workspace.grid(in_=self.color_host, row=0, column=0, sticky='nsew')
            ws.color_workspace.set_full_page(True)
            ws._color_mode = True
            if ws.selected_program_range is None:
                span = ws._program_span_at(ws.current)
                if span is not None:
                    ws.selected_program_range = (span.start, span.end)
            ws.grade_target = 'selection' if ws.selected_program_range else None
            ws.set_grade_target(ws.grade_target)
            ws.color_workspace.refresh()
            self.refresh_clips()
        ws.program_panel.grid(in_=self.viewer_host, row=0, column=0, sticky='nsew')
        ws.program_panel.lift()
        ws._tag_transport_widgets(self)
        self.update_idletasks()
        self._size_panes()
        self.refresh_summary()
        ws.draw_timeline()
        if ws.project.clips and not ws.player and ws.app.mpv_path and not ws.busy and not ws.app.working:
            ws.start_monitors()
        if mode == 'color':
            ws._audition_grade_target()
        self._raise_active_panels()
        ws.status.set('调色页 · 色轮、RGB 曲线与自动调色' if mode == 'color' else
                      '导出页 · 设置输出参数，添加到渲染队列后点击“渲染所有”。')

    def _size_panes(self):
        if self.mode == 'edit':
            return
        px = self.ws._px
        width, height = self.upper.winfo_width(), self.split.winfo_height()
        if self.mode == 'delivery' and width > 10:
            left = min(px(340), round(width * .29))
            right = min(px(280), round(width * .24))
            self.upper.sashpos(0, left)
            self.upper.sashpos(1, max(left + px(240), width - right))
        elif self.mode == 'color' and width > 10:
            self.upper.sashpos(0, width - min(px(260), round(width * .23)))
        if height > 10:
            delivery_ratio = .67 if height < px(800) else .59
            top = round(height * delivery_ratio) if self.mode == 'delivery' else min(
                round(height * .46), max(px(140), height - px(410)))
            self.split.sashpos(0, top)

    def _raise_active_panels(self):
        # These panels have the editor as their native parent; their geometry
        # hosts are descendants of this newer frame. Keep the leaves above it.
        if self.mode == 'edit':
            return
        self.ws.program_panel.lift()
        if self.mode == 'color':
            self.ws.color_workspace.lift()
        else:
            self.ws.delivery_tab.lift()
            self.ws.timeline_section.lift()

    def _resize(self, event):
        if event.widget is self and self._last_size != (event.width, event.height):
            self._last_size = event.width, event.height
            self.after_idle(self._size_panes)
            self.after_idle(self._raise_active_panels)

    def _leave(self):
        if self.mode == 'edit':
            return
        ws = self.ws
        ws._cancel_grade_drafts()
        if ws.player:
            ws.player.submit('audition', camera=None)
        ws._color_mode = False
        ws.color_workspace.set_full_page(False)
        for widget in (ws.program_panel, ws.timeline_section, ws.color_workspace, ws.delivery_tab):
            widget.grid_forget()
        self.grid_remove()
        ws.workspace_split.grid()
        ws.workspace_bar.grid()
        ws.delivery_export_button.configure(text='开始导出', command=ws.export)
        self.mode = 'edit'
        ws.dock_manager.restore(self.saved_layout)
        ws._color_mode = self.saved_color_mode
        ws.update_idletasks()
        # Restore only the split positions. Reapplying the camera-grid settings
        # would recreate decoder surfaces even though the grid never changed.
        for pane, field, horizontal in ((ws.body, 'body_sashes', True),
                                        (ws.workspace_split, 'main_sashes', False)):
            length = pane.winfo_width() if horizontal else pane.winfo_height()
            for index, ratio in enumerate(self.saved_chrome.get(field, ())):
                if index < len(pane.panes()) - 1:
                    pane.sashpos(index, round(length * ratio))
        ws._dock_saving_suspended = self.saved_suspended
        self.saved_layout = self.saved_chrome = None
        ws._tag_transport_widgets(ws)
        ws._bind_floating_transport()
        ws.draw_timeline()

    def refresh_summary(self):
        ws = self.ws
        self.summary_var.set(f'时间线  {ws.project.fps:g} fps  ·  时长 {format_time(ws.project.duration)}')

    def refresh_clips(self):
        spans = program_spans(self.ws.project)
        signature = tuple((span.start, span.end, span.camera) for span in spans)
        if signature != self._clip_signature:
            self._clip_signature = signature
            self.clips.delete(*self.clips.get_children())
            for index, span in enumerate(spans):
                self.clips.insert('', 'end', iid=str(index), values=(f'机位 {span.camera}', format_time(span.start)))
        self.refresh_summary()

    def _select_clip(self, _event=None):
        selection = self.clips.selection()
        if not selection or self.mode != 'color':
            return
        spans = program_spans(self.ws.project)
        index = int(selection[0])
        if index < len(spans):
            span = spans[index]
            self.ws.select_program_range(span.start, span.end)

    def add_job(self):
        ws = self.ws
        if not ws._available():
            return
        try:
            prepared = ws.prepare_delivery_export()
            if prepared is None:
                return
            project, settings, output = prepared
            target = os.path.normcase(str(Path(output).resolve()))
            if any(os.path.normcase(str(Path(job.output).resolve())) == target and
                   job.state in {'等待渲染', '正在渲染'} for job in self.jobs):
                raise ValueError('队列中已有相同输出文件，请修改保存位置或文件名。')
        except Exception as error:
            messagebox.showerror('添加渲染任务', str(error), parent=ws)
            return
        self.serial += 1
        job = RenderJob(str(self.serial), copy.deepcopy(project), copy.deepcopy(settings), output)
        self.jobs.append(job)
        self.selected_job.set(job.identifier)
        self._add_card(job)
        self.queue_status.set(f'{sum(j.state == "等待渲染" for j in self.jobs)} 个任务等待渲染')
        ws.status.set('已添加渲染任务：' + output)

    def _add_card(self, job):
        px = self.ws._px
        self.empty_queue.pack_forget()
        card = tk.Frame(self.cards, bg=INPUT, highlightthickness=1,
                        highlightbackground='#41454b', padx=px(10), pady=px(9))
        card.pack(fill='x', padx=px(8), pady=px(6))
        tk.Radiobutton(card, text=f'作业 {job.identifier}  ·  {Path(job.output).name}',
                       variable=self.selected_job, value=job.identifier, bg=INPUT, fg=INK,
                       selectcolor=INPUT, activebackground=INPUT, activeforeground=INK,
                       anchor='w', font=(FONT, 10, 'bold'), wraplength=px(225)).pack(fill='x')
        tk.Label(card, text=job.output, bg=INPUT, fg=MUTED, anchor='w', justify='left',
                 wraplength=px(225), font=(FONT, 8)).pack(fill='x', pady=px(5))
        duration = (job.project.range_end if job.project.range_end is not None else
                    job.project.duration) - job.project.range_start
        resolution = {'original': '源分辨率', 'timeline': '时间线分辨率'}.get(
            job.settings.resolution, job.settings.resolution)
        tk.Label(card, text=f'{codec_label(job.settings.codec)} · {job.project.fps:g} fps\n{format_time(duration)} · {resolution}',
                 bg=INPUT, fg=MUTED, anchor='w', justify='left', font=(FONT, 9)).pack(fill='x')
        status = tk.StringVar(self, value=job.state)
        tk.Label(card, textvariable=status, bg=INPUT, fg=ACCENT, anchor='w', justify='left',
                 wraplength=px(225), font=(FONT, 9)).pack(fill='x', pady=(px(8), px(3)))
        progress = tk.DoubleVar(self, value=job.progress)
        ttk.Progressbar(card, variable=progress, maximum=100).pack(fill='x')
        self.job_widgets[job.identifier] = (card, status, progress)

    def _update_card(self, job):
        _card, status, progress = self.job_widgets[job.identifier]
        status.set(f'{job.state}  {job.progress:.1f}%' + (f'\n{job.detail}' if job.detail else ''))
        progress.set(job.progress)

    def remove_selected(self):
        job = next((j for j in self.jobs if j.identifier == self.selected_job.get()), None)
        if job is None or job is self.active_job:
            return
        self.jobs.remove(job)
        self.job_widgets.pop(job.identifier)[0].destroy()
        if not self.jobs:
            self.empty_queue.pack(fill='x', pady=self.ws._px(38))
        self.queue_status.set(f'队列中 {len(self.jobs)} 个任务')

    def retry_selected(self):
        job = next((j for j in self.jobs if j.identifier == self.selected_job.get()), None)
        if job is None or job.state in {'等待渲染', '正在渲染'} or self.running:
            return
        if any(j is not job and j.output == job.output and j.state == '等待渲染' for j in self.jobs):
            return
        job.state, job.progress, job.detail = '等待渲染', 0, ''
        self._update_card(job)
        self.queue_status.set('任务已重新排队')

    def start_queue(self):
        if self.running or not self.ws._available():
            return
        if not any(j.state == '等待渲染' for j in self.jobs):
            self.queue_status.set('请先添加渲染任务')
            return
        self.running = True
        self.render_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self._next_job()

    def _next_job(self):
        if not self.running or not self.ws.alive:
            self._idle('已停止渲染')
            return
        job = next((j for j in self.jobs if j.state == '等待渲染'), None)
        if job is None:
            self._idle('全部任务已完成')
            return
        self.active_job = job
        job.state, job.detail = '正在渲染', '正在准备'
        self._update_card(job)
        self.queue_status.set(f'正在渲染作业 {job.identifier}')
        if not self.ws.export(render_job=job):
            job.state, job.detail = '等待渲染', ''
            self._update_card(job)
            self._idle('渲染未开始，请检查输出设置')

    def update_metrics(self, snapshot):
        if self.active_job is None:
            return
        job = self.active_job
        job.progress = max(0, min(100, float(snapshot.get('progress_percent') or 0)))
        job.detail = str(snapshot.get('phase') or '')
        fps = float(snapshot.get('fps') or 0)
        if fps > 0:
            job.detail += f' · {fps:.1f} fps'
        self._update_card(job)

    def complete(self, *, error='', cancelled=False):
        job = self.active_job
        if job is None:
            return False
        job.state = '已取消' if cancelled else '失败' if error else '已完成'
        job.progress = job.progress if error or cancelled else 100
        job.detail = error if error and not cancelled else ''
        self._update_card(job)
        self.active_job = None
        if error or cancelled:
            self._idle('渲染已停止；其余任务仍在等待')
        else:
            self.ws.after_idle(self._next_job)
        return True

    def stop_queue(self):
        self.running = False
        if self.active_job is not None:
            self.ws.app._cancel()
            self.queue_status.set('正在停止当前任务…')
        else:
            self._idle('已停止渲染')

    def _idle(self, text):
        self.running = False
        self.active_job = None
        self.render_button.configure(state='normal')
        self.stop_button.configure(state='disabled')
        self.queue_status.set(text)

    def _destroy(self, event):
        if event.widget is self:
            self.running = False
