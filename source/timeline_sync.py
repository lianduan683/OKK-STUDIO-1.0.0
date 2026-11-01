"""Select exact picture instances and anchored audio for waveform alignment."""
from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from camera_display import camera_number
from editor_core import format_signed_time
from timeline_audio import master_audio_clips, source_audio_clips
from timeline_link import edit_keys


def selected_video_clips(project, keys, media_selection=()):
    """Timeline selection takes precedence; only explicit tree headers expand."""
    keys = set(keys)
    if keys:
        identifiers = {identifier for kind, identifier in keys if kind == 'clip'}
        cameras = set()
    else:
        identifiers = set(media_selection)
        cameras = {camera for camera in project.cameras if f'camera_{camera}' in identifiers}
    return [clip for clip in project.clips if clip.id in identifiers or clip.camera in cameras]


def reference_audio_clips(project, keys):
    """Resolve sparse audio edits and A0 trims on their current project clock.

    Reference IDs are namespaced because detached audio and its video can
    share an edit ID while occupying different positions.
    """
    keys = set(keys)
    result = []
    for clip in master_audio_clips(project):
        if ('audio', clip.id) in keys:
            result.append(dict(id='audio:master:'+clip.id, camera=0, path=project.master_audio,
                start=clip.start, duration=clip.duration, source_in=clip.source_in))
    for clip in source_audio_clips(project):
        if ('camera_audio', clip.id) in keys and clip.has_audio is not False:
            result.append(dict(id='audio:camera:'+clip.id, camera=clip.camera, path=clip.path,
                start=clip.start, duration=clip.duration, source_in=clip.source_in))
    if len(result) != len(keys):
        raise ValueError('所选参考音频已不存在或没有音轨，请重新选择。')
    return result


class TimelineSyncMixin:
    def auto_align(self):
        if self._timeline_selection:
            self.align_selected()
        else:
            self.align()

    def align_selected(self, reference_mode=None):
        if not self._available(): return
        keys = set(self._timeline_selection)
        clips = selected_video_clips(self.project, keys, self.media_tree.selection())
        if not clips:
            messagebox.showinfo('自动对齐', '请先在时间线上选中需要对齐的视频片段。', parent=self)
            return
        # Linked A/V selection is represented by a picture key. Offer its
        # audio explicitly without changing edit/link behavior.
        effective = edit_keys(self.project, keys or {('clip', clip.id) for clip in clips})
        audio_keys = {key for key in effective if key[0] in {'audio', 'camera_audio'}}
        choices = []
        for key in sorted(audio_keys):
            try:
                reference = reference_audio_clips(self.project, {key})[0]
            except ValueError:
                continue
            choices.append((key, reference))
        camera_reference = self.project.alignment_reference_camera in self.project.cameras
        if not self.project.master_audio and not choices and not camera_reference:
            messagebox.showinfo('自动对齐', '请同时选中参考音频片段，或先导入主音频。', parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title('自动对齐所选视频')
        dialog.transient(self.winfo_toplevel())
        dialog.resizable(True, True)
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=f'对齐 {len(clips)} 个所选视频片段，参考音频保持原位。').pack(anchor='w')
        has_explicit_audio = any(kind in {'audio', 'camera_audio'} for kind, _ in keys)
        default = reference_mode or ('audio' if has_explicit_audio else 'camera' if camera_reference
                                     else 'master' if self.project.master_audio else 'audio')
        mode = tk.StringVar(dialog, value=default)
        master_radio = ttk.Radiobutton(frame, text='主音频（A0）', variable=mode, value='master')
        master_radio.pack(anchor='w', pady=(10, 4))
        if not self.project.master_audio: master_radio.configure(state='disabled')
        if camera_reference:
            ttk.Radiobutton(frame, text='参考机位：'+self._label_camera(self.project.alignment_reference_camera),
                            variable=mode, value='camera').pack(anchor='w', pady=4)
        ttk.Radiobutton(frame, text='选中的音频片段（可多选）', variable=mode, value='audio',
            state='normal' if choices else 'disabled').pack(anchor='w', pady=4)
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill='both', expand=True)
        listing = tk.Listbox(list_frame, selectmode='extended', exportselection=False,
                             width=72, height=min(8, max(3, len(choices))))
        listing.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=listing.yview)
        scrollbar.pack(side='right', fill='y')
        listing.configure(yscrollcommand=scrollbar.set)
        for key, reference in choices:
            label = f'A{camera_number(self.project, reference["camera"])} · {Path(reference["path"]).name}'
            listing.insert('end', label+' · '+format_signed_time(reference['start']))
        explicit = [index for index, (key, _) in enumerate(choices) if key in keys]
        for index in explicit or ([0] if choices else []): listing.selection_set(index)
        listing.bind('<<ListboxSelect>>', lambda _event: mode.set('audio'))
        ttk.Label(frame, text='Ctrl/Shift 可多选参考音频。绑定音频的对应视频也保持原位。').pack(anchor='w', pady=6)
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(6, 0))

        def start():
            chosen = {choices[index][0] for index in listing.curselection()}
            selected_mode = mode.get()
            if selected_mode == 'audio' and not chosen:
                messagebox.showinfo('自动对齐', '请选择至少一个参考音频片段。', parent=dialog)
                return
            dialog.destroy()
            self.align(True, reference_audio_keys=chosen if selected_mode == 'audio' else None,
                       use_master=selected_mode == 'master')

        ttk.Button(buttons, text='开始对齐', command=start).pack(side='right')
        ttk.Button(buttons, text='取消', command=dialog.destroy).pack(side='right', padx=8)
        dialog.bind('<Escape>', lambda _event: dialog.destroy())
        self.alignment_dialog = dialog
        dialog.grab_set()

    def _append_selection_sync_menu(self, menu):
        if not any(kind == 'clip' for kind, _ in self._timeline_selection): return
        menu.add_command(label='自动对齐所选视频…', command=self.align_selected)
