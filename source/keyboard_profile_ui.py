"""Keyboard presets in settings and an inspectable shortcut reference."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from keyboard_profiles import (PROFILE_LABELS, ACTION_LABELS, checked_profile,
                               load_profile, save_profile, shortcut_label)

class KeyboardProfileSettingsMixin:
    def _init_keyboard_profile(self):
        self.keyboard_profile = load_profile()
        self.keyboard_profile_var = tk.StringVar(self, value=PROFILE_LABELS[self.keyboard_profile])

    def _build_keyboard_settings(self, panel, window, values):
        selected = tk.StringVar(window, value=PROFILE_LABELS[self.keyboard_profile])
        values['keyboard_profile'] = selected
        row = tk.Frame(panel, bg='#202124')
        row.pack(fill='x', pady=(0, 20))
        tk.Label(row, text='快捷键方案', bg='#202124', fg='#dedfe3').pack(side='left')
        combo = ttk.Combobox(row, textvariable=selected, values=tuple(PROFILE_LABELS.values()),
                             state='readonly', width=24)
        combo.pack(side='right')
        ttk.Button(panel, text='查看键位对照表…', command=self._show_keyboard_reference).pack(anchor='w')
        tk.Label(panel, text='选择当前键位、DaVinci Resolve 或 Premiere Pro。\n'
            '点击“保存设置”后生效，并在下次启动时继续使用。',
            bg='#202124', fg='#969ba3', justify='left', anchor='w').pack(fill='x', pady=(24, 0))
        def validate():
            profile = next((key for key, label in PROFILE_LABELS.items() if label == selected.get()), None)
            if profile is None:
                raise ValueError('请选择有效的快捷键方案。')
            return profile
        return validate

    def _apply_keyboard_settings(self, profile):
        if profile == self.keyboard_profile:
            return
        # Persist before publishing so a failed write leaves the dialog retryable.
        save_profile(profile)
        self.keyboard_profile_var.set(PROFILE_LABELS[profile])
        self._keyboard_profile_selected(persist=False, focus=False)

    def _keyboard_profile_selected(self, _event=None, *, persist=True, focus=True):
        label = self.keyboard_profile_var.get()
        profile = next((key for key, name in PROFILE_LABELS.items() if name == label), 'current')
        self.keyboard_profile = checked_profile(profile)
        ws = getattr(self, 'multicam_workspace', None)
        if ws is not None:
            ws._stop_transport_hold()
            ws._marker_key_release()
            ws._transport_space_pressed = False
            ws._set_keyboard_tool('select')
        try:
            if persist: save_profile(self.keyboard_profile)
            status = '已切换为 '+PROFILE_LABELS[self.keyboard_profile]+' 键位。'
        except OSError as error:
            status = '键位已切换；偏好保存失败：'+str(error)
        if ws is not None:
            ws.status.set(status)
            if focus and ws.winfo_ismapped(): ws.timeline.focus_set()

    def _show_keyboard_reference(self):
        previous = getattr(self, '_keyboard_reference_window', None)
        if previous is not None and previous.winfo_exists():
            previous.lift(); return
        window = self._keyboard_reference_window = tk.Toplevel(self)
        workspace = getattr(self, 'multicam_workspace', None)
        settings = vars(workspace).get('_project_settings_window') if workspace is not None else None
        if settings is not None and settings.winfo_exists():
            window.transient(settings)
            window.grab_set()
        def close():
            window.destroy()
            if settings is not None and settings.winfo_exists():
                settings.grab_set()
        window.protocol('WM_DELETE_WINDOW', close)
        window.bind('<Escape>', lambda _event: close())
        window.title('OKK STUDIO · 键位对照表')
        window.geometry('920x650')
        window.minsize(760, 400)
        window.configure(bg='#202124')
        tk.Label(window, text='在“工程设置 → 快捷键”中切换键位；以下为本软件支持的操作。',
                 bg='#202124', fg='#dedfe3', anchor='w').pack(fill='x', padx=14, pady=12)
        body = ttk.Frame(window); body.pack(fill='both', expand=True, padx=14)
        tree = ttk.Treeview(body, columns=tuple(PROFILE_LABELS), show='tree headings', selectmode='browse')
        tree.heading('#0', text='操作'); tree.column('#0', width=300, minwidth=200)
        for key, label in PROFILE_LABELS.items():
            tree.heading(key, text=label); tree.column(key, width=180, minwidth=135, anchor='center')
        for action, title in ACTION_LABELS.items():
            tree.insert('', 'end', text=title,
                        values=tuple(shortcut_label(key, action) for key in PROFILE_LABELS))
        tree.insert('', 'end', text='切换机位（保留 OKK 多机位操作）', values=('1–9',)*3)
        scroll = ttk.Scrollbar(body, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); tree.pack(fill='both', expand=True)
        ttk.Button(window, text='关闭', command=close).pack(pady=12)
