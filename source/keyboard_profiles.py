"""Windows keyboard presets for commands supported by OKK STUDIO."""
from __future__ import annotations
import json
import os
from pathlib import Path
from shortcut_keys import canonical_key

PROFILE_LABELS = {'current': '当前键位', 'resolve': 'DaVinci Resolve', 'premiere': 'Premiere Pro'}
COMMON = {'space': 'toggle', 'Left': 'previous_frame', 'Right': 'next_frame',
          'm': 'marker', 'Delete': 'delete', 'BackSpace': 'delete', 'Escape': 'escape',
          'Ctrl+z': 'undo', 'Ctrl+s': 'save', 'Ctrl+a': 'select_all',
          **{str(i): f'camera_{i}' for i in range(1, 10)}}
EDITING = {'j': 'reverse', 'k': 'pause', 'l': 'play', 'i': 'mark_in', 'o': 'mark_out',
           'Shift+i': 'go_in', 'Shift+o': 'go_out', 'Up': 'previous_cut', 'Down': 'next_cut',
           'Home': 'home', 'End': 'end', 'Ctrl+Shift+z': 'redo', 'Ctrl+Shift+a': 'deselect',
           'Ctrl+o': 'open', 'Ctrl+Shift+s': 'save_as', 'Ctrl+Alt+k': 'show_keys'}
PROFILE_BINDINGS = {
    'current': {**COMMON, 'c': 'split', 'Ctrl+y': 'redo'},
    'resolve': {**COMMON, **EDITING, 'a': 'select_tool', 'b': 'razor_tool', 'Ctrl+b': 'split',
                'Alt+i': 'clear_in', 'Alt+o': 'clear_out', 'Alt+x': 'clear_range',
                'Ctrl+=': 'zoom_in', 'Ctrl+Shift+=': 'zoom_in', 'Ctrl+-': 'zoom_out', 'Shift+z': 'fit'},
    'premiere': {**COMMON, **EDITING, 'v': 'select_tool', 'c': 'razor_tool', 'Ctrl+k': 'split',
                 'Ctrl+Shift+i': 'clear_in', 'Ctrl+Shift+o': 'clear_out', 'Ctrl+Shift+x': 'clear_range',
                 '=': 'zoom_in', 'Shift+=': 'zoom_in', '-': 'zoom_out', '\\': 'fit'},
}
ACTION_LABELS = {
    'toggle': '播放 / 暂停', 'previous_frame': '上一帧（长按倒放定位）',
    'next_frame': '下一帧（长按正放定位）', 'split': '在播放头切开节目片段',
    'select_tool': '选择工具', 'razor_tool': '剃刀工具（点击片段切开节目）',
    'reverse': '倒放定位（K 停止）', 'pause': '停止播放 / 定位', 'play': '向前播放',
    'marker': '添加时间标记', 'mark_in': '标记成片入点', 'mark_out': '标记成片出点',
    'go_in': '定位成片入点', 'go_out': '定位成片出点', 'clear_in': '清除入点',
    'clear_out': '清除出点', 'clear_range': '清除入点与出点',
    'previous_cut': '上一个节目切点', 'next_cut': '下一个节目切点',
    'home': '定位开头', 'end': '定位末帧', 'undo': '撤销', 'redo': '重做',
    'save': '保存工程', 'save_as': '完整工程另存为', 'open': '打开工程',
    'select_all': '全选素材与字幕', 'deselect': '取消选中', 'delete': '删除所选内容',
    'escape': '取消操作 / 返回选择工具', 'zoom_in': '放大时间线', 'zoom_out': '缩小时间线',
    'fit': '适配时间线', 'show_keys': '查看键位表',
}

def checked_profile(profile):
    return profile if isinstance(profile, str) and profile in PROFILE_LABELS else 'current'

def shortcut_action(profile, event):
    profile = checked_profile(profile)
    state = getattr(event, 'state', 0)
    key = canonical_key(event)
    code = getattr(event, 'keycode', None)
    if os.name == 'nt' and code in {0xBB, 0xBD, 0xDC}:
        key = {0xBB: '=', 0xBD: '-', 0xDC: '\\'}[code]
    shift, ctrl, alt = bool(state & 1), bool(state & 4), bool(state & (0x20000 | 8))
    # The original preset treated shifted editor keys like unshifted keys.
    if profile == 'current': shift = False
    chord = ('Ctrl+' if ctrl else '') + ('Alt+' if alt else '') + ('Shift+' if shift else '') + key
    return PROFILE_BINDINGS[profile].get(chord)

def shortcut_label(profile, action):
    names = {'space': '空格', 'Left': '←', 'Right': '→', 'Up': '↑', 'Down': '↓'}
    for chord, value in PROFILE_BINDINGS[checked_profile(profile)].items():
        if value == action:
            return '+'.join(names.get(part, part.upper() if len(part) == 1 else part) for part in chord.split('+'))
    return '—'

def preferences_path():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home())/'OKK STUDIO'/'keyboard-shortcuts.json'

def load_profile(path=None):
    try:
        value = json.loads(Path(path or preferences_path()).read_text(encoding='utf-8'))
        return checked_profile(value.get('profile')) if isinstance(value, dict) else 'current'
    except (OSError, ValueError):
        return 'current'

def save_profile(profile, path=None):
    path = Path(path or preferences_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.pending')
    try:
        temporary.write_text(json.dumps({'version': 1, 'profile': checked_profile(profile)}), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
