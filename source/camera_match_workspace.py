"""Transactional batch matching, sharing automatic-colour cancellation."""
from __future__ import annotations

import copy
import math
from pathlib import Path
import threading
from tkinter import messagebox

from camera_match import CameraMatchResult, METHODS, match_camera
from exporter_core import ExportCancelled
from transition_preview import transition_signature


class CameraMatchWorkspaceMixin:
    def _camera_match_context(self):
        return (transition_signature(self.project), self.project.color_reference_camera,
                tuple(self.selected_program_range) if self.selected_program_range else None,
                self.start_var.get(), self.end_var.get(), self.fps_var.get())

    def start_camera_match(self, reference_camera, targets, *, range_mode='selection',
                           strength=1., method='anchors', reference_time=None):
        if not self._available() or self._auto_color_running:
            return False
        try:
            if type(reference_camera) is not int or reference_camera not in self.project.cameras:
                raise ValueError('请先选择标准机位。')
            targets = list(targets)
            if not targets or any(type(camera) is not int or camera not in self.project.cameras for camera in targets):
                raise ValueError('请勾选需要匹配的目标机位。')
            if reference_camera in targets:
                raise ValueError('标准机位不能同时作为目标机位。')
            targets = sorted(set(targets))
            if (isinstance(strength, bool) or not isinstance(strength, (int, float))
                    or not math.isfinite(strength) or not 0 < strength <= 1):
                raise ValueError('匹配强度必须大于 0，且不超过 100%。')
            if method not in METHODS or range_mode not in {'selection', 'project'}:
                raise ValueError('机位匹配方式或取样范围无效。')
            ffmpeg, ffprobe = self.app.caps.ffmpeg_path, self.app.caps.ffprobe_path
            if not ffmpeg or not Path(ffmpeg).is_file():
                raise ValueError('请等待 FFmpeg 检测完成。')
            self.set_color_reference_camera(reference_camera)
            snapshot = copy.deepcopy(self.project)
            snapshot.validate()
            if range_mode == 'selection':
                selected = self.selected_program_range
                if selected is None:
                    span = self._program_span_at(self.current)
                    if span is None:
                        raise ValueError('请先选择当前场景的片段，或改用整个工程取样。')
                    selected = span.start, span.end
                first, last = selected
            else:
                first, end, _fps = self._read_range()
                last = end if end is not None else snapshot.duration
            reference_time = self.current if reference_time is None else reference_time
            if (isinstance(reference_time, bool) or not isinstance(reference_time, (int, float))
                    or not math.isfinite(reference_time) or not 0 <= reference_time <= snapshot.duration):
                raise ValueError('参考调色时刻必须在工程范围内。')
            if not 0 <= first < last <= snapshot.duration + 1e-7:
                raise ValueError('取样范围必须在工程范围内。')
            context = self._camera_match_context()
        except Exception as error:
            messagebox.showerror('标准机位匹配', str(error), parent=self)
            return False
        self.pause()
        self._cancel_grade_drafts()
        event = threading.Event()
        self._auto_color_cancel = event
        self._camera_match_running = True
        self._auto_color_set_running(True)
        self.camera_match_status.set(f'正在读取标准机位 {reference_camera} 的已调色画面…')

        def worker():
            results, cache = [], {}
            try:
                for index, camera in enumerate(targets):
                    if event.is_set():
                        raise ExportCancelled('已取消标准机位匹配')

                    def progress(percent, text, index=index, camera=camera):
                        self.jobs.put(('camera_match_progress',
                                       (event, camera, (index + percent / 100) / len(targets) * 100, text)))

                    try:
                        result = match_camera(snapshot, reference_camera, camera, first, last,
                            ffmpeg, ffprobe, strength=strength, method=method, reference_time=reference_time,
                            cancel_event=event, progress=progress, reference_cache=cache)
                    except ExportCancelled:
                        raise
                    except Exception as error:
                        result = CameraMatchResult(camera, None, message='无法匹配，保留原调色：' + str(error).split('\n')[0][:200])
                    results.append(result)
                self.jobs.put(('camera_match_ready', (event, context, reference_camera, results)))
            except Exception as error:
                self.jobs.put(('camera_match_error', (event, str(error), event.is_set() or isinstance(error, ExportCancelled))))

        self._auto_color_worker = threading.Thread(target=worker, name='standard-camera-match', daemon=False)
        self._auto_color_worker.start()
        return True

    def cancel_camera_match(self):
        if self._camera_match_running:
            self._auto_color_cancel.set()
            self.camera_match_status.set('正在取消，所有机位保留原调色…')

    def _handle_camera_match_job(self, kind, payload):
        event = payload[0]
        if event is not self._auto_color_cancel:
            return
        if kind == 'camera_match_progress':
            _, camera, percent, text = payload
            if not event.is_set():
                self.camera_match_status.set(f'机位 {camera} · {text} · 总进度 {percent:.0f}%')
            return
        self._camera_match_running = False
        self._auto_color_set_running(False)
        if kind == 'camera_match_error':
            _, text, cancelled = payload
            self.camera_match_status.set('已取消，所有机位保留原调色。' if cancelled else text)
            self.status.set(self.camera_match_status.get())
            return
        if kind != 'camera_match_ready':
            return
        _, context, reference, results = payload
        if event.is_set():
            self.camera_match_status.set('已取消，所有机位保留原调色。')
            return
        if (context != self._camera_match_context() or self.busy or self.app.working
                or self._grade_preview_pending is not None or self._grade_preview_active):
            self.camera_match_status.set('工程、片段或调色已变化，结果未应用；请重新匹配。')
            return
        updates = {item.camera: item.grade for item in results if item.grade is not None and item.camera != reference}
        if updates:
            candidate = copy.deepcopy(self.project)
            candidate.camera_grades.update(copy.deepcopy(updates))
            try:
                candidate.validate()
            except Exception as error:
                self.camera_match_status.set('结果校验失败，保留原调色：' + str(error))
                return
            self._remember()
            self.project = candidate
            self._refresh()
        panel = vars(self.color_workspace).get('camera_match_panel')
        if panel is not None:
            panel.show_results(results)
        unchanged = len(results) - len(updates)
        text = (f'已向标准机位 {reference} 匹配 {len(updates)} 个机位；'
                f'{unchanged} 个保持原调色。' + (' 可 Ctrl+Z 整批撤销。' if updates else ''))
        self.camera_match_status.set(text)
        self.status.set(text)

