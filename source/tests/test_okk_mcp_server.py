"""Official MCP client round trips over both transports + real FFmpeg export."""
import asyncio
import importlib.util
import json
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from multicam_core import CameraClip, Cut, MulticamProject

HAS_MCP = importlib.util.find_spec('mcp') is not None
if HAS_MCP:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client


def payload(result):
    if result.is_error:
        raise AssertionError(str(result.content))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(c.text for c in result.content if c.type == 'text'))


@unittest.skipUnless(HAS_MCP, 'optional MCP dependencies not installed')
class McpProtocolTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='okk_mcp_protocol_')
        cls.folder = Path(cls.temp.name)
        cls.media = cls.folder/'合成素材.mp4'
        cls.ffmpeg = SOURCE/'bin'/'ffmpeg.exe'
        if not cls.ffmpeg.is_file():
            cls.temp.cleanup()
            raise unittest.SkipTest('bundled FFmpeg required')
        result = subprocess.run([str(cls.ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc2=size=96x54:rate=30:duration=1.2', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=1.2', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-c:a', 'aac', '-shortest', str(cls.media)], capture_output=True, timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', 'replace'))
        project = MulticamProject(clips=[CameraClip('a', 1, str(cls.media), 0., 1.2),
                                         CameraClip('b', 2, str(cls.media), 0., 1.2)],
                                  cuts=[Cut(0, 1)], fps=30.)
        cls.project_path = cls.folder/'工程.multicam.json'
        project.save(cls.project_path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    async def test_stdio_edit_save_image_resources_and_real_export(self):
        params = StdioServerParameters(command=sys.executable,
            args=[str(SOURCE/'okk_mcp_server.py'), '--project', str(self.project_path)])
        original = self.project_path.read_bytes()
        async with stdio_client(params) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                self.assertTrue({'get_project', 'get_usage_report', 'preview_edits', 'apply_edits',
                                 'get_source_frame', 'export_video', 'cancel_export'} <= names)
                self.assertEqual(len(names), 23)
                state = payload(await session.call_tool('get_project'))
                self.assertIsNotNone((await session.call_tool('get_project')).structured_content)
                initial = state['revision']
                stats = payload(await session.call_tool('get_usage_report'))
                self.assertEqual(stats['usage']['items'][0]['used_frames'], 36)
                resources = await session.list_resources()
                self.assertIn('okk://capabilities', [str(r.uri) for r in resources.resources])
                prompts = await session.list_prompts()
                self.assertIn('edit_assistant', [p.name for p in prompts.prompts])
                image = await session.call_tool('get_source_frame', {'time': .1})
                self.assertFalse(image.is_error, str(image.content))
                self.assertIn('image', [c.type for c in image.content])
                plan = payload(await session.call_tool('preview_edits', {'expected_revision': initial,
                    'operations': [{'op': 'set_camera', 'start': .4, 'end': .8, 'camera': 2},
                                   {'op': 'split_program', 'time': .2}]}))
                self.assertEqual(payload(await session.call_tool('get_project'))['revision'], initial)
                applied = payload(await session.call_tool('apply_edits',
                    {'plan_id': plan['plan_id'], 'expected_revision': initial}))
                fresh = applied['revision']
                stale = await session.call_tool('preview_edits', {'expected_revision': initial,
                    'operations': [{'op': 'add_marker', 'time': .1}]})
                self.assertTrue(stale.is_error)
                self.assertIn('Project changed', str(stale.content))
                target = self.folder/'AI 剪辑副本.multicam.json'
                payload(await session.call_tool('save_project_copy', {'path': str(target), 'expected_revision': fresh}))
                self.assertEqual(MulticamProject.load(target).camera_at(.5), 2)
                exported = self.folder/'AI 剪辑导出.mp4'
                job = payload(await session.call_tool('export_video', {'output': str(exported), 'expected_revision': fresh}))
                deadline = time.monotonic()+30
                while job['status'] in {'queued', 'running'} and time.monotonic() < deadline:
                    await asyncio.sleep(.1)
                    job = payload(await session.call_tool('get_export_job', {'job_id': job['job_id']}))
                self.assertEqual(job['status'], 'completed', str(job))
                self.assertGreater(exported.stat().st_size, 1000)
                from exporter_core import probe_media
                media = probe_media(str(SOURCE/'bin'/'ffprobe.exe'), str(exported))
                self.assertAlmostEqual(media.duration, 1.2, delta=.08)
                undone = payload(await session.call_tool('undo_edit', {'expected_revision': fresh}))
                self.assertEqual(undone['revision'], initial)
        self.assertEqual(self.project_path.read_bytes(), original)

    async def test_export_cancel_and_failure_are_queryable(self):
        from okk_automation import revision
        from okk_mcp_server import Backend
        backend = Backend()
        backend.file.open_project(self.project_path, revision(backend.file.project))
        try:
            destination = self.folder/'取消导出.mp4'
            job = backend.export(str(destination), revision(backend.file.project))
            backend.job(job['job_id'], cancel=True)
            deadline = time.monotonic()+10
            while backend.job(job['job_id'])['status'] in {'queued', 'running'} and time.monotonic() < deadline:
                await asyncio.sleep(.05)
            self.assertEqual(backend.job(job['job_id'])['status'], 'cancelled')
            self.assertFalse(destination.exists())
            concurrent = self.folder/'other-process-output.mp4'
            racing = backend.export(str(concurrent), revision(backend.file.project))
            concurrent.write_bytes(b'created by another process after export was requested')
            while backend.job(racing['job_id'])['status'] in {'queued', 'running'} and time.monotonic() < deadline:
                await asyncio.sleep(.05)
            self.assertEqual(backend.job(racing['job_id'])['status'], 'failed')
            self.assertEqual(concurrent.read_bytes(), b'created by another process after export was requested')
            backend.file.project.clips[0].path = str(self.folder/'missing-source.mp4')
            failed = backend.export(str(self.folder/'failed.mp4'), revision(backend.file.project))
            while backend.job(failed['job_id'])['status'] in {'queued', 'running'} and time.monotonic() < deadline:
                await asyncio.sleep(.05)
            self.assertEqual(backend.job(failed['job_id'])['status'], 'failed')
            self.assertIn('原始素材不存在', backend.job(failed['job_id'])['message'])
        finally:
            backend.close()

    async def test_streamable_http_with_official_client(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        log_path = self.folder/'http-test.log'
        with log_path.open('w', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, str(SOURCE/'okk_mcp_server.py'),
                '--transport', 'streamable-http', '--port', str(port), '--project', str(self.project_path)],
                stdout=subprocess.DEVNULL, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                ready = False
                for _ in range(100):
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1):
                            ready = True
                            break
                    except OSError:
                        if process.poll() is not None:
                            break
                        await asyncio.sleep(.05)
                self.assertTrue(ready, log_path.read_text(encoding='utf-8'))
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp') as streams:
                    async with ClientSession(*streams, read_timeout_seconds=10) as session:
                        await session.initialize()
                        report = payload(await session.call_tool('get_usage_report', {'group_by': 'cameras'}))
                        self.assertEqual(report['duration_frames'], 36)
                        self.assertEqual(report['usage']['total'], 2)
            finally:
                process.terminate()
                process.wait(timeout=10)

    async def test_legacy_harness_protocol_negotiation(self):
        process = subprocess.Popen([sys.executable, str(SOURCE/'okk_mcp_server.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        responses = queue.Queue()
        def reader():
            for line in process.stdout:
                responses.put(json.loads(line))
        threading.Thread(target=reader, daemon=True).start()
        try:
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-03-26', 'capabilities': {},
                'clientInfo': {'name': 'legacy-test-harness', 'version': '1.0'}}}
            process.stdin.write(json.dumps(request)+'\n')
            process.stdin.flush()
            response = await asyncio.to_thread(responses.get, True, 10)
            self.assertEqual(response['result']['protocolVersion'], '2025-03-26')
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'})+'\n')
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})+'\n')
            process.stdin.flush()
            response = await asyncio.to_thread(responses.get, True, 10)
            self.assertEqual(response['id'], 2)
            self.assertTrue(response['result']['tools'])
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
