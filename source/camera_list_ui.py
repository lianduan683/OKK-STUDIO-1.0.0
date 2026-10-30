"""Camera-tree folding and actionable reports for unconfirmed audio alignment."""
from __future__ import annotations
import math
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from editor_core import AlignmentResult


class CameraListUIMixin:
    def _build_camera_list_tools(self, parent):
        row = tk.Frame(parent, bg='#292b2e')
        row.pack(fill='x', padx=self._px(6), pady=(0, self._px(3)))
        self.camera_fold_button = ttk.Button(row, text='全部收起', width=9,
            command=self._toggle_media_camera_groups, style='Compact.TButton')
        self.camera_fold_button.pack(side='left')
        self.sync_review_summary = tk.StringVar(self, value='')
        tk.Label(row, textvariable=self.sync_review_summary, bg='#292b2e', fg='#d3b880',
                 font=('Microsoft YaHei UI', 8), anchor='w').pack(side='left', padx=self._px(6), fill='x', expand=True)
        self.sync_review_button = ttk.Button(row, text='查看', width=5,
            command=self._show_sync_review, style='Compact.TButton')

    def _update_camera_fold_button(self):
        self._camera_fold_after = None
        if not self.alive: return
        parents = self.media_tree.get_children()
        any_open = any(self.media_tree.item(parent, 'open') for parent in parents)
        if parents: self._media_tree_collapsed = not any_open
        self.camera_fold_button.configure(text='全部收起' if any_open else '全部展开',
                                           state='normal' if parents else 'disabled')

    def _queue_camera_fold_update(self, _event=None):
        if getattr(self, '_camera_fold_after', None) is None:
            self._camera_fold_after = self.after_idle(self._update_camera_fold_button)

    def _toggle_media_camera_groups(self):
        tree = self.media_tree
        parents = tree.get_children()
        open_groups = not any(tree.item(parent, 'open') for parent in parents)
        selected = tree.selection()
        for parent in parents: tree.item(parent, open=open_groups)
        if not open_groups and selected:
            parent = tree.parent(selected[0])
            if parent: tree.selection_set(parent); tree.focus(parent)
        self._media_tree_collapsed = not open_groups
        self._update_camera_fold_button()

    def _sync_clip_state(self, clip_id):
        result = self.sync_results.get(clip_id)
        if result is None: return ''
        return ('synced' if math.isfinite(result.confidence) and math.isfinite(result.offset_seconds)
                and result.confidence >= .2 else 'review')

    def _camera_tree_label(self, camera):
        pending = sum(self._sync_clip_state(clip.id) == 'review'
                      for clip in self.project.clips if clip.camera == camera)
        return self._label_camera(camera) + (f' · {pending} 段待检查' if pending else '')

    def _camera_clip_tree_text(self, clip):
        state = self._sync_clip_state(clip.id)
        title = Path(clip.path).name+' · '+self._clip_camera_label(clip)
        if state == 'review': return '[未对齐] '+title+' · 待检查'
        return title + (' · 已对齐' if state == 'synced' else '')

    def _sync_review_clips(self):
        return sorted((clip for clip in self.project.clips if self._sync_clip_state(clip.id) == 'review'),
                      key=lambda clip: (clip.camera, clip.start, clip.id))

    @staticmethod
    def _sync_review_reason(result):
        if not math.isfinite(result.confidence) or not math.isfinite(result.offset_seconds):
            return '同步结果无效，未应用位置'
        reason = str(result.engine)
        if any(word in reason for word in ('失败', '待手动', '无音频', '未找到', '不一致')):
            return reason
        return '匹配可信度不足，需人工检查'+(' · '+reason if reason else '')

    def _refresh_sync_review(self):
        pending = self._sync_review_clips()
        confirmed = sum(self._sync_clip_state(clip.id) == 'synced' for clip in self.project.clips)
        self.sync_review_summary.set(f'待检查 {len(pending)} 段' if pending else
                                     f'已对齐 {confirmed} 段' if confirmed else '')
        if pending: self.sync_review_button.pack(side='right')
        else: self.sync_review_button.pack_forget()
        window = getattr(self, '_sync_review_window', None)
        if window is None or not window.winfo_exists(): return
        if not pending:
            self._close_sync_review()
            return
        tree = self._sync_review_tree
        selected = tree.selection()
        tree.delete(*tree.get_children())
        for clip in pending:
            result = self.sync_results[clip.id]
            confidence = f'{result.confidence:.1%}' if math.isfinite(result.confidence) else '无效'
            tree.insert('', 'end', iid=clip.id,
                values=(self._label_camera(clip.camera), Path(clip.path).name, confidence, self._sync_review_reason(result)))
        selected = next((value for value in selected if tree.exists(value)), pending[0].id)
        tree.selection_set(selected)
        tree.focus(selected)
        self._sync_review_title.set(f'{len(pending)} 段未对齐 / 待检查 · 参考：'
            +getattr(self, '_alignment_reference_label', '主音频')+' · 均保留原位置')
        self._sync_review_selection()

    def _show_sync_review(self):
        if not self._sync_review_clips(): return
        window = getattr(self, '_sync_review_window', None)
        if window is not None and window.winfo_exists():
            self._refresh_sync_review()
            window.deiconify(); window.lift()
            return
        window = self._sync_review_window = tk.Toplevel(self)
        window.title('同步结果 · 未对齐片段')
        window.configure(bg='#292b2e')
        window.geometry(f'{self._px(960)}x{self._px(440)}+{self.winfo_rootx()+30}+{self.winfo_rooty()+60}')
        window.minsize(self._px(640), self._px(300))
        window.protocol('WM_DELETE_WINDOW', self._close_sync_review)
        self._sync_review_title = tk.StringVar(self)
        tk.Label(window, textvariable=self._sync_review_title, bg='#292b2e', fg='#d3b880',
                 anchor='w').pack(fill='x', padx=12, pady=12)
        body = ttk.Frame(window)
        body.pack(fill='both', expand=True, padx=12)
        tree = self._sync_review_tree = ttk.Treeview(body, columns=('camera', 'file', 'confidence', 'reason'),
            show='headings', selectmode='browse')
        for key, title, width in (('camera', '机位', 155), ('file', '未对齐片段', 260),
                                  ('confidence', '可信度', 75), ('reason', '原因 / 需检查项', 425)):
            tree.heading(key, text=title)
            tree.column(key, width=self._px(width), minwidth=self._px(55), stretch=key in {'file', 'reason'})
        vertical = ttk.Scrollbar(body, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(body, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        body.rowconfigure(0, weight=1); body.columnconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky='nsew'); vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        self._sync_review_detail = tk.StringVar(self)
        tk.Label(window, textvariable=self._sync_review_detail, bg='#292b2e', fg='#dedfe3',
                 anchor='w', justify='left', wraplength=self._px(900)).pack(fill='x', padx=12, pady=8)
        buttons = tk.Frame(window, bg='#292b2e'); buttons.pack(fill='x', padx=12, pady=(0, 12))
        ttk.Button(buttons, text='定位所选片段', command=self._locate_sync_review).pack(side='left')
        ttk.Button(buttons, text='关闭', command=self._close_sync_review).pack(side='right')
        tree.bind('<<TreeviewSelect>>', self._sync_review_selection)
        tree.bind('<Double-1>', self._locate_sync_review)
        tree.bind('<Return>', self._locate_sync_review)
        self._refresh_sync_review()

    def _sync_review_selection(self, _event=None):
        selected = self._sync_review_tree.selection()
        clip = next((clip for clip in self.project.clips if selected and clip.id == selected[0]), None)
        if clip is not None and clip.id in self.sync_results:
            self._sync_review_detail.set(clip.path+'\n'+self._sync_review_reason(self.sync_results[clip.id]))

    def _locate_sync_review(self, _event=None):
        if not self._available(): return
        selected = self._sync_review_tree.selection()
        clip = next((clip for clip in self.project.clips if selected and clip.id == selected[0]), None)
        if clip is None: return
        if not self._media_visible: self.toggle_media_pool()
        self._select_workspace_page(self.tools, self.media_tab)
        self.media_tree.item(f'camera_{clip.camera}', open=True)
        self.media_tree.selection_set(clip.id); self.media_tree.focus(clip.id); self.media_tree.see(clip.id)
        self._update_camera_fold_button()
        self._reveal_timeline_clip(clip)
        self.seek(max(0., clip.start))
        self.status.set('待检查：'+self._label_camera(clip.camera)+' · '+Path(clip.path).name)

    def _record_alignment_failure(self, message):
        identifiers = set(getattr(self, '_alignment_attempt_ids', ()))
        for clip in self.project.clips:
            if clip.id in identifiers:
                self.sync_results[clip.id] = AlignmentResult(-clip.start, 0., 0, '同步失败：'+str(message))
        self._alignment_attempt_ids = ()
        if identifiers:
            self._refresh_camera_labels()
            self._show_sync_review()
        return bool(identifiers)

    def _close_sync_review(self):
        window = getattr(self, '_sync_review_window', None)
        if window is not None and window.winfo_exists(): window.destroy()
        self._sync_review_window = None

    def _close_camera_list_ui(self):
        pending = getattr(self, '_camera_fold_after', None)
        if pending is not None:
            self.after_cancel(pending)
            self._camera_fold_after = None
        self._close_sync_review()
