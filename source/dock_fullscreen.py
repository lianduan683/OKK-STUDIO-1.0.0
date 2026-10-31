"""Fullscreen an existing floating panel without replacing its video HWNDs."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from monitor_fullscreen import WindowsMonitorFullscreen


class DockFullscreen:
    def __init__(self, manager):
        self.manager = manager
        self.active = None

    def geometry(self, window):
        if self.active is not None and self.active['window'] is window:
            return self.active['geometry']
        return str(window.tk.call('wm', 'geometry', window))

    def toggle(self, key):
        if self.active is not None and self.active['key'] == key:
            self.exit()
            return False
        self.exit()
        manager = self.manager
        panel = manager.panels[key]
        manager.focus(key)
        panel.widget.update_idletasks()
        native = WindowsMonitorFullscreen(panel.widget) if os.name == 'nt' else None
        window = manager.floating_group_window(key)
        if window is None:
            if key not in manager._floating:
                manager.float_panel(key)
            window = manager.panels[key].widget
        manager.focus(key)
        window.update_idletasks()
        group = manager._floating_group(key)
        host = manager._float_hosts[group['id']] if group else None
        if native is not None:
            native.capture(window)
        state = self.active = {
            'key': key, 'window': window, 'geometry': self.geometry(window),
            'state': str(window.tk.call('wm', 'state', window)),
            'focus': window.focus_get(), 'hidden': [], 'host': host,
            'style': host.notebook.cget('style') if host else None,
            'native': native,
        }
        try:
            for widget in (panel.header, panel.grip, host.header if host else None):
                if widget is not None and widget.winfo_manager() in ('pack', 'grid'):
                    home = manager._capture_home(widget)
                    state['hidden'].append((widget, home))
                    getattr(widget, home.manager + '_forget')()
            if host:
                style = ttk.Style(window)
                style.layout('Fullscreen.Dock.TNotebook', [])
                style.layout('Fullscreen.Dock.TNotebook.Tab', [])
                style.configure('Fullscreen.Dock.TNotebook', borderwidth=0, padding=0)
                host.notebook.configure(style='Fullscreen.Dock.TNotebook')
            window.tk.call('wm', 'attributes', window, '-fullscreen', True)
            if native is not None:
                native.cover()
            manager._sync_native_drag()
            window.focus_set()
        except Exception:
            self.exit()
            raise
        return True

    def exit(self):
        state = self.active
        if state is None:
            return False
        self.active = None
        window = state['window']
        if window.winfo_exists():
            window.tk.call('wm', 'attributes', window, '-fullscreen', False)
            if state['state'] == 'normal':
                window.tk.call('wm', 'state', window, 'normal')
                window.tk.call('wm', 'geometry', window, state['geometry'])
            else:
                window.tk.call('wm', 'state', window, state['state'])
            if state['native'] is not None:
                state['native'].restore()
            host = state['host']
            if host:
                host.notebook.configure(style=state['style'])
            for widget, home in reversed(state['hidden']):
                if widget.winfo_exists():
                    self.manager._mount(widget, home)
            focus = state['focus']
            if focus is not None and focus.winfo_exists():
                focus.focus_set()
            self.manager._sync_native_drag()
        return True
