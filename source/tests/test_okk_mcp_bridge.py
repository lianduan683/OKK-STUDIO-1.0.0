"""Real Tk workspace + real authenticated HTTP bridge, no user input/media."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okk_automation import revision
from okk_mcp_bridge import LiveClient, LocalBridge, Pending


class LiveBridgeTests(unittest.TestCase):
    def setUp(self):
        from tests.test_program_edit_ui import ProgramEditWorkspaceTests
        name = unittest.defaultTestLoader.getTestCaseNames(ProgramEditWorkspaceTests)[0]
        self.fixture = ProgramEditWorkspaceTests(name)
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.root
        self.workspace = self.fixture.workspace
        self.app.multicam_workspace = self.workspace
        self.app.project_path = None
        self.app._project_update_title = Mock()
        self.temp = tempfile.TemporaryDirectory(prefix='okk_bridge_')
        self.addCleanup(self.temp.cleanup)
        self.patch = patch('okk_mcp_bridge.discovery_directory', return_value=Path(self.temp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.bridge = LocalBridge(self.app)
        self.addCleanup(self.bridge.close)
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown)

    def call(self, method, **params):
        future = self.executor.submit(lambda: LiveClient(self.bridge.instance).call(method, **params))
        deadline = time.monotonic()+10
        while not future.done() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.005)
        return future.result(timeout=1)

    def test_live_preview_apply_refresh_and_native_undo(self):
        initial = self.call('snapshot')
        before = len(self.workspace.undo_stack)
        plan = self.call('preview', operations=[{'op': 'split_program', 'time': 3.},
                                              {'op': 'set_camera', 'start': 3., 'end': 6., 'camera': 2}],
                         expected_revision=initial['revision'])
        self.assertEqual(self.workspace.project.edit_points, [])
        applied = self.call('apply', plan_id=plan['plan_id'], expected_revision=initial['revision'])
        self.assertEqual(self.workspace.project.camera_at(4.), 2)
        self.assertEqual(len(self.workspace.undo_stack), before+1)
        self.assertTrue(self.workspace.dirty)
        self.assertEqual(self.app._project_update_title.call_count, 1)
        undone = self.call('history', action='undo', expected_revision=applied['revision'])
        self.assertEqual(undone['revision'], initial['revision'])
        self.assertEqual(self.fixture.callback_errors, [])

    def test_live_stale_revision_and_busy_rejected(self):
        initial = self.call('snapshot')
        plan = self.call('preview', operations=[{'op': 'split_program', 'time': 3.}],
                         expected_revision=initial['revision'])
        self.workspace.project.timeline_markers = [1.]
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.call('apply', plan_id=plan['plan_id'], expected_revision=initial['revision'])
        fresh = self.call('snapshot')
        plan = self.call('preview', operations=[{'op': 'split_program', 'time': 3.}],
                         expected_revision=fresh['revision'])
        self.workspace.busy = True
        try:
            with self.assertRaisesRegex(ValueError, 'busy'):
                self.call('apply', plan_id=plan['plan_id'], expected_revision=fresh['revision'])
        finally:
            self.workspace.busy = False
        self.assertEqual(self.workspace.project.edit_points, [])

    def test_refresh_failure_rolls_back_the_edit_and_history(self):
        initial = self.call('snapshot')
        plan = self.call('preview', operations=[{'op': 'split_program', 'time': 3.}],
                         expected_revision=initial['revision'])
        before = len(self.workspace.undo_stack)
        original_refresh = self.workspace._refresh
        fail_once = Mock(side_effect=[ValueError('simulated UI failure'), None])
        with patch.object(self.workspace, '_refresh', side_effect=fail_once):
            with self.assertRaisesRegex(ValueError, 'simulated UI failure'):
                self.call('apply', plan_id=plan['plan_id'], expected_revision=initial['revision'])
        self.assertEqual(revision(self.workspace.project), initial['revision'])
        self.assertEqual(len(self.workspace.undo_stack), before)
        original_refresh()

    def test_live_save_copy_is_readable_complete_project(self):
        initial = self.call('snapshot')
        path = Path(self.temp.name)/'完整副本.rtxproj'
        result = self.call('save_copy', path=str(path), expected_revision=initial['revision'])
        from project_file import read_project_file
        saved = read_project_file(result['path'])
        self.assertEqual(saved['state']['okk_multicam']['project']['clips'][0]['id'], 'a')
        self.assertEqual(self.workspace.project_path, '')

    @unittest.skipUnless(importlib.util.find_spec('mcp') is not None, 'optional MCP dependencies not installed')
    def test_official_mcp_client_through_stdio_to_real_tk_editor(self):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        appdata = Path(self.temp.name)/'appdata'
        discovery = appdata/'OKK STUDIO'/'mcp'
        discovery.mkdir(parents=True)
        (discovery/self.bridge.discovery_file.name).write_bytes(self.bridge.discovery_file.read_bytes())
        def data(result):
            self.assertFalse(result.is_error, str(result.content))
            if result.structured_content is not None:
                return result.structured_content
            return json.loads(next(c.text for c in result.content if c.type == 'text'))
        async def interact():
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).resolve().parents[1]/'okk_mcp_server.py')],
                env={'LOCALAPPDATA': str(appdata)})
            async with stdio_client(params) as streams:
                async with ClientSession(*streams, read_timeout_seconds=10) as session:
                    await session.initialize()
                    blank = data(await session.call_tool('get_project'))
                    live = data(await session.call_tool('connect_live', {
                        'expected_revision': blank['revision'], 'instance': self.bridge.instance}))
                    plan = data(await session.call_tool('preview_edits', {'expected_revision': live['revision'],
                        'operations': [{'op': 'split_program', 'time': 5.}]}))
                    applied = data(await session.call_tool('apply_edits', {
                        'plan_id': plan['plan_id'], 'expected_revision': live['revision']}))
                    return applied
        future = self.executor.submit(lambda: asyncio.run(interact()))
        deadline = time.monotonic()+20
        while not future.done() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.005)
        result = future.result(timeout=1)
        self.assertEqual(result['mode'], 'live')
        self.assertEqual(self.workspace.project.edit_points, [5.])

    def test_auth_origin_and_unknown_method(self):
        url = f'http://127.0.0.1:{self.bridge.server.server_port}/rpc'
        for headers in ({}, {'Authorization': 'Bearer '+self.bridge.token, 'Origin': 'https://example.com'}):
            request = Request(url, data=b'{"method":"snapshot"}', headers=headers)
            with self.assertRaises(HTTPError) as error:
                build_opener(ProxyHandler({})).open(request, timeout=2)
            self.assertEqual(error.exception.code, 403)
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.call('execute_python', script='not permitted')

    def test_expired_request_never_mutates_and_discovery_removed(self):
        before = revision(self.workspace.project)
        pending = Pending('apply', {'plan_id': 'anything', 'expected_revision': before},
                          deadline=time.monotonic()-1)
        self.bridge.pending.put(pending)
        self.bridge.drain()
        self.assertIn('expired', pending.response['error'])
        self.assertEqual(revision(self.workspace.project), before)
        self.assertTrue(self.bridge.discovery_file.is_file())
        self.bridge.close()
        self.assertFalse(self.bridge.discovery_file.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
