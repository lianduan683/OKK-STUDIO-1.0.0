"""Explorer imports with explicit camera grouping and atomic timeline placement."""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from camera_import import grouped_import
from exporter_core import ExportCancelled, find_binary
from multicam_core import CameraClip

VIDEO_TYPES = [('视频', '*.mp4 *.mov *.mkv *.mxf *.avi *.mts *.m2ts *.webm *.mpg *.mpeg *.m4v *.wmv *.ts'),
               ('所有文件', '*.*')]


class CameraImportMixin:
    def add_camera_parts(self):
        if not self._available():
            return
        paths = filedialog.askopenfilenames(parent=self, title='导入同一机位的分段视频（按文件名顺序放入一条轨道）', filetypes=VIDEO_TYPES)
        if paths:
            self._start_camera_import(paths)

    def add_camera_folder(self, separate=False):
        if not self._available():
            return
        title = '选择机位文件夹（含子文件夹，全部视频放一条轨道）' if not separate else '选择文件夹（每个视频各建一个机位）'
        folder = filedialog.askdirectory(parent=self, title=title, mustexist=True)
        if folder:
            self._start_camera_import([folder], separate_folder_files=separate)

    def _start_camera_import(self, paths, *, separate_files=False, separate_folder_files=False, camera=None):
        if not paths or not self._available():
            return
        ffprobe = self.app.caps.ffprobe_path or find_binary('ffprobe', Path(__file__).parent)
        if not ffprobe:
            messagebox.showerror('导入机位', '未找到 ffprobe.exe。', parent=self)
            return
        self.busy = True
        self.app.cancel_event.clear()
        self.app._set_working(True)
        self.app._begin_shared_progress('读取机位素材 0%')
        self.status.set('正在扫描视频；同机位分段将先按顺序放入一条轨道。')
        project = self.project
        existing = tuple(project.timeline_cameras)
        cancel = self.app.cancel_event
        paths = tuple(paths)
        def worker():
            try:
                groups = grouped_import(ffprobe, paths, existing, camera=camera,
                    separate_files=separate_files, separate_folder_files=separate_folder_files,
                    cancel_event=cancel, progress=lambda percent, text: self.jobs.put(('camera_import_progress', (percent, text))))
                self.jobs.put(('camera_import_ready', (project, groups)))
            except Exception as error:
                self.jobs.put(('camera_import_error', (str(error), cancel.is_set() or isinstance(error, ExportCancelled))))
        threading.Thread(target=worker, name='camera-folder-import', daemon=False).start()

    def _handle_camera_import_job(self, kind, payload):
        if kind == 'camera_import_progress':
            percent, text = payload
            self.status.set(text)
            self.app.progress_var.set(percent)
            self.app.progress_text_var.set(text)
            return True
        if kind not in ('camera_import_ready', 'camera_import_error'):
            return False
        self.busy = False
        self.app._set_working(False)
        if kind == 'camera_import_error':
            text, cancelled = payload
            self.status.set('已取消导入，保留原时间线。' if cancelled else text)
            if not cancelled:
                messagebox.showerror('导入机位', text, parent=self)
            return True
        project, groups = payload
        if self.app.cancel_event.is_set() or project is not self.project:
            self.status.set('已取消导入，保留原时间线。')
            return True
        if not groups:
            return True
        self._remember()
        first = not project.clips
        had_master = bool(project.master_audio)
        imported = []
        for camera, infos in groups:
            start = max((clip.end for clip in project.clips if clip.camera == camera), default=0.)
            for info in infos:
                clip = CameraClip(uuid.uuid4().hex, camera, info.path, start, info.duration,
                                  has_alpha=bool(getattr(info, 'has_alpha', False)), has_audio=info.has_audio)
                project.clips.append(clip)
                imported.append(clip)
                start += info.duration
                if not project.master_audio and info.has_audio:
                    project.master_audio, project.master_duration = info.path, info.duration
        if first and not project.project_settings:
            project.fps = groups[0][1][0].fps or 30
            self.fps_var.set(f'{project.fps:.8g}')
        if not project.cuts:
            project.set_cut(0, groups[0][0])
        reference_camera = next((camera for camera, infos in groups
                                 if len(infos) > 1 and any(info.has_audio for info in infos)), None)
        reference_selected = False
        if not had_master and project.alignment_reference_camera is None and reference_camera is not None:
            # A split recording is a full reference track, not just its first
            # audio-bearing file. A later explicit master selection overrides it.
            project.alignment_reference_camera = reference_camera
            reference_selected = True
        self._refresh()
        self.fit_timeline()
        # Select a real clip, so linking proxies and synchronizing this camera
        # work immediately after an import without an extra tree expansion.
        if imported:
            self.media_tree.selection_set(imported[0].id)
            self.media_tree.see(imported[0].id)
        self.app.progress_var.set(100)
        count = len(imported)
        message = f'已导入 {count} 段视频到 {len(groups)} 条机位轨道；分段已首尾接齐。'
        if reference_selected:
            message += f'机位 {reference_camera} 整轨作为对齐参考；节目主音频暂为首段，请选择全程主音频。'
        else:
            message += '可选择全程主音频后同步。'
        self.status.set(message)
        return True

    def _setup_camera_drop(self):
        self._camera_drop_targets = []
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            TkinterDnD._require(self.winfo_toplevel())
            for widget in (self.media_tab, self.media_tree):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind('<<Drop>>', self._camera_explorer_drop)
                self._camera_drop_targets.append(widget)
        except (ImportError, RuntimeError, tk.TclError) as error:
            self.app._log(f'机位文件拖放不可用，可使用添加菜单：{error}')

    def _camera_explorer_drop(self, event):
        if not self._available():
            return 'none'
        try:
            paths = self.tk.splitlist(event.data)
        except tk.TclError:
            self.status.set('无法识别拖入路径，请使用添加机位菜单。')
            return 'none'
        if not paths:
            return 'none'
        if len(paths) == 1 and Path(paths[0]).suffix.lower() in ('.xml', '.fcpxml', '.fcpxmld', '.edl', '.otio', '.aaf', '.mlt', '.kdenlive'):
            self.import_timeline(paths[0])
            return 'copy'
        self._start_camera_import(paths)
        return 'copy'

    def _close_camera_drop(self):
        for widget in getattr(self, '_camera_drop_targets', ()):
            try:
                widget.drop_target_unregister()
                widget.unbind('<<Drop>>')
            except tk.TclError:
                pass
        self._camera_drop_targets = []
