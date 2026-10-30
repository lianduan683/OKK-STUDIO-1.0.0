"""Official MCP client: scan -> actual image -> harness review -> stale guard."""
import asyncio
import json
from pathlib import Path
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import test_okk_mcp_server as fixture


@unittest.skipUnless(fixture.HAS_MCP, 'optional MCP runtime unavailable')
class ExposureMCPTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls): fixture.McpProtocolTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls): fixture.McpProtocolTests.tearDownClass.__func__(cls)

    async def test_stdio_image_review_and_revision_guard(self):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        payload = fixture.payload
        params = StdioServerParameters(command=sys.executable,
            args=[str(SOURCE / 'okk_mcp_server.py'), '--project', str(self.project_path)])
        original = self.project_path.read_bytes()
        async with stdio_client(params) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as client:
                await client.initialize()
                before = payload(await client.call_tool('get_project'))
                tools = {t.name for t in (await client.list_tools()).tools}
                self.assertTrue({'start_exposure_scan', 'get_exposure_scan', 'get_exposure_frame',
                                 'submit_exposure_reviews', 'cancel_exposure_scan'} <= tools)
                started = payload(await client.call_tool('start_exposure_scan',
                    {'expected_revision': before['revision'], 'scope': 'cameras', 'interval': .5, 'max_frames': 6}))
                for _ in range(150):
                    report = payload(await client.call_tool('get_exposure_scan', {'scan_id': started['scan_id']}))
                    if report['status'] != 'running': break
                    await asyncio.sleep(.03)
                self.assertEqual(report['status'], 'complete', report)
                self.assertEqual(report['error_count'], 0, report)
                self.assertEqual(len(report['frames']), 6)
                frame_id = report['frames'][0]['frame_id']
                picture = await client.call_tool('get_exposure_frame', {'scan_id': report['scan_id'], 'frame_id': frame_id})
                self.assertFalse(picture.is_error)
                self.assertIn('image', [item.type for item in picture.content])
                judgment = dict(classification='uncertain', confidence=.65, reason='测试工具链；拍摄意图需人工确认。',
                                intentional_lighting=False, regions=['主体区域'])
                submitted = payload(await client.call_tool('submit_exposure_reviews', {'scan_id': report['scan_id'],
                    'scan_revision': report['scan_revision'], 'reviews': [{'frame_id': frame_id, 'result': judgment}], 'model': 'fixture-harness'}))
                self.assertEqual(submitted['frames'][0]['review']['source'], 'harness')
                self.assertEqual(payload(await client.call_tool('get_project'))['revision'], before['revision'])
                plan = payload(await client.call_tool('preview_edits', {'expected_revision': before['revision'],
                    'operations': [{'op': 'add_marker', 'time': .3}]}))
                payload(await client.call_tool('apply_edits', {'plan_id': plan['plan_id'], 'expected_revision': before['revision']}))
                stale = await client.call_tool('submit_exposure_reviews', {'scan_id': report['scan_id'],
                    'scan_revision': report['scan_revision'], 'reviews': [{'frame_id': frame_id, 'result': judgment}]})
                self.assertTrue(stale.is_error)
        self.assertEqual(self.project_path.read_bytes(), original)


if __name__ == '__main__': unittest.main()
