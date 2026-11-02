"""Opt-in, authenticated localhost bridge; all Tk work stays on the UI thread."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import threading
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from okk_automation import BASE, EditSession, revision, snapshot


def discovery_directory():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home())/'OKK STUDIO'/'mcp'


def list_instances():
    result = []
    for path in sorted(discovery_directory().glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            result.append({'instance': path.stem, 'pid': data['pid'], 'bridge_file': str(path)})
        except (OSError, ValueError, KeyError):
            continue
    return result


class LiveClient:
    def __init__(self, instance=None):
        candidates = list_instances()
        if instance is None:
            reachable = []
            for candidate in candidates:
                try:
                    client = LiveClient(candidate['instance'])
                    client.call('snapshot')
                    reachable.append(client)
                except (OSError, ValueError, TimeoutError):
                    pass
            if len(reachable) != 1:
                raise ValueError('Enable 工程 → 启用 MCP 连接 in OKK STUDIO; '
                                 'if multiple instances are enabled, select an instance explicitly')
            self.url, self.token = reachable[0].url, reachable[0].token
            return
        if not isinstance(instance, str) or not instance or any(c not in '0123456789abcdef-' for c in instance):
            raise ValueError('Invalid instance ID')
        data = json.loads((discovery_directory()/(instance+'.json')).read_text(encoding='utf-8'))
        url = urlsplit(data['url'])
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
                or url.username or url.password or url.path != '/rpc' or url.query or url.fragment):
            raise ValueError('Invalid local bridge URL')
        if not isinstance(data['token'], str) or not data['token']:
            raise ValueError('Invalid bridge credential')
        self.url, self.token = data['url'], data['token']

    def call(self, method, **params):
        request = Request(self.url, data=json.dumps({'method': method, 'params': params},
                          ensure_ascii=False, allow_nan=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json',
                                   'Authorization': 'Bearer '+self.token})
        # Local requests must never inherit a corporate HTTP proxy.
        with build_opener(ProxyHandler({})).open(request, timeout=35) as response:
            value = json.loads(response.read())
        if 'error' in value:
            raise ValueError(value['error'])
        return value['result']


class WorkspaceSession(EditSession):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def get_project(self):
        workspace = self.app.multicam_workspace
        project = deepcopy(workspace.project)
        project.range_start, project.range_end, project.fps = workspace._read_range()
        return project

    def get_snapshot(self):
        workspace = self.app.multicam_workspace
        return snapshot(self.get_project(), mode='live',
                        project_path=str(workspace.project_path or getattr(self.app, 'project_path', '') or ''),
                        busy=bool(workspace.busy or self.app.working), playhead=workspace.current)

    def require_idle(self):
        if self.app.working or self.app.multicam_workspace.busy:
            raise ValueError('OKK STUDIO is busy; retry after the current task finishes')

    def exposure(self, action, **params):
        from exposure_check import workspace_service
        workspace = self.app.multicam_workspace
        service = workspace_service(workspace)
        if action == 'start':
            self.check_revision(params.pop('expected_revision'))
            return service.start(workspace.project, params.get('options'))
        identifier = params.pop('scan_id')
        if action == 'get': return service.get(identifier, **params)
        if action == 'frame': return service.frame(identifier, **params)
        if action == 'cancel': return service.cancel(identifier)
        if action == 'submit':
            return service.submit(identifier, params['reviews'], params['scan_revision'], workspace.project,
                                  model=params.get('model', ''))
        raise ValueError('Unknown exposure operation')

    def publish(self, candidate):
        self.require_idle()
        workspace = self.app.multicam_workspace
        workspace._cancel_grade_drafts()
        workspace.stop_monitors()
        original = self.get_project()
        undo, redo = list(workspace.undo_stack), list(workspace.redo_stack)
        dirty = workspace.dirty
        try:
            workspace.project = original
            workspace._remember()
            workspace.project = candidate
            workspace._restore_timeline_range_fields()
            workspace.selected_program_range = None
            workspace.sync_results.clear()
            workspace._refresh()
        except Exception:
            workspace.project = original
            workspace.undo_stack, workspace.redo_stack = undo, redo
            workspace.dirty = dirty
            workspace._restore_timeline_range_fields()
            try:
                workspace._refresh()
            except Exception:
                pass
            raise
        self.app._project_update_title()

    def history(self, action, expected_revision):
        self.require_idle()
        self.check_revision(expected_revision)
        if action not in {'undo', 'redo'}:
            raise ValueError('action must be undo or redo')
        workspace = self.app.multicam_workspace
        if not getattr(workspace, action+'_stack'):
            raise ValueError(f'Nothing to {action}')
        getattr(workspace, action)()
        self.plans.clear()
        self.app._project_update_title()
        return self.get_snapshot()

    def save_copy(self, path, expected_revision, overwrite=False):
        self.require_idle()
        self.check_revision(expected_revision)
        from project_file import build_project_payload
        from studio_project import APP_VERSION
        self.envelope = build_project_payload(self.app, app_version=APP_VERSION)
        return super().save_copy(path, expected_revision, overwrite)

    def seek(self, time, expected_revision):
        from okk_automation import number
        self.require_idle()
        self.check_revision(expected_revision)
        position = number(time, 'time')
        if not 0 <= position <= self.get_project().duration:
            raise ValueError('Seek position is outside the timeline')
        self.app.multicam_workspace.seek(position)
        return self.get_snapshot()

    def render_status(self):
        workspace = self.app.multicam_workspace
        finishing = workspace.finishing_workspace
        current_revision = revision(self.get_project())
        jobs = [dict(job_id=job.identifier, state=job.state, output=str(job.output),
                     project_revision=revision(job.project), settings=asdict(job.settings),
                     progress=job.progress, detail=job.detail) for job in finishing.jobs]
        try:
            from delivery_panel import make_delivery_settings
            settings, suffix = make_delivery_settings(workspace)
            output = workspace.output_var.get().strip()
            output = str(Path(output).with_suffix(suffix)) if output else ''
            delivery = {'output': output, 'settings': asdict(settings)}
        except (ValueError, TypeError, AttributeError) as error:
            delivery = {'error': str(error)}
        active_id = finishing.active_job.identifier if finishing.active_job is not None else None
        identity = dict(revision=current_revision, delivery=delivery, active_job_id=active_id,
                        running=finishing.running, jobs=[{key: value for key, value in job.items()
                        if key not in {'progress', 'detail'}} for job in jobs])
        queue_revision = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                                   allow_nan=False).encode('utf-8')).hexdigest()
        return dict(revision=current_revision, queue_revision=queue_revision,
                    project_path=str(getattr(self.app, 'project_path', '') or workspace.project_path or ''),
                    working=bool(self.app.working), busy=bool(workspace.busy),
                    running=bool(finishing.running), active_job_id=active_id, jobs=jobs,
                    delivery=delivery, metrics=deepcopy(getattr(workspace, '_export_last_snapshot', {})),
                    close_pending=bool(getattr(self, '_close_pending', False)))

    def _check_queue(self, expected_queue_revision):
        status = self.render_status()
        if not expected_queue_revision or status['queue_revision'] != expected_queue_revision:
            raise ValueError('Render queue or delivery settings changed; read fresh render_status')
        return status

    def render_set_bitrate(self, value, expected_revision, expected_queue_revision):
        self.require_idle()
        self.check_revision(expected_revision)
        self._check_queue(expected_queue_revision)
        from editor_core import normalize_video_bitrate
        normalized = normalize_video_bitrate(value)
        if not normalized:
            raise ValueError('Target bitrate must be a positive video bitrate')
        self.app.multicam_workspace.bitrate_var.set(normalized)
        return self.render_status()

    def render_enqueue_current(self, expected_revision, expected_queue_revision):
        self.require_idle()
        self.check_revision(expected_revision)
        self._check_queue(expected_queue_revision)
        workspace = self.app.multicam_workspace
        finishing = workspace.finishing_workspace
        if not workspace.output_var.get().strip():
            raise ValueError('Set the Deliver output path before adding a render job')
        prepared = workspace.prepare_delivery_export()
        if prepared is None:
            raise ValueError('Delivery settings are incomplete')
        project, settings, output = prepared
        target = os.path.normcase(str(Path(output).resolve()))
        for job in finishing.jobs:
            if (os.path.normcase(str(Path(job.output).resolve())) == target
                    and job.state in {'等待渲染', '正在渲染'}):
                if revision(job.project) == revision(project) and asdict(job.settings) == asdict(settings):
                    return dict(self.render_status(), job_id=job.identifier, created=False)
                raise ValueError('A different pending job already uses this output path')
        from finishing_workspace import RenderJob
        finishing.serial += 1
        job = RenderJob(str(finishing.serial), deepcopy(project), deepcopy(settings), output)
        finishing.jobs.append(job)
        try:
            finishing._add_card(job)
        except Exception:
            finishing.jobs.remove(job)
            raise
        finishing.selected_job.set(job.identifier)
        finishing.queue_status.set(f'{sum(item.state == "等待渲染" for item in finishing.jobs)} 个任务等待渲染')
        workspace.status.set('已添加渲染任务：' + output)
        return dict(self.render_status(), job_id=job.identifier, created=True)

    def render_start_queue(self, expected_revision, expected_queue_revision):
        self.require_idle()
        self.check_revision(expected_revision)
        self._check_queue(expected_queue_revision)
        workspace = self.app.multicam_workspace
        finishing = workspace.finishing_workspace
        waiting = [job for job in finishing.jobs if job.state == '等待渲染']
        if not waiting:
            raise ValueError('No waiting render jobs')
        from hardware_encoders import available_codecs
        supported = available_codecs(self.app.caps)
        for job in waiting:
            job.project.validate()
            if Path(job.output).exists():
                raise ValueError('Output already exists; remote queue start will not overwrite it')
            if job.settings.codec not in supported:
                raise ValueError('Queued encoder is unavailable; wait for hardware detection')
        if not self.app.caps.ffmpeg_path or not self.app.caps.ffprobe_path:
            raise ValueError('FFmpeg / ffprobe is unavailable')
        finishing.start_queue()
        if not finishing.running or finishing.active_job is None:
            raise ValueError('Render did not start; inspect delivery settings')
        return self.render_status()

    def render_cancel(self, expected_revision, job_id):
        self.check_revision(expected_revision)
        finishing = self.app.multicam_workspace.finishing_workspace
        active = finishing.active_job
        if active is None or active.identifier != job_id:
            raise ValueError('Active render job changed; read fresh render_status before cancelling')
        finishing.stop_queue()
        return dict(self.render_status(), cancellation_requested=True)

    def project_save(self, expected_revision):
        # Saving a UI-thread snapshot is safe while the renderer uses its own
        # immutable project copy. Never open a Save As dialog from an RPC.
        self.check_revision(expected_revision)
        path = getattr(self.app, 'project_path', None)
        if not path or Path(path).suffix.lower() != '.rtxproj':
            raise ValueError('Current full project has no save path; use save_copy with an explicit path')
        from project_file import build_project_payload, state_digest, write_project_file
        from studio_project import APP_VERSION
        path = Path(path).resolve()
        payload = build_project_payload(self.app, project_path=str(path), app_version=APP_VERSION)
        backup = ''
        if path.exists():
            backup_path = path.with_name(path.name+'.mcp-'+secrets.token_hex(8)+'.bak')
            with backup_path.open('xb') as stream:
                stream.write(path.read_bytes())
            backup = str(backup_path)
        write_project_file(path, payload)
        workspace = self.app.multicam_workspace
        workspace.dirty = False
        workspace._project_upgrade_blocked_path = None
        self.app._project_saved_digest = state_digest(payload['state'])
        self.app._project_update_title()
        return {'path': str(path), 'backup_path': backup, 'revision': revision(self.get_project())}

    def app_close(self, expected_revision):
        self.require_idle()
        self.check_revision(expected_revision)
        if self.app._project_is_dirty():
            raise ValueError('Save the current project before closing')
        if getattr(self, '_close_pending', False):
            return {'close_scheduled': True}
        self._close_pending = True

        def close_when_unchanged():
            try:
                self.require_idle()
                self.check_revision(expected_revision)
                if self.app._project_is_dirty():
                    return
                self.app._close()
            except Exception as error:
                logger = getattr(self.app, '_log', None)
                if callable(logger):
                    logger('MCP 关闭未执行：' + str(error))
            finally:
                self._close_pending = False

        # Let the authenticated HTTP response leave before closing its server.
        self.app.after(200, close_when_unchanged)
        return {'close_scheduled': True}


@dataclass
class Pending:
    method: str
    params: dict
    deadline: float = field(default_factory=lambda: time.monotonic()+25)
    event: threading.Event = field(default_factory=threading.Event)
    response: dict = field(default_factory=dict)


class LocalBridge:
    def __init__(self, app, directory=None):
        self.app = app
        self.session = WorkspaceSession(app)
        self.pending = queue.Queue(maxsize=32)
        self.token = secrets.token_urlsafe(32)
        self.instance = f'{os.getpid()}-{secrets.token_hex(4)}'
        self.closed = False
        self.after_id = None
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Credentials and project data never enter HTTP logs.

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def reply(self, status, payload):
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_POST(self):
                expected = 'Bearer '+bridge.token
                if (self.path != '/rpc' or self.headers.get('Origin') is not None or
                        not hmac.compare_digest(self.headers.get('Authorization', ''), expected)):
                    self.reply(403, {'error': 'Unauthorized local bridge request'})
                    return
                if self.headers.get('Host') != f'127.0.0.1:{bridge.server.server_port}':
                    self.reply(403, {'error': 'Invalid Host'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 2*1024*1024:
                        raise ValueError('Request must be 1 byte to 2 MiB')
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or not isinstance(payload.get('params', {}), dict):
                        raise ValueError('Invalid request')
                    item = Pending(payload.get('method'), payload.get('params', {}))
                    bridge.pending.put_nowait(item)
                except (ValueError, TypeError, queue.Full) as exc:
                    self.reply(400, {'error': str(exc)})
                    return
                if not item.event.wait(30):
                    self.reply(504, {'error': 'UI did not respond; read a new snapshot before retrying'})
                    return
                self.reply(200, item.response)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        folder = Path(directory) if directory else discovery_directory()
        folder.mkdir(parents=True, exist_ok=True)
        self.discovery_file = folder/(self.instance+'.json')
        try:
            with self.discovery_file.open('x', encoding='utf-8') as stream:
                json.dump({'pid': os.getpid(), 'url': f'http://127.0.0.1:{self.server.server_port}/rpc',
                           'token': self.token}, stream)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                           name='OKK local MCP bridge')
            self.thread.start()
            self.after_id = self.app.after(40, self.drain)
        except Exception:
            self.server.server_close()
            self.discovery_file.unlink(missing_ok=True)
            raise

    def drain(self):
        self.after_id = None
        if self.closed:
            return
        # One request per tick keeps multi-client traffic from starving Tk.
        try:
            item = self.pending.get_nowait()
        except queue.Empty:
            item = None
        if item is not None:
            try:
                if time.monotonic() > item.deadline:
                    raise ValueError('Request expired before execution; no edit was applied')
                methods = {'snapshot': self.session.get_snapshot, 'preview': self.session.preview,
                           'apply': self.session.apply, 'history': self.session.history,
                           'save_copy': self.session.save_copy, 'seek': self.session.seek,
                           'render_status': self.session.render_status,
                           'render_set_bitrate': self.session.render_set_bitrate,
                           'render_enqueue_current': self.session.render_enqueue_current,
                           'render_start_queue': self.session.render_start_queue,
                           'render_cancel': self.session.render_cancel,
                           'project_save': self.session.project_save,
                           'app_close': self.session.app_close}
                for action in ('start', 'get', 'frame', 'cancel', 'submit'):
                    methods['exposure_' + action] = lambda action=action, **params: self.session.exposure(action, **params)
                if item.method not in methods:
                    raise ValueError('Unknown local bridge method')
                item.response = {'result': methods[item.method](**item.params)}
            except Exception as exc:
                item.response = {'error': str(exc)}
            finally:
                item.event.set()
        self.after_id = self.app.after(40, self.drain)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.after_id is not None:
            self.app.after_cancel(self.after_id)
        while True:
            try:
                item = self.pending.get_nowait()
            except queue.Empty:
                break
            item.response = {'error': 'MCP bridge closed; no edit was applied'}
            item.event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.discovery_file.unlink(missing_ok=True)


class McpIntegrationMixin:
    def _toggle_mcp_bridge(self):
        from tkinter import messagebox
        bridge = getattr(self, '_mcp_bridge', None)
        if bridge is not None:
            bridge.close()
            self._mcp_bridge = None
            self._log('MCP 连接已关闭。')
            return
        try:
            self._mcp_bridge = LocalBridge(self)
            self._log(f'MCP 连接已启用；实例 {self._mcp_bridge.instance}。')
            messagebox.showinfo('MCP 连接已启用',
                '本机 AI harness 现在可以读取工程、预览和应用剪辑计划。\n'
                '应用的计划可在时间线撤销；保存工程通过显式工具执行。\n\n'
                f'实例：{self._mcp_bridge.instance}\n'
                '再次点击“启用 / 关闭 MCP 连接”可关闭。', parent=self)
        except Exception as exc:
            self._mcp_bridge = None
            messagebox.showerror('MCP 连接失败', str(exc), parent=self)

    def _close_mcp_bridge(self):
        bridge = getattr(self, '_mcp_bridge', None)
        if bridge is not None:
            bridge.close()
            self._mcp_bridge = None
