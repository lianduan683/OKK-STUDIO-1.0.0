"""Upgrade validated project snapshots with an original-byte backup."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from multicam_core import MulticamProject
from project_file import (PROJECT_SCHEMA_VERSION, ProjectFileError, _now_text,
                          validate_project_payload, write_project_file)

MULTICAM_VERSION = 19


class ProjectUpgradeError(ValueError):
    pass


def read_project_snapshot(path, *, full=False):
    source = Path(path)
    try:
        original = source.read_bytes()
        if not full and len(original) > 32 * 1024 * 1024:
            raise ValueError('工程文件过大')
        data = json.loads(original.decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeError) as error:
        if full:
            raise ProjectFileError('工程文件无法读取：' + str(error)) from error
        raise ValueError('工程文件无法读取：' + str(error)) from error
    if full:
        validate_project_payload(data)
    return data, original


def needs_multicam_upgrade(payload):
    return (payload.get('version', 0) < MULTICAM_VERSION
            or not payload.get('timeline_view'))


def _current_project(workspace, base):
    project = copy.deepcopy(workspace.project)
    if not project.timeline_view:
        project.timeline_view = workspace._capture_timeline_view()
    if not project.monitor_layout:
        project.monitor_layout = workspace._capture_monitor_layout()
    return project.to_payload(base)


def upgraded_standalone_payload(data, workspace, path):
    if not needs_multicam_upgrade(data):
        return None
    return _current_project(workspace, Path(path).resolve().parent)


def upgraded_session_payload(data, app, path, app_version):
    state = data['state']
    old = state.get('okk_multicam')
    if (int(data.get('schema', 0)) == PROJECT_SCHEMA_VERSION and old
            and not needs_multicam_upgrade(old['project'])):
        return None
    workspace = app.multicam_workspace
    if not old:
        legacy = state.get('multicam') or []
        sources = legacy.get('sources', []) if isinstance(legacy, dict) else legacy
        if len(workspace.project.clips) < len(sources):
            raise ProjectUpgradeError('旧工程的多机位素材未能完整恢复，请重新链接素材后再保存。')
    from studio_state import collect_okk
    collected = collect_okk(app)
    upgraded = copy.deepcopy(data)
    upgraded.update(schema=PROJECT_SCHEMA_VERSION, app_version=app_version,
                    project_path=str(Path(path).resolve()), saved_at=_now_text())
    if not isinstance(upgraded['state'].get('okk_multicam'), dict):
        upgraded['state']['okk_multicam'] = {}
    section = upgraded['state']['okk_multicam']
    base = section.get('base_directory') or str(Path(path).resolve().parent)
    section['base_directory'] = base
    section['project'] = _current_project(workspace, base)
    settings = section.setdefault('settings', {})
    for name, value in collected['settings'].items():
        settings.setdefault(name, value)
    validate_project_payload(upgraded)
    return upgraded


def _backup_original(source, original):
    index = 0
    while True:
        suffix = '.pre-upgrade' + (f'.{index}' if index else '') + '.bak'
        backup = source.with_name(source.name + suffix)
        try:
            with backup.open('xb') as stream:
                try:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    stream.close()
                    backup.unlink(missing_ok=True)
                    raise
            return backup
        except FileExistsError:
            if not backup.is_symlink() and backup.is_file() and backup.read_bytes() == original:
                return backup
            index += 1


def write_project_upgrade(path, original, upgraded, *, full=False):
    if upgraded is None:
        return None
    source = Path(path).resolve()
    if full:
        validate_project_payload(upgraded)
    else:
        MulticamProject.from_payload(upgraded, source.parent)
    if source.read_bytes() != original:
        raise ProjectUpgradeError('工程文件在打开后发生了变化，请重新打开后升级。')
    backup = _backup_original(source, original)
    if source.read_bytes() != original:
        raise ProjectUpgradeError('工程文件在升级前发生了变化，已停止写入。')
    write_project_file(source, upgraded)
    return backup
