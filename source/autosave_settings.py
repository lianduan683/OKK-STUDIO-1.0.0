"""Local autosave preferences and staged settings controls."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk

DEFAULT_INTERVAL_SECONDS = 30


def preferences_path():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'OKK STUDIO' / 'autosave-settings.json'


def validate_interval(value):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('自动保存间隔须为 1–86400 秒的整数。') from None
    if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer() or not 1 <= number <= 86400:
        raise ValueError('自动保存间隔须为 1–86400 秒的整数。')
    return int(number)


def load_preferences(path=None):
    try:
        value = json.loads(Path(path or preferences_path()).read_text(encoding='utf-8'))
        if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
            raise ValueError('Invalid autosave preferences')
        return value['enabled'], validate_interval(value['interval_seconds'])
    except (OSError, ValueError, KeyError, TypeError):
        return True, DEFAULT_INTERVAL_SECONDS


def save_preferences(enabled, interval_seconds, path=None):
    interval_seconds = validate_interval(interval_seconds)
    if type(enabled) is not bool:
        raise ValueError('自动保存开关无效。')
    path = Path(path or preferences_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.pending')
    try:
        temporary.write_text(json.dumps(dict(version=1, enabled=enabled,
            interval_seconds=interval_seconds), ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AutosaveSettingsMixin:
    def _init_autosave_settings(self):
        self.autosave_enabled, self.autosave_interval_seconds = load_preferences()
        self._project_autosave_after = None

    def _build_autosave_settings(self, panel, window, values):
        enabled = tk.BooleanVar(window, value=self.autosave_enabled)
        interval = tk.StringVar(window, value=str(self.autosave_interval_seconds))
        values.update(autosave_enabled=enabled, autosave_interval=interval)
        ttk.Checkbutton(panel, text='开启自动保存', variable=enabled).pack(anchor='w', pady=(0, 20))
        row = tk.Frame(panel, bg='#202124')
        row.pack(fill='x')
        tk.Label(row, text='保存间隔（秒）', bg='#202124', fg='#dedfe3').pack(side='left')
        entry = ttk.Spinbox(row, from_=1, to=86400, textvariable=interval, width=14)
        entry.pack(side='right')
        def toggle(*_):
            entry.configure(state='normal' if enabled.get() else 'disabled')
        enabled.trace_add('write', toggle)
        toggle()
        tk.Label(panel, text='按设定间隔保存有修改的工程；开关与间隔在本机记住。\n'
            '已命名工程自动写入原工程文件，同时保留完整会话恢复副本。\n'
            '未命名工程保存恢复副本，可从“工程 → 恢复上次会话”找回。\n'
            '处理任务或拖动编辑时稍后保存。关闭后停止自动写入。',
            bg='#202124', fg='#969ba3', justify='left', anchor='w',
            wraplength=480).pack(fill='x', pady=(24, 0))
        def validate():
            return bool(enabled.get()), validate_interval(interval.get()) if enabled.get() else self.autosave_interval_seconds
        return validate

    def _apply_autosave_settings(self, enabled, interval_seconds):
        interval_seconds = validate_interval(interval_seconds)
        if (enabled, interval_seconds) == (self.autosave_enabled, self.autosave_interval_seconds):
            return
        save_preferences(enabled, interval_seconds)
        self.autosave_enabled, self.autosave_interval_seconds = enabled, interval_seconds
        self._schedule_project_autosave()
