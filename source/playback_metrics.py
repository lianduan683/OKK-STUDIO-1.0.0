"""Wall-clock playback throughput; polling frequency is never a frame count.

MPV IPC estimates displayed frames from frame-number deltas minus decoder/VO
drops (see https://mpv.io/manual/stable/#properties). Native GPU and Tk stack
outputs count fresh frames actually presented. Seeking invalidates a sample.
"""
from collections import deque
import json
import math
import time
import tkinter as tk


class FrameRate:
    def __init__(self, window=1.5):
        self.window = window
        self.samples = deque()

    def reset(self):
        self.samples.clear()

    def sample(self, now, count):
        if not isinstance(count, (int, float)) or not math.isfinite(count):
            self.reset(); return None
        if self.samples and (count < self.samples[-1][1] or now <= self.samples[-1][0]): self.reset()
        self.samples.append((now, count))
        while len(self.samples) > 2 and self.samples[1][0] <= now-self.window:
            self.samples.popleft()
        elapsed = now-self.samples[0][0]
        return max(0., (count-self.samples[0][1])/elapsed) if elapsed >= .35 else None


class PlaybackMetricsPlayerMixin:
    def _poll_playback_metrics(self):
        now = time.monotonic()
        if now-getattr(self, '_metrics_last_poll', float('-inf')) < .5: return
        self._metrics_last_poll = now
        meters = vars(self).setdefault('_metrics_meters', {})
        if not self._playing:
            meters.clear()
            self._emit('playback_metrics', dict(source=None, program=None, playing=False))
            return
        active_keys = set()

        def measure(key, count):
            active_keys.add(key)
            return meters.setdefault(key, FrameRate()).sample(time.monotonic(), count)

        def slot_rate(slot):
            if slot is None or not slot.active or slot.controller is None: return None
            controller = slot.controller
            key = (id(controller), repr(slot.key), getattr(self, '_navigation_serial', 0))
            try:
                raw = controller.command(['expand-text', '{"frame":${estimated-frame-number:null},"drop":${frame-drop-count:0},"decoder_drop":${decoder-frame-drop-count:0},"seeking":"${seeking:false}"}'], timeout=.08)
                data = json.loads(raw)
                if data.get('seeking') in (True, 'yes'): return None
                frame = data.get('frame')
                if not isinstance(frame, (int, float)): return None
                count = frame-float(data.get('drop', 0))-float(data.get('decoder_drop', 0))
                meter = meters.get(key)
                # A drift seek must not look like hundreds of displayed frames.
                if meter and meter.samples:
                    elapsed = time.monotonic()-meter.samples[-1][0]
                    if count-meter.samples[-1][1] > 1000*max(.001, elapsed): meter.reset()
                return measure(key, count)
            except (RuntimeError, ValueError, TypeError, OSError): return None

        source_rates = [slot_rate(slot) for slot in self._slots.values() if slot.active]
        source = min(source_rates) if source_rates and all(rate is not None for rate in source_rates) else None
        program = None
        if not getattr(self, '_stack_enabled', False):
            if getattr(self, '_gpu_live_enabled', False) and self._gpu_session is not None:
                count = getattr(self._gpu_session, 'presented_video_frames', None)
                if count is not None: program = measure(('gpu', id(self._gpu_session)), count)
            else:
                program = slot_rate(self._program)
        for key in tuple(meters):
            if key not in active_keys: del meters[key]
        self._emit('playback_metrics', dict(source=source, program=program, playing=True))


class PlaybackMetricsUIMixin:
    def _init_playback_metrics(self):
        self.source_fps_var = tk.StringVar(self, value='— FPS')
        self.program_fps_var = tk.StringVar(self, value='— FPS')
        self._stack_frame_rate = FrameRate()
        self._stack_presented_count = 0
        self._stack_metrics_stamp = None
        self._stack_metrics_update = 0.

    def _reset_playback_metrics(self):
        self.source_fps_var.set('— FPS')
        self.program_fps_var.set('— FPS')
        self._stack_frame_rate.reset()
        self._stack_metrics_stamp = None

    @staticmethod
    def _fps_text(rate, playing):
        if not playing: return '暂停 · — FPS'
        return f'实时 {rate:.1f} FPS' if isinstance(rate, (int, float)) and math.isfinite(rate) else '实时 — FPS'

    def _apply_playback_metrics(self, payload):
        if self.player is None: return
        playing = bool(payload.get('playing')) and self.playing
        if not self._stack_preview_active:
            getattr(self, '_observe_prerender_fps', lambda _rate: None)(payload.get('program'))
        self.source_fps_var.set(self._fps_text(payload.get('source'), playing))
        if not self._stack_preview_active or not playing:
            self.program_fps_var.set(self._fps_text(payload.get('program'), playing))
        elif time.monotonic()-self._stack_metrics_update > 1.5:
            self.program_fps_var.set('实时 0.0 FPS')
            getattr(self, '_observe_prerender_fps', lambda _rate: None)(0.)

    def _count_presented_stack_frame(self, payload):
        if not self.playing:
            self._stack_frame_rate.reset(); return
        stamp = (payload.get('request_id'), payload.get('time'))
        if stamp == self._stack_metrics_stamp: return
        if self._stack_metrics_stamp and stamp[0] != self._stack_metrics_stamp[0]: self._stack_frame_rate.reset()
        self._stack_metrics_stamp = stamp
        self._stack_presented_count += 1
        now = time.monotonic()
        rate = self._stack_frame_rate.sample(now, self._stack_presented_count)
        self._stack_metrics_update = now
        getattr(self, '_observe_prerender_fps', lambda _rate: None)(rate)
        self.program_fps_var.set(self._fps_text(rate, True))
