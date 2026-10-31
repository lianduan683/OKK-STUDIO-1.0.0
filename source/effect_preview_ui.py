"""Independent, looping effect previews that leave programme playback alone."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from cache_paths import cache_dir
from crop_frame import pixels_ppm, resize_pixels
from crop_video import CropVideoStream
from effect_preview import render_effect_preview
from effects_library import default_app_dir
from exporter_core import ExportCancelled, find_binary

BG, PANEL, INK, MUTED = '#18191b', '#292b2e', '#dedfe3', '#969ba3'


class EffectPreviewWindow(tk.Toplevel):
    def __init__(self, browser):
        super().__init__(browser.ws)
        self._transport_scope_excluded = True
        self.browser = browser
        self.ws = browser.ws
        self._closed = False
        self._cancel = threading.Event()
        self._results = queue.Queue()
        self._serial = 0
        self._stream = None
        self._result = None
        self._pixels = self._photo = None
        self._position = 0.
        self._paused = False
        self._poll_after = None
        self.withdraw()
        self.configure(bg=BG)
        self.transient(self.ws.winfo_toplevel())
        self.title('效果预览')
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.status = tk.StringVar(self, value='正在生成预览…')
        self.heading = tk.StringVar(self)
        header = tk.Frame(self, bg=PANEL)
        header.pack(fill='x')
        tk.Label(header, textvariable=self.heading, bg=PANEL, fg=INK,
                 font=('Microsoft YaHei UI', 12, 'bold'), anchor='w').pack(fill='x', padx=14, pady=(12, 4))
        tk.Label(header, text='示例画面 · 循环播放 · 应用前预览', bg=PANEL, fg=MUTED,
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x', padx=14, pady=(0, 10))
        footer = tk.Frame(self, bg=PANEL)
        footer.pack(side='bottom', fill='x')
        self.pause_button = ttk.Button(footer, text='暂停', command=self.toggle, state='disabled')
        self.pause_button.pack(side='left', padx=(12, 4), pady=10)
        self.replay_button = ttk.Button(footer, text='重新播放', command=self.replay, state='disabled')
        self.replay_button.pack(side='left', padx=4, pady=10)
        ttk.Button(footer, text='重新预览', command=browser.preview_effect).pack(side='left', padx=4, pady=10)
        ttk.Button(footer, text='关闭', command=self.close).pack(side='right', padx=12, pady=10)
        self.status_label = tk.Label(self, textvariable=self.status, bg=BG, fg=MUTED,
                                    font=('Microsoft YaHei UI', 9), anchor='w', justify='left')
        self.status_label.pack(side='bottom', fill='x', padx=14, pady=8)
        self.canvas = tk.Canvas(self, bg='#101113', highlightthickness=0, width=640, height=360)
        self.canvas.pack(fill='both', expand=True, padx=10, pady=10)
        self.canvas.bind('<Configure>', lambda _event: self._draw())
        self.bind('<Configure>', lambda event: self.status_label.configure(wraplength=max(100, event.width-28))
                  if event.widget is self else None, add='+')
        self.bind('<Escape>', lambda _event: self.close())
        self.bind('<space>', lambda _event: self.toggle())
        root = self.ws.winfo_toplevel()
        width, height = self.ws._px(700), self.ws._px(500)
        self.geometry(f'{width}x{height}+{max(0, root.winfo_rootx() + (root.winfo_width()-width)//2)}'
                      f'+{max(0, root.winfo_rooty() + (root.winfo_height()-height)//2)}')
        self.minsize(self.ws._px(420), self.ws._px(330))
        self.deiconify()
        self._poll_after = self.after(40, self._poll)

    def load(self, item, strength, duration):
        self._cancel.set()
        self._cancel = threading.Event()
        self._serial += 1
        serial, cancel = self._serial, self._cancel
        self._stop_stream()
        self._result = None
        self._pixels = self._photo = None
        self._paused = False
        self._position = 0.
        self.pause_button.configure(state='disabled', text='暂停')
        self.replay_button.configure(state='disabled')
        self.heading.set(item.name)
        self.title(item.name + ' · 效果预览')
        self.status.set('正在生成预览…')
        self.canvas.delete('all')
        app = self.ws.app
        ffmpeg = getattr(app.caps, 'ffmpeg_path', '') or find_binary('ffmpeg', default_app_dir())
        ffprobe = getattr(app.caps, 'ffprobe_path', '') or find_binary('ffprobe', default_app_dir())

        def worker():
            try:
                if not ffmpeg or not ffprobe:
                    raise ValueError('未找到视频预览组件，请检查程序文件。')
                result = render_effect_preview(item, strength, duration, ffmpeg, ffprobe,
                                               cache_dir('previews/library-effects'), cancel_event=cancel)
                if not cancel.is_set():
                    self._results.put((serial, result, ''))
            except ExportCancelled:
                pass
            except Exception as error:
                if not cancel.is_set():
                    self._results.put((serial, None, str(error)))
        self._ffmpeg = ffmpeg
        threading.Thread(target=worker, name='okk-effect-preview', daemon=True).start()

    def _stop_stream(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def _start_stream(self):
        self._stop_stream()
        if self._result is None or self._paused or self._closed:
            return
        remaining = self._result.duration - self._position
        if remaining <= .05:
            self._position, remaining = 0., self._result.duration
        self._stream = CropVideoStream(self._ffmpeg, self._result.path, self._position, remaining, fps=15)

    def _poll(self):
        self._poll_after = None
        if self._closed:
            return
        try:
            for _ in range(32):
                serial, result, error = self._results.get_nowait()
                if serial != self._serial:
                    continue
                if error:
                    self.status.set(error)
                else:
                    self._result = result
                    self.status.set('示例预览，尚未应用到工程。可返回特效库调整参数后重新预览。')
                    self.pause_button.configure(state='normal')
                    self.replay_button.configure(state='normal')
                    self._start_stream()
        except queue.Empty:
            pass
        stream = self._stream
        if stream is not None:
            frame = stream.read()
            if frame is not None:
                self._position, self._pixels = frame
                self._draw()
            elif stream.done.is_set():
                error = stream.error
                self._stop_stream()
                if error:
                    self.status.set(error)
                elif not self._paused:
                    self._position = 0.
                    self._start_stream()
        self._poll_after = self.after(40, self._poll)

    def _draw(self):
        if self._pixels is None or self._closed:
            return
        height, width = self._pixels.shape[:2]
        scale = min(self.canvas.winfo_width()/width, self.canvas.winfo_height()/height)
        displayed = resize_pixels(self._pixels, max(1, round(width*scale)), max(1, round(height*scale)))
        self._photo = tk.PhotoImage(master=self.canvas, data=pixels_ppm(displayed), format='PPM')
        self.canvas.delete('all')
        self.canvas.create_image(self.canvas.winfo_width()/2, self.canvas.winfo_height()/2,
                                 image=self._photo, anchor='center')
        self.status_label.configure(wraplength=max(100, self.winfo_width()-28))

    def toggle(self):
        if self._result is None:
            return 'break'
        self._paused = not self._paused
        self.pause_button.configure(text='播放' if self._paused else '暂停')
        if self._paused:
            self._stop_stream()
        else:
            self._start_stream()
        return 'break'

    def replay(self):
        self._position = 0.
        self._paused = False
        self.pause_button.configure(text='暂停')
        self._start_stream()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        if self._poll_after is not None:
            self.after_cancel(self._poll_after)
            self._poll_after = None
        self._stop_stream()
        if vars(self.browser).get('_effect_preview_window') is self:
            self.browser._effect_preview_window = None
        super().destroy()

    def destroy(self):
        self.close()
