"""Camera-linked audio rows, mute controls, and asynchronous real waveforms."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import threading
import tkinter as tk

from audio_waveform import Waveform, decode_waveform, envelope_points, waveform_key
from exporter_core import find_binary
from timeline_audio import audio_track_enabled


class TimelineAudioUIMixin:
    def _init_timeline_audio(self):
        self._waveform_results = queue.Queue()
        self._waveform_cache = OrderedDict()
        self._waveform_pending = set()
        self._waveform_stop = threading.Event()
        self._waveform_processes = set()
        self._waveform_lock = threading.Lock()
        self._waveform_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='audio-waveform')
        self._audio_track_buttons = {}
        self._waveform_poll_after = self.after(120, self._poll_timeline_audio)

    def _timeline_audio_tracks(self, cameras):
        return ([0] if self.project.master_audio else []) + list(cameras)

    def _waveform_for(self, path):
        if not hasattr(self, '_waveform_cache'): self._init_timeline_audio()
        key = waveform_key(path)
        if key is None: return None
        cached = self._waveform_cache.get(key)
        if cached is not None:
            self._waveform_cache.move_to_end(key)
            return cached
        if key in self._waveform_pending or len(self._waveform_pending) >= 8 or self._waveform_stop.is_set():
            return None
        ffmpeg = self.app.caps.ffmpeg_path or find_binary('ffmpeg', Path(__file__).parent)
        if not ffmpeg or not Path(ffmpeg).is_file():
            cached = Waveform((), state='unavailable')
            self._waveform_cache[key] = cached
            return cached
        self._waveform_pending.add(key)
        def started(process):
            with self._waveform_lock:
                self._waveform_processes.add(process)
                if self._waveform_stop.is_set():
                    try: process.terminate()
                    except OSError: pass
        def finished(process):
            with self._waveform_lock: self._waveform_processes.discard(process)
        def worker():
            try:
                value = decode_waveform(ffmpeg, path, self._waveform_stop,
                                        process_started=started, process_finished=finished)
            except Exception:
                value = Waveform((), state='unavailable')
            self._waveform_results.put((key, value))
        try: self._waveform_pool.submit(worker)
        except RuntimeError: self._waveform_pending.discard(key)
        return None

    def _poll_timeline_audio(self):
        self._waveform_poll_after = None
        if self._waveform_stop.is_set(): return
        changed = False
        try:
            while True:
                key, waveform = self._waveform_results.get_nowait()
                self._waveform_pending.discard(key)
                if waveform is not None:
                    self._waveform_cache[key] = waveform
                    self._waveform_cache.move_to_end(key)
                    while len(self._waveform_cache) > 128: self._waveform_cache.popitem(last=False)
                    changed = True
        except queue.Empty:
            pass
        if changed and self.winfo_exists(): self.draw_timeline()
        self._waveform_poll_after = self.after(120, self._poll_timeline_audio)

    def _set_timeline_audio_track(self, track, enabled, *, solo=False):
        if not self._available(): return
        tracks = self._timeline_audio_tracks(self._timeline_camera_order())
        states = {candidate: audio_track_enabled(self.project, candidate) for candidate in tracks}
        target = {candidate: candidate == track for candidate in tracks} if solo else dict(states)
        if not solo: target[track] = bool(enabled)
        if target == states: return
        self._remember()
        # The first explicit setting must retain the old fallback state of all
        # other tracks. Otherwise enabling A2 would silently turn A1 off.
        self.project.audio_track_enabled.update(target)
        update = getattr(self, '_audio_track_changed', None)
        if update is not None: update()
        else: self._configure_player()
        self.draw_timeline()
        state = '出声' if audio_track_enabled(self.project, track) else '静音'
        self.status.set(f'A{track} 已设为{state}；Ctrl+Z 撤销。')

    def _toggle_timeline_audio_track(self, track):
        self._set_timeline_audio_track(track, not audio_track_enabled(self.project, track))

    def _timeline_audio_press(self, event):
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        for track, (left, top, right, bottom) in getattr(self, '_audio_track_buttons', {}).items():
            if left <= x <= right and top <= y <= bottom:
                self._toggle_timeline_audio_track(track)
                return True
        return False

    def _timeline_audio_context(self, event):
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        header_left = self.timeline.canvasx(0)
        if not header_left <= x <= header_left+self._timeline_left: return False
        track = next((track for track, (top, bottom) in getattr(self, '_timeline_audio_track_bounds', {}).items()
                      if top <= y <= bottom), None)
        if track is None: return False
        menu = tk.Menu(self, tearoff=False, bg='#292b2e', fg='#dedfe3', activebackground='#3c5263')
        state = 'normal' if not (self.busy or self.app.working) else 'disabled'
        enabled = audio_track_enabled(self.project, track)
        menu.add_command(label='设为静音' if enabled else '设为出声', state=state,
                         command=lambda: self._toggle_timeline_audio_track(track))
        menu.add_command(label='仅此轨出声', state=state,
                         command=lambda: self._set_timeline_audio_track(track, True, solo=True))
        previous = getattr(self, 'audio_track_context_menu', None)
        if previous is not None: previous.destroy()
        self.audio_track_context_menu = menu
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()
        return True

    def _draw_timeline_audio(self, cameras, audio_y, audio_h, width, label_font, timeline_x, visible):
        canvas = self.timeline
        self._audio_track_buttons = {}
        self._timeline_audio_track_bounds = {}
        self._timeline_audio_bounds = None
        tracks = self._timeline_audio_tracks(cameras)
        duration = self._timeline_view_duration()
        view_left = canvas.canvasx(0)
        view_right = canvas.canvasx(canvas.winfo_width())
        pps = self._timeline_scale()
        for index, track in enumerate(tracks):
            y = audio_y + index*audio_h
            self._timeline_audio_track_bounds[track] = (y, y+audio_h)
            if track == 0: self._timeline_audio_bounds = (y, y+audio_h)
            if not visible(0, y, width, y+audio_h): continue
            enabled = audio_track_enabled(self.project, track)
            canvas.create_rectangle(0, y, width, y+audio_h, fill='#232c29' if enabled else '#27292b', outline='')
            if track == 0:
                clips = [(self.project.master_audio, 0., min(duration, self.project.master_duration or duration), True)]
            else:
                clips = [(clip.path, clip.start, clip.duration, getattr(clip, 'has_audio', None))
                         for clip in self.project.clips if clip.camera == track]
            for path, start, length, has_audio in clips:
                x, x2 = timeline_x(start), timeline_x(start+length)
                if x2 <= x or not visible(x, y, x2, y+audio_h): continue
                fill = '#36594e' if enabled else '#343c3b'
                item = canvas.create_rectangle(x, y+self._px(2), x2, y+audio_h-self._px(2),
                                               fill=fill, outline='#5e8074' if enabled else '#465650', tags=('audio-track',))
                if track == 0: self.timeline_audio_items.add(item)
                waveform = self._waveform_for(path) if has_audio is not False else None
                wave_left, wave_right = max(x, view_left), min(x2, view_right)
                wave_top, wave_bottom = y+self._px(17), y+audio_h-self._px(4)
                if waveform is not None and waveform.state == 'ready' and wave_right > wave_left:
                    source_start = max(0., (wave_left-x)/pps)
                    source_end = min(length, (wave_right-x)/pps)
                    peaks = envelope_points(waveform, source_start, source_end,
                                            max(1, int((wave_right-wave_left)/max(1,self._px(2)))))
                    center, amplitude = (wave_top+wave_bottom)/2, max(1., (wave_bottom-wave_top)/2)
                    if len(peaks):
                        step = (wave_right-wave_left)/len(peaks)
                        top = [(wave_left+i*step, center-float(value)*amplitude) for i,value in enumerate(peaks)]
                        bottom = [(wave_left+i*step, center+float(value)*amplitude) for i,value in reversed(list(enumerate(peaks)))]
                        points = [value for point in top+bottom for value in point]
                        if len(points) >= 6:
                            canvas.create_polygon(*points, fill='#9ed6bf' if enabled else '#6d8f7f', outline='', tags=('audio-waveform',))
                if x2-x > self._px(65):
                    status = ''
                    if has_audio is False or (waveform is not None and waveform.state == 'no_audio'): status = ' · 无音频'
                    elif waveform is None: status = ' · 读取波形…' if waveform_key(path) else ' · 离线'
                    elif waveform.state != 'ready': status = ' · 波形不可用'
                    title = Path(path).name+status
                    title = self._timeline_fit_label(label_font, title, x2-x-self._px(10))
                    item = canvas.create_text(x+self._px(5), y+self._px(9), text=title,
                        fill='#d1e5db' if enabled else '#a5b0ac', anchor='w', font=label_font, tags=('audio-track',))
                    if track == 0: self.timeline_audio_items.add(item)
            # Draw headers last, covering the part of negative-start audio beneath them.
            canvas.create_rectangle(view_left, y, view_left+self._timeline_left, y+audio_h, fill='#242c2a', outline='')
            canvas.create_text(view_left+self._px(5), y+audio_h/2, text=f'A{track}', fill='#8ab7a4', anchor='w', font=label_font)
            left, right = view_left+self._px(28), view_left+self._timeline_left-self._px(3)
            if right > left:
                bounds = left, y+self._px(5), right, y+audio_h-self._px(5)
                self._audio_track_buttons[track] = bounds
                canvas.create_rectangle(*bounds, fill='#42715d' if enabled else '#3d4143',
                                        outline='#77af93' if enabled else '#62686b', tags=('audio-mute-button',))
                button_label = ('主音频 · ' if track == 0 else '') + ('出声' if enabled else '静音')
                canvas.create_text((left+right)/2, y+audio_h/2, text=button_label,
                                   fill='#e3f1e8' if enabled else '#b9bec1', font=label_font, tags=('audio-mute-button',))

    def _close_timeline_audio(self):
        if not hasattr(self, '_waveform_stop'): return
        self._waveform_stop.set()
        pending = getattr(self, '_waveform_poll_after', None)
        if pending is not None:
            try: self.after_cancel(pending)
            except Exception: pass
            self._waveform_poll_after = None
        with self._waveform_lock:
            for process in tuple(self._waveform_processes):
                if process.poll() is None:
                    try: process.terminate()
                    except OSError: pass
        self._waveform_pool.shutdown(wait=False, cancel_futures=True)
