"""Keyboard presets in the layout menu and an inspectable shortcut reference."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from keyboard_profiles import (PROFILE_LABELS, ACTION_LABELS, checked_profile,
                               load_profile, save_profile, shortcut_label)

class KeyboardProfileSettingsMixin:
    def _init_keyboard_profile(self):
        self.keyboard_profile = load_profile()
        self.keyboard_profile_var = tk.StringVar(self, value=PROFILE_LABELS[self.keyboard_profile])

    def _append_keyboard_layout_menu(self, parent):
        menu = self.keyboard_profile_menu = tk.Menu(parent, tearoff=False,
            bg='#292b2e', fg='#dedfe3', activebackground='#3c5263', activeforeground='#dedfe3')
        for label in PROFILE_LABELS.values():
            menu.add_radiobutton(label=label, variable=self.keyboard_profile_var, value=label,
                                 command=self._keyboard_profile_selected)
        menu.add_separator()
        menu.add_command(label='键位表…', command=self._show_keyboard_reference)
        parent.add_cascade(label='键位选择', menu=menu)

    def _keyboard_profile_selected(self, _event=None):
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
            save_profile(self.keyboard_profile)
            status = '已切换为 '+PROFILE_LABELS[self.keyboard_profile]+' 键位。'
        except OSError as error:
            status = '键位已切换；偏好保存失败：'+str(error)
        if ws is not None:
            ws.status.set(status)
            if ws.winfo_ismapped(): ws.timeline.focus_set()

    def _show_keyboard_reference(self):
        previous = getattr(self, '_keyboard_reference_window', None)
        if previous is not None and previous.winfo_exists():
            previous.lift(); return
        window = self._keyboard_reference_window = tk.Toplevel(self)
        window.title('OKK STUDIO · 键位对照表')
        window.geometry('920x650')
        window.minsize(760, 400)
        window.configure(bg='#202124')
        tk.Label(window, text='在“布局 → 键位选择”中切换键位；以下为本软件支持的操作。',
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
        ttk.Button(window, text='关闭', command=window.destroy).pack(pady=12)
