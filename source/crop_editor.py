"""Visual, non-destructive crop monitoring for one selected programme range."""
from __future__ import annotations

from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from crop_geometry import (ASPECT, automatic_crop, crop_at, draw_crop,
                           move_crop, rect_from_transform, resize_crop,
                           transform_from_rect, zoom_crop)
from crop_frame import (CropCancelled, content_bounds, decode_frame,
                        pixels_ppm, resize_pixels, choose_preview_source)
from crop_video import CropVideoStream
from editor_core import format_time

BG, PANEL, INK, MUTED, ACCENT = '#18191b', '#292b2e', '#dedfe3', '#969ba3', '#58a6d6'
FONT = 'Microsoft YaHei UI'


class CropEditor(tk.Toplevel):
    """Draft rectangle over the full source; commit exactly one undo operation."""

    def __init__(self, workspace, *, automatic=False):
        super().__init__(workspace.winfo_toplevel())
        self.ws = workspace
        self.project = workspace.project
        self.selection = tuple(workspace.selected_program_range)
        self._closed = False
        self._generation = 0
        self._cancel = threading.Event()
        self._results = queue.Queue()
        self._workers = []
        self._pending = None
        self._detecting = False
        self._job_started = 0.
        self._phase = ''
        self._preview_note = ''
        self._displayed_caption = ''
        self._preview_context = None
        self._video_stream = None
        self._video_started = 0.
        self._video_received = False
        self._resize_after = None
        self._draw_after = None
        self._poll_after = None
        self._drag = None
        self._pixels = None
        self._photo = None
        self._preview_photo = None
        self.rect = None
        self._image_box = (0., 0., 1., 1.)
        self._display_scale = 1.
        self._handles = {}
        self._updating = False
        self._initial_transform = workspace.get_selected_transform()
        self._draft_transform = self._initial_transform
        self.status = tk.StringVar(self, value='正在读取完整原画面…')
        self.info = tk.StringVar(self)
        self.frame_label = tk.StringVar(self)
        self.zoom = tk.DoubleVar(self, value=100.)
        self.frame_time = tk.DoubleVar(self)
        self.withdraw()
        self.configure(bg=BG)
        self.title('裁切监看 · 16:9 · 所选片段')
        self.transient(workspace.winfo_toplevel())
        self.protocol('WM_DELETE_WINDOW', self.close)
        self._build()
        self.bind('<Escape>', lambda event: self._key_close())
        self.bind('<Return>', lambda event: self._key_apply())
        self.bind('<space>', lambda event: self._key_play())
        for key, delta in (('Left', (-1, 0)), ('Right', (1, 0)), ('Up', (0, -1)), ('Down', (0, 1))):
            self.bind('<'+key+'>', lambda event, d=delta: self._nudge(event, d))
        self.bind('<Delete>', lambda event: 'break')
        self.update_idletasks()
        root = workspace.winfo_toplevel()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        width, height = min(self.px(1180), sw-self.px(50)), min(self.px(790), sh-self.px(100))
        self.geometry(f'{width}x{height}+{max(0, root.winfo_rootx()+(root.winfo_width()-width)//2)}'
                      f'+{max(0, root.winfo_rooty()+(root.winfo_height()-height)//2)}')
        self.minsize(min(width, self.px(760)), min(height, self.px(500)))
        self.deiconify()
        self.grab_set()
        self.focus_set()
        position = min(max(workspace.current, self.selection[0]), self.selection[1]-1/self.project.fps)
        camera = self.project.camera_at(position)
        clip = self.project.clip_at(camera, position)
        if clip is None:
            self.status.set('所选位置没有可裁切画面，请选择有画面的片段。')
            self._set_loading(False)
        else:
            self.first = max(self.selection[0], clip.start)
            self.last = min(self.selection[1], clip.end)-1/self.project.fps
            self.last = max(self.first, self.last)
            self.frame_slider.configure(from_=self.first, to=self.last,
                                        resolution=1/self.project.fps)
            self.frame_time.set(min(self.last, max(self.first, position)))
            self._request_frame(automatic=automatic)
        self._poll_after = self.after(50, self._poll)

    def px(self, value):
        return self.ws._px(value)

    def _label(self, parent, text='', *, variable=None, color=INK, size=9, bold=False):
        return tk.Label(parent, text=text, textvariable=variable, bg=parent.cget('bg'), fg=color,
                        anchor='w', justify='left', font=(FONT, size, 'bold' if bold else 'normal'))

    def _build(self):
        header = tk.Frame(self, bg=PANEL, padx=self.px(16), pady=self.px(12))
        header.pack(fill='x')
        self._label(header, '裁切监看', size=14, bold=True).pack(side='left')
        self._label(header, '16:9 锁定 · 框内保留，暗区裁掉', color=ACCENT).pack(side='left', padx=self.px(16))
        footer = tk.Frame(self, bg=PANEL, padx=self.px(14), pady=self.px(10))
        footer.pack(side='bottom', fill='x')
        status = self._label(footer, variable=self.status, color=MUTED)
        status.pack(fill='x', pady=(0, self.px(8)))
        footer.bind('<Configure>', lambda event: status.configure(wraplength=max(200, event.width-self.px(28))))
        ttk.Button(footer, text='取消', command=self.close, style='Compact.TButton').pack(side='right')
        self.apply_button = ttk.Button(footer, text='应用裁切', command=self.apply, style='Compact.TButton', state='disabled')
        self.apply_button.pack(side='right', padx=self.px(10))
        ttk.Button(footer, text='重新读取画面', command=self._request_frame,
                   style='Compact.TButton').pack(side='right', padx=self.px(4))
        self._label(footer, '应用后保存到所选片段，支持撤销。', color=MUTED).pack(side='left')
        body = tk.Frame(self, bg=BG)
        body.pack(fill='both', expand=True)
        side = tk.Frame(body, bg=PANEL, width=self.px(270), padx=self.px(12), pady=self.px(12))
        side.pack(side='right', fill='y')
        side.pack_propagate(False)
        self._label(side, '保留区域预览', bold=True).pack(fill='x')
        self.preview = tk.Canvas(side, width=self.px(246), height=self.px(139), bg='black', highlightthickness=0)
        self.preview.pack(fill='x', pady=self.px(10))
        self.preview.bind('<Configure>', lambda event: self._schedule_draw())
        self._label(side, variable=self.info, color=ACCENT).pack(fill='x', pady=(0, self.px(10)))
        self.auto_button = ttk.Button(side, text='自动裁切 16:9', command=self.automatic, style='Compact.TButton', state='disabled')
        self.auto_button.pack(fill='x', pady=self.px(4))
        self.max_button = ttk.Button(side, text='最大画幅', command=self.maximum, style='Compact.TButton', state='disabled')
        self.max_button.pack(fill='x', pady=self.px(4))
        self.center_button = ttk.Button(side, text='裁切框居中', command=self.center, style='Compact.TButton', state='disabled')
        self.center_button.pack(fill='x', pady=self.px(4))
        self._label(side, '缩放（100%–400%）', color=MUTED).pack(fill='x', pady=(self.px(16), 0))
        self.zoom_slider = tk.Scale(side, from_=100, to=400, orient='horizontal', variable=self.zoom,
            command=self._zoom_changed, resolution=1, bg=PANEL, fg=INK, troughcolor=BG,
            highlightthickness=0, activebackground=ACCENT, state='disabled')
        self.zoom_slider.pack(fill='x')
        help_label = self._label(side,
            '拖动框内：移动取景\n拖动边角：缩放裁切框\n框外按住拖动：重新框选\n方向键：微调位置\nShift + 方向键：快速移动\n\n自动裁切仅识别当前这一帧。\n可播放视频检查保留的画面。', color=MUTED)
        help_label.pack(fill='x', pady=self.px(18))
        main = tk.Frame(body, bg=BG)
        main.pack(side='left', fill='both', expand=True)
        self.canvas = tk.Canvas(main, bg='#101113', highlightthickness=0, takefocus=True)
        self.canvas.pack(fill='both', expand=True, padx=self.px(12), pady=self.px(12))
        self.canvas.bind('<Configure>', self._canvas_resize)
        self.canvas.bind('<ButtonPress-1>', self._press)
        self.canvas.bind('<B1-Motion>', self._motion)
        self.canvas.bind('<ButtonRelease-1>', self._release)
        self.canvas.bind('<Motion>', self._hover)
        self.canvas.bind('<MouseWheel>', self._wheel)
        seek = tk.Frame(main, bg=BG, padx=self.px(12))
        seek.pack(fill='x', pady=(0, self.px(10)))
        self._label(seek, variable=self.frame_label, color=MUTED).pack(fill='x')
        controls = tk.Frame(seek, bg=BG)
        controls.pack(fill='x', pady=self.px(4))
        self.play_button = ttk.Button(controls, text='播放视频', command=self.toggle_video, style='Compact.TButton', state='disabled')
        self.play_button.pack(side='left')
        self.step_buttons = []
        for title, direction in (('上一帧', -1), ('下一帧', 1)):
            button = ttk.Button(controls, text=title, command=lambda d=direction: self.step_frame(d),
                                style='Compact.TButton', state='disabled')
            button.pack(side='left', padx=self.px(4))
            self.step_buttons.append(button)
        self._label(controls, '空格 播放 / 暂停', color=MUTED).pack(side='left', padx=self.px(10))
        self.frame_slider = tk.Scale(seek, from_=0, to=1, orient='horizontal', variable=self.frame_time,
            showvalue=False, bg=BG, fg=INK, troughcolor=PANEL, highlightthickness=0, activebackground=ACCENT)
        self.frame_slider.pack(fill='x')
        self.frame_slider.bind('<ButtonPress-1>', lambda event: self.pause_video())
        self.frame_slider.bind('<ButtonRelease-1>', lambda event: self._request_frame())
        self.frame_slider.bind('<KeyRelease>', lambda event: self._request_frame())

    def _valid_target(self):
        return (not self._closed and getattr(self.ws, 'alive', True)
                and self.ws.project is self.project
                and tuple(self.ws.selected_program_range or ()) == self.selection)

    def _set_loading(self, loading):
        self._pending = bool(loading)
        active = self._pixels is not None and not loading and self._valid_target()
        for widget in (self.apply_button, self.auto_button, self.max_button, self.center_button,
                       self.zoom_slider, self.play_button, *self.step_buttons):
            widget.configure(state='normal' if active else 'disabled')

    def _message(self, generation, kind, **data):
        self._results.put((generation, kind, data))

    def _request_frame(self, *, automatic=False):
        if self._closed or not hasattr(self, 'first'): return
        self.pause_video()
        if not self._valid_target():
            self.status.set('所选片段已变化，请关闭后重新打开裁切。'); self._set_loading(False); return
        ffmpeg = getattr(self.ws.app.caps, 'ffmpeg_path', '')
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.status.set('未找到 FFmpeg，暂时无法读取裁切画面。'); self._set_loading(False); return
        if self.rect is not None and self._pixels is not None:
            self._draft_transform = transform_from_rect(self.rect, self.source_size)
        position = min(self.last, max(self.first, self.frame_time.get()))
        camera = self.project.camera_at(position)
        try:
            media = self.project.media_at(camera, position, use_proxies=False)
        except (ValueError, OSError) as error:
            self.status.set(str(error)); self._set_loading(False); return
        if media is None:
            self.status.set('此处没有可用画面，请选择其他帧。'); self._set_loading(False); return
        self._cancel.set()
        self._cancel = cancel = threading.Event()
        self._generation += 1
        generation = self._generation
        self._job_started = time.monotonic()
        self._detecting = automatic
        self._set_loading(True)
        self._phase = '正在读取裁切画面'
        self.status.set(self._phase+'…')
        path, source_time, _ = media
        caption = f'{Path(path).name} · {format_time(position)}'
        self.frame_label.set(caption+' · 正在读取画面…')
        proxy = ''
        clip = self.project.clip_at(camera, position)
        if clip is not None and str(path) == str(clip.path):
            candidate = self.project.media_at(camera, position, use_proxies=True)
            if candidate is not None and str(candidate[0]) != str(path): proxy = str(candidate[0])
        ffprobe = getattr(self.ws.app.caps, 'ffprobe_path', '')
        def work():
            try:
                self._message(generation, 'phase', text='正在校验代理画幅与时长' if proxy else '正在读取原片画面')
                preview_path, note = choose_preview_source(ffprobe, path, proxy, cancel)
                self._message(generation, 'phase', text='正在读取代理画面' if preview_path != str(path) else '正在读取原片画面')
                try:
                    pixels = decode_frame(ffmpeg, preview_path, float(source_time), cancel)
                except (ValueError, OSError):
                    if preview_path == str(path): raise
                    preview_path, note = str(path), '代理取帧失败，使用原片预览'
                    self._message(generation, 'phase', text='代理取帧失败，正在读取原片画面')
                    pixels = decode_frame(ffmpeg, preview_path, float(source_time), cancel)
                self._message(generation, 'preview', pixels=pixels, automatic=automatic,
                              caption=caption, note=note, path=preview_path,
                              source_time=float(source_time), timeline_time=position)
            except CropCancelled:
                pass
            except Exception as error:
                self._message(generation, 'error', text=str(error))
        worker = threading.Thread(target=work, name='okk-crop-frame', daemon=True)
        self._workers = [thread for thread in self._workers if thread.is_alive()]
        self._workers.append(worker)
        worker.start()

    @property
    def source_size(self):
        return self._pixels.shape[1], self._pixels.shape[0]

    def _poll(self):
        if self._closed: return
        try:
            while True:
                generation, kind, data = self._results.get_nowait()
                if generation != self._generation: continue
                if not self._valid_target():
                    self._cancel.set(); self._detecting = False
                    self.status.set('所选片段已变化，未应用裁切。'); self._set_loading(False); continue
                if kind in ('phase', 'progress'):
                    self._phase = data['text']
                    self.status.set(self._phase)
                elif kind == 'error':
                    self._detecting = False
                    self.status.set(data['text']+'；可重新读取或拖动时间滑块换一帧。')
                    if self._displayed_caption: self.frame_label.set(self._displayed_caption)
                    self._set_loading(False)
                    logger = getattr(self.ws.app, '_log', None)
                    if callable(logger): logger('裁切预览：'+data['text'])
                elif kind == 'preview':
                    self._detecting = False
                    self._pixels = data['pixels']
                    self.rect = (automatic_crop(self.source_size, content_bounds([self._pixels])) if data['automatic'] else
                                 rect_from_transform(self._draft_transform, self.source_size))
                    self._preview_context = data
                    self._preview_note = data['note']
                    self._displayed_caption = data['caption']+' · '+self._preview_note
                    self.frame_label.set(self._displayed_caption)
                    self._set_loading(False)
                    self._render_source()
                    self.status.set('已仅按当前帧识别黑边并生成 16:9 框，可手动调整或播放视频检查。' if data['automatic'] else
                        '视频已暂停在当前帧，可直接拖框裁切；点击“播放视频”检查其他画面。')
        except queue.Empty:
            pass
        except Exception as error:
            self._cancel.set(); self._detecting = False; self._pixels = None; self.rect = None
            self._generation += 1
            self._set_loading(False)
            self.status.set('裁切画面显示失败：'+str(error)+'；请点击“重新读取画面”。')
            logger = getattr(self.ws.app, '_log', None)
            if callable(logger): logger('裁切画面显示失败：'+str(error))
        if self._pending:
            elapsed = time.monotonic()-self._job_started
            if elapsed > 35:
                self._cancel.set(); self._generation += 1; self._detecting = False
                self._set_loading(False)
                self.status.set('读取画面超时，已停止。请重新读取或拖动时间滑块换一帧。')
            elif elapsed >= 1:
                self.status.set(f'{self._phase}… {elapsed:.0f} 秒；可取消或重新读取。')
        self._poll_video()
        self._poll_after = self.after(50, self._poll)

    def toggle_video(self):
        if self._video_stream is not None:
            self._poll_video()
            self.pause_video(report=True)
            return
        if self._pending or self._pixels is None or self._preview_context is None or not self._valid_target(): return
        context = self._preview_context
        position = self.frame_time.get()
        origin = context['timeline_time']-context['source_time']
        if position >= self.last-.5/self.project.fps:
            position = max(self.first, origin)
        source_time = max(0., position-origin)
        duration = self.last-position+1/self.project.fps
        self._video_stream = CropVideoStream(self.ws.app.caps.ffmpeg_path, context['path'], source_time, duration)
        self._video_started = time.monotonic()
        self._video_received = False
        self.play_button.configure(text='暂停视频')
        self.status.set('正在播放视频；可直接调整裁切框，空格暂停在当前帧。')

    def _poll_video(self):
        stream = self._video_stream
        if stream is None: return
        if not self._valid_target():
            self.pause_video()
            self.status.set('所选片段已变化，视频预览已停止。')
            return
        try:
            picture = stream.read()
            if picture is not None:
                seconds, pixels = picture
                saved = transform_from_rect(self.rect, self.source_size)
                self._pixels = pixels
                self.rect = rect_from_transform(saved, self.source_size)
                context = self._preview_context
                position = min(self.last, max(self.first, seconds+context['timeline_time']-context['source_time']))
                self.frame_time.set(position)
                self._displayed_caption = f"{Path(context['path']).name} · {format_time(position)} · {self._preview_note}"
                self.frame_label.set(self._displayed_caption)
                self._video_received = True
                self._render_source()
            if stream.done.is_set():
                message = stream.error or '视频播放到末尾，已停在当前帧。可拖动时间滑块或继续调整裁切框。'
                self.pause_video()
                self.status.set(message)
            elif not self._video_received and time.monotonic()-self._video_started > 15:
                self.pause_video()
                self.status.set('视频预览读取超时，已保留当前帧，可重新读取或继续裁切。')
        except Exception as error:
            self.pause_video()
            self.status.set('视频预览失败，已保留当前帧：'+str(error))

    def pause_video(self, *, report=False):
        stream, self._video_stream = self._video_stream, None
        if stream is not None: stream.close()
        self.play_button.configure(text='播放视频')
        if report and self._pixels is not None:
            self.status.set('视频已暂停，直接使用当前这一帧调整裁切。')

    def step_frame(self, direction):
        if self._pending or not hasattr(self, 'first'): return
        self.pause_video()
        self.frame_time.set(min(self.last, max(self.first, self.frame_time.get()+direction/self.project.fps)))
        self._request_frame()

    def _key_play(self):
        self.toggle_video()
        return 'break'

    def _canvas_resize(self, event=None):
        if self._resize_after is not None: self.after_cancel(self._resize_after)
        self._resize_after = self.after(70, self._render_source)

    def _render_source(self):
        self._resize_after = None
        if self._closed or self._pixels is None: return
        sw, sh = self.source_size
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        scale = min((width-self.px(22))/sw, (height-self.px(22))/sh)
        dw, dh = max(1, round(sw*scale)), max(1, round(sh*scale))
        self._display_scale = min(dw/sw, dh/sh)
        # Use the exact display box on each axis when translating handles.
        left, top = (width-dw)/2, (height-dh)/2
        self._image_box = left, top, left+dw, top+dh
        self._photo = tk.PhotoImage(master=self, data=pixels_ppm(resize_pixels(self._pixels, dw, dh)), format='PPM')
        self.canvas.delete('all')
        self.canvas.create_image(left, top, image=self._photo, anchor='nw', tags='source')
        self._draw()

    def _canvas_point(self, point):
        x, y = point
        l, t, r, b = self._image_box
        sw, sh = self.source_size
        return l+x*(r-l)/sw, t+y*(b-t)/sh

    def _source_point(self, x, y):
        l, t, r, b = self._image_box
        sw, sh = self.source_size
        return min(sw, max(0., (x-l)*sw/(r-l))), min(sh, max(0., (y-t)*sh/(b-t)))

    def _schedule_draw(self):
        if self._closed or self._pixels is None or self._draw_after is not None: return
        self._draw_after = self.after(16, self._draw)

    def _draw(self):
        self._draw_after = None
        if self._closed or self._pixels is None or self.rect is None: return
        self.canvas.delete('crop')
        il, it, ir, ib = self._image_box
        left, top = self._canvas_point(self.rect[:2])
        right, bottom = self._canvas_point(self.rect[2:])
        for box in ((il, it, ir, top), (il, bottom, ir, ib), (il, top, left, bottom), (right, top, ir, bottom)):
            self.canvas.create_rectangle(*box, fill='#000000', stipple='gray50', width=0, tags='crop')
        self.canvas.create_rectangle(left, top, right, bottom, outline=ACCENT, width=2, tags='crop')
        for fraction in (1/3, 2/3):
            self.canvas.create_line(left+(right-left)*fraction, top, left+(right-left)*fraction, bottom,
                                    fill='#d0d7de', dash=(3, 5), tags='crop')
            self.canvas.create_line(left, top+(bottom-top)*fraction, right, top+(bottom-top)*fraction,
                                    fill='#d0d7de', dash=(3, 5), tags='crop')
        self._handles = dict(nw=(left, top), n=((left+right)/2, top), ne=(right, top),
            e=(right, (top+bottom)/2), se=(right, bottom), s=((left+right)/2, bottom),
            sw=(left, bottom), w=(left, (top+bottom)/2))
        radius = self.px(4)
        for x, y in self._handles.values():
            self.canvas.create_rectangle(x-radius, y-radius, x+radius, y+radius,
                fill='white', outline=ACCENT, tags='crop')
        self.canvas.create_text(left+self.px(7), top+self.px(8), text='保留 16:9', fill='white',
            anchor='nw', font=(FONT, 9, 'bold'), tags='crop')
        transform = transform_from_rect(self.rect, self.source_size)
        self._updating = True
        try: self.zoom.set(transform.zoom*100)
        finally: self._updating = False
        sw, sh = self.source_size
        self.info.set(f'缩放 {transform.zoom*100:.1f}%\n'
            f'保留原画面宽 {(self.rect[2]-self.rect[0])/sw:.1%} · 高 {(self.rect[3]-self.rect[1])/sh:.1%}')
        l, t, r, b = self.rect
        region = self._pixels[max(0, round(t)):max(round(t)+1, round(b)),
                              max(0, round(l)):max(round(l)+1, round(r))]
        width = max(1, self.preview.winfo_width())
        height = max(1, round(width/ASPECT))
        self.preview.configure(height=height)
        self._preview_photo = tk.PhotoImage(master=self, data=pixels_ppm(resize_pixels(region, width, height)), format='PPM')
        self.preview.delete('all')
        self.preview.create_image(0, 0, image=self._preview_photo, anchor='nw')

    def _hit(self, x, y):
        radius = self.px(10)
        for name, (hx, hy) in self._handles.items():
            if abs(x-hx) <= radius and abs(y-hy) <= radius: return name
        left, top = self._canvas_point(self.rect[:2])
        right, bottom = self._canvas_point(self.rect[2:])
        return 'move' if left <= x <= right and top <= y <= bottom else 'draw'

    def _press(self, event):
        if self._pixels is None or self._pending: return 'break'
        self.canvas.focus_set()
        self._drag = self._hit(event.x, event.y), self._source_point(event.x, event.y), self.rect
        return 'break'

    def _motion(self, event):
        if self._drag is None or self._pending: return 'break'
        kind, origin, original = self._drag
        point = self._source_point(event.x, event.y)
        if kind == 'move': self.rect = move_crop(original, (point[0]-origin[0], point[1]-origin[1]), self.source_size)
        elif kind == 'draw': self.rect = draw_crop(origin, point, self.source_size)
        else: self.rect = resize_crop(original, kind, point, self.source_size)
        self._schedule_draw()
        return 'break'

    def _release(self, event):
        if self._drag is not None: self._motion(event)
        self._drag = None
        return 'break'

    def _hover(self, event):
        if self._pixels is None or self._pending: return
        kind = self._hit(event.x, event.y)
        self.canvas.configure(cursor='fleur' if kind == 'move' else 'crosshair')

    def _wheel(self, event):
        if self.rect is not None and not self._pending:
            value = min(400., max(100., self.zoom.get()+(5 if event.delta > 0 else -5)))
            self.rect = zoom_crop(self.rect, value/100, self.source_size)
            self._schedule_draw()
        return 'break'

    def _zoom_changed(self, value):
        if self._updating or self.rect is None or self._pending: return
        rectangle = zoom_crop(self.rect, float(value)/100, self.source_size)
        if max(abs(a-b) for a, b in zip(rectangle, self.rect)) < 1e-6: return
        self.rect = rectangle
        self._schedule_draw()

    def _nudge(self, event, delta):
        if event.widget in (self.frame_slider, self.zoom_slider): return 'break'
        if self.rect is not None and not self._pending:
            step = 10 if event.state & 1 else 1
            self.rect = move_crop(self.rect, (delta[0]*step, delta[1]*step), self.source_size)
            self._schedule_draw()
        return 'break'

    def maximum(self):
        if self._pixels is None or self._pending: return
        self.rect = automatic_crop(self.source_size)
        self._schedule_draw()

    def center(self):
        if self.rect is None or self._pending: return
        sw, sh = self.source_size
        self.rect = crop_at(self.source_size, self.rect[2]-self.rect[0], (sw/2, sh/2))
        self._schedule_draw()

    def automatic(self):
        if self._pixels is None:
            self._request_frame(automatic=True)
            return
        self.pause_video()
        self.rect = automatic_crop(self.source_size, content_bounds([self._pixels]))
        self._draw()
        self.status.set('已仅按当前这一帧识别黑边并裁成 16:9，可继续手动调整或播放视频检查。')

    def apply(self):
        if not self._valid_target():
            self.status.set('所选片段已变化，未应用裁切。'); return False
        if self.rect is None or self._pending: return False
        self.pause_video()
        transform = transform_from_rect(self.rect, self.source_size)
        if self.ws.apply_selected_transform(transform, aspect_16_9=True):
            self.close()
            return True
        return False

    def _key_apply(self):
        self.apply()
        return 'break'

    def _key_close(self):
        self.close()
        return 'break'

    def close(self):
        if self._closed: return
        self._closed = True
        self.pause_video()
        self._cancel.set()
        deadline = time.monotonic()+.5
        for worker in self._workers:
            worker.join(timeout=max(0., deadline-time.monotonic()))
        for name in ('_resize_after', '_draw_after', '_poll_after'):
            pending = getattr(self, name)
            if pending is not None:
                self.after_cancel(pending)
                setattr(self, name, None)
        if vars(self.ws).get('_crop_editor') is self:
            self.ws._crop_editor = None
        self.destroy()


def open_crop_editor(workspace, *, automatic=False):
    if not workspace._available() or workspace.selected_program_range is None:
        workspace.status.set('请先选中节目轨中的片段。')
        return None
    previous = vars(workspace).get('_crop_editor')
    if previous is not None and previous.winfo_exists():
        previous.lift()
        if automatic: previous.automatic()
        return previous
    workspace.pause()
    editor = workspace._crop_editor = CropEditor(workspace, automatic=automatic)
    return editor
