"""Opt-in background rendering, persistent local preferences and ruler status."""
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import ttk, filedialog

from prerender import COLORS, RESOLUTIONS, PrerenderWorker, check_folder, picture_signature, plan_blocks, validate_resolution


class PrerenderUIMixin:
    def _init_prerender(self):
        self.prerender_enabled_var = tk.BooleanVar(self, value=False)
        self.prerender_folder_var = tk.StringVar(self, value='')
        self._prerender_resolution = (1280, 720)
        self._prerender_source_rates = {}
        self.prerender_status_var = tk.StringVar(self, value='预渲染已关闭')
        self._prerender_events = queue.Queue()
        self._prerender_worker = None
        self._prerender_generation = 0
        self._prerender_blocks = []
        self._prerender_signature = None
        self._prerender_slow = set()
        self._prerender_pending = False
        self._prerender_running = False
        self._prerender_eligible_at = 0.
        self._prerender_lag = None
        self._prerender_media_check = 0.
        layout = getattr(self.app, 'workspace_layout_path', None)
        self._prerender_preferences = Path(layout).with_name('prerender-settings.json') if layout else None
        path = self._prerender_preferences
        if path and path.is_file():
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                if type(value.get('enabled')) is bool and isinstance(value.get('folder'), str):
                    self.prerender_enabled_var.set(value['enabled'])
                    self.prerender_folder_var.set(value['folder'])
                    self._prerender_resolution = validate_resolution(value.get('width', 1280), value.get('height', 720))
            except (OSError, ValueError): pass
        self._prerender_poll_after = self.after(200, self._poll_prerender)

    def _build_prerender_settings(self, panel, window):
        enabled = tk.BooleanVar(window, value=self.prerender_enabled_var.get())
        folder = tk.StringVar(window, value=self.prerender_folder_var.get())
        self._project_settings_vars.update(prerender_enabled=enabled, prerender_folder=folder)
        ttk.Checkbutton(panel, text='开启自动预渲染', variable=enabled).pack(anchor='w', pady=(0, 18))
        tk.Label(panel, text='预渲染视频保存位置', bg='#202124', fg='#dedfe3',
                 font=('Microsoft YaHei UI', 10)).pack(anchor='w')
        row = tk.Frame(panel, bg='#202124')
        row.pack(fill='x', pady=(8, 16))
        entry = ttk.Entry(row, textvariable=folder)
        entry.pack(side='left', fill='x', expand=True)
        def browse():
            path = filedialog.askdirectory(parent=window, title='选择预渲染视频保存位置',
                initialdir=folder.get() or None, mustexist=False)
            if path: folder.set(path)
        button = ttk.Button(row, text='浏览…', command=browse)
        button.pack(side='right', padx=(8, 0))
        resolution_row = tk.Frame(panel, bg='#202124')
        resolution_row.pack(fill='x', pady=(0, 10))
        tk.Label(resolution_row, text='预渲染分辨率', bg='#202124', fg='#dedfe3',
                 font=('Microsoft YaHei UI', 10)).pack(side='left')
        preset = tk.StringVar(window, value=next((name for name, size in RESOLUTIONS.items()
            if size == self._prerender_resolution), '自定义'))
        combo = ttk.Combobox(resolution_row, textvariable=preset, values=tuple(RESOLUTIONS), state='readonly', width=20)
        combo.pack(side='right')
        size_row = tk.Frame(panel, bg='#202124')
        size_row.pack(fill='x', pady=(0, 12))
        width = tk.StringVar(window, value=str(self._prerender_resolution[0]))
        height = tk.StringVar(window, value=str(self._prerender_resolution[1]))
        tk.Label(size_row, text='宽 × 高（像素上限，保持画幅比例）', bg='#202124', fg='#969ba3',
                 font=('Microsoft YaHei UI', 9)).pack(side='left')
        height_entry = ttk.Entry(size_row, textvariable=height, width=8)
        height_entry.pack(side='right')
        tk.Label(size_row, text=' × ', bg='#202124', fg='#969ba3').pack(side='right')
        width_entry = ttk.Entry(size_row, textvariable=width, width=8)
        width_entry.pack(side='right')
        self._project_settings_vars.update(prerender_preset=preset, prerender_width=width, prerender_height=height)
        def resolution_changed(_event=None):
            size = RESOLUTIONS[preset.get()]
            if size: width.set(str(size[0])); height.set(str(size[1]))
            for widget in (width_entry, height_entry):
                widget.configure(state='normal' if enabled.get() and size is None else 'readonly' if enabled.get() else 'disabled')
        combo.bind('<<ComboboxSelected>>', resolution_changed)
        for state, text in (('yellow', '黄色 · 预计可直接流畅播放'),
                            ('red', '红色 · 播放负载较高，等待自动渲染'),
                            ('green', '绿色 · 预渲染完成，播放使用缓存')):
            tk.Label(panel, text='━  '+text, bg='#202124', fg=COLORS[state],
                font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x', pady=2)
        tk.Label(panel, text='暂停且软件空闲时自动渲染红色区间；播放时按实测 FPS 校正。\n'
            '支持预设或自定义分辨率，保留工程帧率；原音轨继续同步播放。\n'
            '修改画面后缓存重新检查。此开关与保存位置在本机记住。',
            bg='#202124', fg='#969ba3', justify='left', anchor='w', wraplength=self._px(500),
            font=('Microsoft YaHei UI', 9)).pack(fill='x', pady=(10, 8))
        tk.Label(panel, textvariable=self.prerender_status_var, bg='#202124', fg='#969ba3',
                 wraplength=self._px(500), justify='left', anchor='w', font=('Microsoft YaHei UI', 9)).pack(fill='x')
        def toggle(*_):
            for widget in (entry, button): widget.configure(state='normal' if enabled.get() else 'disabled')
            combo.configure(state='readonly' if enabled.get() else 'disabled')
            resolution_changed()
        enabled.trace_add('write', toggle); toggle()

        def validate():
            value = folder.get().strip()
            if enabled.get(): value = str(check_folder(value))
            try: resolution = validate_resolution(int(width.get()), int(height.get()))
            except ValueError: raise ValueError('预渲染宽高须为 16–7680 之间的偶数。')
            return bool(enabled.get()), value, resolution
        return validate

    def _apply_prerender_settings(self, enabled, folder, resolution=(1280, 720)):
        path = self._prerender_preferences
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.pending')
            try:
                temporary.write_text(json.dumps(dict(enabled=enabled, folder=folder,
                    width=resolution[0], height=resolution[1]), ensure_ascii=False), encoding='utf-8')
                os.replace(temporary, path)
            finally: temporary.unlink(missing_ok=True)
        changed = (enabled != self.prerender_enabled_var.get() or folder != self.prerender_folder_var.get()
                   or resolution != self._prerender_resolution)
        self._prerender_resolution = resolution
        self.prerender_enabled_var.set(enabled)
        self.prerender_folder_var.set(folder)
        if changed: self._invalidate_prerender()

    def _invalidate_prerender(self):
        if not hasattr(self, '_prerender_generation'): return
        self._prerender_generation += 1
        if self._prerender_worker: self._prerender_worker.invalidate()
        self._prerender_running = False
        self._prerender_signature = None
        self._prerender_slow.clear()
        self._prerender_lag = None
        enabled = self.prerender_enabled_var.get()
        self._prerender_blocks = plan_blocks(self.project) if enabled else []
        self._prerender_pending = bool(enabled and self.project.clips)
        self._prerender_eligible_at = time.monotonic()+.7
        self.prerender_status_var.set('等待空闲时分析时间线…' if self._prerender_pending else
                                      '等待添加视频' if enabled else '预渲染已关闭')
        self._publish_prerender()
        self._draw_prerender_line()

    def _check_prerender_project(self):
        if not self.prerender_enabled_var.get(): return
        signature = picture_signature(self.project, self._prerender_resolution)
        if signature != self._prerender_signature:
            self._invalidate_prerender()
            self._prerender_signature = signature

    def _publish_prerender(self):
        if self.player:
            self.player.submit('prerender_cache', signature=self._prerender_signature, resolution=self._prerender_resolution,
                items=[(block.path, block.start, block.end) for block in self._prerender_blocks if block.state == 'green'])

    def _draw_prerender_line(self):
        canvas = vars(self).get('timeline')
        if canvas is None: return
        canvas.delete('prerender-line')
        if not self.prerender_enabled_var.get(): return
        y = canvas.canvasy(0)+getattr(self, '_timeline_ruler_h', self._px(27))-self._px(3)
        left = max(self._timeline_x(0), canvas.canvasx(0))
        right = canvas.canvasx(canvas.winfo_width())
        for block in self._prerender_blocks:
            first, last = max(left, self._timeline_x(block.start)), min(right, self._timeline_x(block.end))
            if last <= first: continue
            canvas.create_line(first, y, last, y, fill=COLORS[block.state],
                width=max(2, self._px(3)), tags=('prerender-line', 'timeline-ruler', 'prerender-'+block.state))
        canvas.tag_raise('head')

    def _observe_prerender_fps(self, rate, position=None):
        if not self.prerender_enabled_var.get() or not self.playing: self._prerender_lag = None; return
        if not isinstance(rate, (int, float)) or not math.isfinite(rate): return
        position = self.current if position is None else position
        block = next((block for block in self._prerender_blocks if block.start <= position < block.end), None)
        if block is None or block.state != 'yellow': self._prerender_lag = None; return
        # MPV honours native source rates; a 24fps source on a 30fps timeline
        # must not be treated as dropped frames. Stack output is capped at 30.
        target = min(self.project.fps, 30.) if self.project.video_stack_enabled else self.project.fps
        if not self.project.video_stack_enabled:
            clip = self.project.clip_at(self.project.camera_at(position), position)
            if clip is not None:
                native = self._prerender_source_rates.get(clip.path, 0.)
                if native > 0: target = min(target, native)
        if rate >= target*.85: self._prerender_lag = None; return
        now = time.monotonic()
        previous = self._prerender_lag
        if previous is None or previous[0] != block.first or abs(position-previous[2]) > 1.5:
            self._prerender_lag = (block.first, now, position); return
        self._prerender_lag = (block.first, previous[1], position)
        if now-previous[1] < 1.: return
        self._prerender_slow.add(block.first)
        self._prerender_blocks = [replace(item, state='red', reason='实测播放帧率不足') if item.first == block.first else item
                                  for item in self._prerender_blocks]
        self._prerender_pending = True
        self.prerender_status_var.set('检测到播放帧率不足，暂停后将自动预渲染红色区间。')
        self._draw_prerender_line()

    def _poll_prerender(self):
        if not self.alive: return
        try:
            while True:
                generation, kind, payload = self._prerender_events.get_nowait()
                if generation != self._prerender_generation: continue
                if kind == 'plan':
                    self._prerender_signature = payload['signature']
                    self._prerender_source_rates = payload.get('source_rates', {})
                    # Preserve completed entries when resuming a suspended task.
                    ready = {block.first: block for block in self._prerender_blocks if block.state == 'green'}
                    self._prerender_blocks = [ready.get(block.first, block) for block in payload['blocks']]
                    self._draw_prerender_line()
                elif kind == 'ready':
                    block = payload['block']
                    self._prerender_blocks = [block if item.first == block.first else item for item in self._prerender_blocks]
                    self._publish_prerender(); self._draw_prerender_line()
                elif kind == 'progress': self.prerender_status_var.set(f'正在预渲染 {payload["first"]/self.project.fps:.1f}s 区间 · {payload["percent"]:.0f}%')
                elif kind == 'failed':
                    self._prerender_blocks = [replace(block, state='red', path='', reason=payload['error'])
                        if block.first == payload['first'] else block for block in self._prerender_blocks]
                    self._publish_prerender(); self._draw_prerender_line()
                    self.prerender_status_var.set('预渲染失败，区间保留红色：'+payload['error'])
                elif kind == 'error':
                    self._prerender_running = False
                    self.prerender_status_var.set('预渲染无法完成：'+payload['error'])
                elif kind == 'done':
                    self._prerender_running = False
                    red = sum(block.state == 'red' for block in self._prerender_blocks)
                    self.prerender_status_var.set(f'{red} 个区间尚未完成渲染；请检查目录或素材。' if red else '预渲染检查完成 · 绿色区间已使用缓存')
        except queue.Empty: pass
        enabled = self.prerender_enabled_var.get()
        blocked = self.playing or self.busy or self.app.working or getattr(self, '_quick_proxy_running', False)
        if enabled and self._prerender_running and blocked:
            self._prerender_worker.invalidate()
            self._prerender_generation += 1
            self._prerender_running = False; self._prerender_pending = True
            self.prerender_status_var.set('播放或其他任务进行中，预渲染将在空闲后继续。')
        now = time.monotonic()
        if enabled and not blocked and now-self._prerender_media_check > 3.:
            self._prerender_media_check = now
            self._check_prerender_project()
            if any(block.path and not Path(block.path).is_file() for block in self._prerender_blocks):
                self._invalidate_prerender()
        if enabled and self._prerender_pending and not blocked and now >= self._prerender_eligible_at:
            caps = self.app.caps
            if caps.ffmpeg_path and caps.ffprobe_path and self.prerender_folder_var.get().strip():
                if self._prerender_worker is None: self._prerender_worker = PrerenderWorker(self._prerender_events)
                self._prerender_generation += 1
                self._prerender_worker.request(self._prerender_generation, self.project,
                    self.prerender_folder_var.get(), caps, self._prerender_slow, self._prerender_resolution)
                self._prerender_pending = False; self._prerender_running = True
        self._prerender_poll_after = self.after(200, self._poll_prerender)

    def _close_prerender(self):
        if self._prerender_poll_after is not None:
            self.after_cancel(self._prerender_poll_after)
            self._prerender_poll_after = None
        if self._prerender_worker: self._prerender_worker.close()
