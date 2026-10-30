"""Regression coverage for existing camera proxies, selections and video clocks."""
from pathlib import Path
import queue
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import external_proxy_link as matching
from exporter_core import CREATE_NO_WINDOW, find_binary
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_proxy import MulticamProxyMixin
from quick_proxy import _fill_video_duration, _probe_media_cancel


class Variable:
    def __init__(self, value=None): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


class ProxyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='okk_proxy_compat_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, name):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
        return str(path)

    def workspace(self, clips):
        ws = MulticamProxyMixin()
        ws.project = MulticamProject(clips, [Cut(0., clips[0].camera)])
        ws.media_tree = SimpleNamespace(selection=lambda: ())
        ws._timeline_selection = set()
        ws.status = Variable()
        ws.proxy_var = Variable(False)
        ws.app = SimpleNamespace(caps=SimpleNamespace(ffprobe_path='ffprobe'), _log=Mock())
        ws.player = Mock()
        ws.jobs = queue.Queue()
        ws._remember = Mock(); ws._mark_dirty = Mock()
        self.addCleanup(lambda: ws._proxy_link_thread.join(3) if vars(ws).get('_proxy_link_thread') else None)
        self.addCleanup(ws._reset_proxy_link)
        return ws

    def test_common_encoder_names_and_additional_video_extensions(self):
        source = self.file('originals/C0001.MP4')
        for name in ('C0001_PRX.MP4', 'Proxy_C0001.mov', 'C0001.MP4_proxy.m4v',
                     'C0001(1920x1080).mov', 'C0001_代理_1080p.mp4', 'C0001.lrv',
                     '1080p_C0001_Proxy.mov'):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as folder:
                proxy = Path(folder)/name
                proxy.write_bytes(b'proxy')
                self.assertEqual(matching.match_proxy_folder([source], folder), ([(source, str(proxy))], []))

    def test_mirrored_camera_folders_resolve_same_basename_without_guessing(self):
        first = self.file('originals/cameraA/C0001.MP4')
        second = self.file('originals/cameraB/C0001.MP4')
        first_proxy = self.file('converted/cameraA/ProxyMedia/C0001_PRX.mov')
        second_proxy = self.file('converted/cameraB/ProxyMedia/C0001_PRX.mov')
        pairs, issues = matching.match_proxy_folder([first, second], self.root/'converted')
        self.assertEqual(set(pairs), {(first, first_proxy), (second, second_proxy)})
        self.assertEqual(issues, [])

    def test_resolve_proxy_subfolders_relink_duplicate_original_names(self):
        first = self.file('footage/cameraA/C0001.MP4')
        second = self.file('footage/cameraB/C0001.MP4')
        first_proxy = self.file('footage/cameraA/Proxy/C0001.mov')
        second_proxy = self.file('footage/cameraB/Proxy/C0001.mov')
        pairs, issues = matching.match_proxy_folder([first, second], self.root/'footage')
        self.assertEqual(set(pairs), {(first, first_proxy), (second, second_proxy)})
        self.assertEqual(issues, [])

    def test_resolve_extracted_proxy_tree_relinks_without_original_extension(self):
        first = self.file('originals/cameraA/C0001.MP4')
        second = self.file('originals/cameraB/C0001.MP4')
        first_proxy = self.file('ResolveProxies/cameraA/Proxy/C0001.mov')
        second_proxy = self.file('ResolveProxies/cameraB/Proxy/C0001.mov')
        pairs, issues = matching.match_proxy_folder([first, second], self.root/'ResolveProxies')
        self.assertEqual(set(pairs), {(first, first_proxy), (second, second_proxy)})
        self.assertEqual(issues, [])

    def test_flat_folder_still_rejects_ambiguous_camera_names(self):
        first = self.file('originals/cameraA/C0001.MP4')
        second = self.file('originals/cameraB/C0001.MP4')
        self.file('converted/C0001_PRX.mov')
        pairs, issues = matching.match_proxy_folder([first, second], self.root/'converted')
        self.assertEqual(pairs, [])
        self.assertEqual(len(issues), 2)

    def test_similar_folder_names_are_not_fuzzy_matched(self):
        first = self.file('originals/camera1/C0001.MP4')
        second = self.file('originals/camera10/C0001.MP4')
        self.file('converted/camera11/C0001_PRX.mov')
        self.assertEqual(matching.match_proxy_folder([first, second], self.root/'converted')[0], [])

    def test_missing_folder_reports_problem_instead_of_empty_match(self):
        with self.assertRaisesRegex(ValueError, '文件夹'):
            matching.match_proxy_folder([], self.root/'missing')

    def test_selected_camera_and_timeline_single_clip_can_link_renamed_proxy(self):
        source = self.file('source/A.mov')
        proxy = self.file('proxy/any_filename.mov')
        ws = self.workspace([CameraClip('a', 1, source, 0., 10.)])
        ws._start_proxy_link = Mock(return_value=True)
        for selection, timeline in ((('camera_1',), set()), ((), {('clip', 'a')})):
            ws.media_tree.selection = lambda: selection
            ws._timeline_selection = timeline
            with patch('multicam_proxy.filedialog.askopenfilename', return_value=proxy):
                self.assertTrue(ws.link_existing_proxy())
            ws._start_proxy_link.assert_called_with([(source, proxy)])

    def test_multisegment_camera_does_not_bind_one_file_to_first_clip_silently(self):
        source = self.file('source/A.mov')
        other = self.file('source/B.mov')
        ws = self.workspace([CameraClip('a', 1, source, 0., 10.), CameraClip('b', 1, other, 10., 10.)])
        ws.media_tree.selection = lambda: ('camera_1',)
        with patch('multicam_proxy.filedialog.askopenfilename') as dialog:
            self.assertFalse(ws.link_existing_proxy())
            dialog.assert_not_called()
        self.assertIn('多份原片', ws.status.get())

    def test_selected_folder_scope_binds_only_explicit_camera(self):
        source = self.file('source/camera1/A.mov')
        other = self.file('source/camera2/B.mov')
        first_proxy = self.file('converted/A_proxy.mov')
        self.file('converted/B_proxy.mov')
        ws = self.workspace([CameraClip('a', 1, source, 0., 10.), CameraClip('b', 2, other, 0., 10.)])
        ws.media_tree.selection = lambda: ('camera_1',)
        with patch('multicam_proxy.filedialog.askdirectory', return_value=str(self.root/'converted')), \
                patch.object(matching, 'validate_existing_proxy'), patch('proxy_cache.register_proxy'):
            self.assertTrue(ws.link_proxy_folder(selected_only=True))
            ws._proxy_link_thread.join(3)
            while not ws.jobs.empty():
                kind, payload = ws.jobs.get_nowait()
                ws._handle_quick_proxy_job(kind, payload)
        self.assertEqual(ws.project.clips[0].proxy_path, first_proxy)
        self.assertEqual(ws.project.clips[1].proxy_path, '')
        self.assertNotIn('未匹配', ws.status.get())

    def test_unselected_original_cannot_be_used_as_proxy(self):
        first = self.file('camera1/A.mov')
        second = self.file('camera2/A.mov')
        pairs, _ = matching.match_proxy_folder([first], self.root/'camera2', exclude_paths=[second])
        self.assertEqual(pairs, [])

    def test_mkv_duration_tag_and_timebase_do_not_accept_invalid_durations(self):
        payload = {'streams': [
            {'codec_type':'video', 'duration_ts':250, 'time_base':'1/25'},
            {'codec_type':'video', 'duration':'N/A', 'tags':{'DURATION':'00:00:10.007'}, 'start_time':'0.007'},
            {'codec_type':'video', 'duration':'9.5', 'tags':{'DURATION':'00:00:10.000'}},
            {'codec_type':'video', 'tags':{'DURATION':'invalid'}},
        ]}
        _fill_video_duration(payload)
        self.assertEqual(payload['streams'][0]['duration'], 10.)
        self.assertAlmostEqual(payload['streams'][1]['duration'], 10.)
        self.assertEqual(payload['streams'][2]['duration'], '9.5')
        self.assertNotIn('duration', payload['streams'][3])


class RealProxyVideoClockTests(unittest.TestCase):
    def test_resolve_dnxhr_lb_mov_is_accepted_with_original_name_and_different_fps(self):
        root = Path(__file__).resolve().parents[1]
        ffmpeg, ffprobe = find_binary('ffmpeg', root), find_binary('ffprobe', root)
        if not ffmpeg or not ffprobe:
            self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory(prefix='okk_resolve_proxy_') as folder:
            folder = Path(folder)
            source = folder/'cameraA'/'C0001.MP4'
            proxy = folder/'cameraA'/'Proxy'/'C0001.mov'
            proxy.parent.mkdir(parents=True)
            result = subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                'testsrc2=size=640x360:rate=60:duration=2', '-c:v', 'libx264', '-preset', 'ultrafast',
                '-threads', '2', str(source)], capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            result = subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', str(source), '-vf',
                'scale=320:180,fps=30', '-c:v', 'dnxhd', '-profile:v', 'dnxhr_lb', '-pix_fmt', 'yuv422p',
                '-threads', '2', str(proxy)], capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            self.assertEqual(matching.match_proxy_folder([str(source)], folder), ([(str(source), str(proxy))], []))
            linked = matching.validate_existing_proxy(ffprobe, str(source), str(proxy))
            self.assertEqual(linked.video_codec, 'dnxhd')
            self.assertEqual((linked.width, linked.height, linked.fps), (320, 180, 30.))
            self.assertAlmostEqual(linked.duration, 2., places=2)

    def test_mkv_with_longer_audio_tail_links_full_picture_proxy_and_rejects_trim(self):
        root = Path(__file__).resolve().parents[1]
        ffmpeg, ffprobe = find_binary('ffmpeg', root), find_binary('ffprobe', root)
        if not ffmpeg or not ffprobe:
            self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory(prefix='okk_mkv_proxy_') as folder:
            folder = Path(folder)
            source, proxy, trim = folder/'original.mkv', folder/'renamed.mov', folder/'trim.mov'
            command = [ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                       'testsrc2=size=160x90:rate=25:duration=2', '-f', 'lavfi', '-i',
                       'sine=frequency=600:duration=2.8', '-c:v', 'libx264', '-preset', 'ultrafast',
                       '-c:a', 'pcm_s16le', '-threads', '2', str(source)]
            result = subprocess.run(command, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            for destination, duration in ((proxy, 2), (trim, 1)):
                result = subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', str(source), '-an',
                    '-t', str(duration), '-vf', 'fps=30', '-c:v', 'prores_ks', '-threads', '2', str(destination)],
                    capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            original = _probe_media_cancel(ffprobe, str(source))
            self.assertAlmostEqual(original.duration, 2., places=2)
            linked = matching.validate_existing_proxy(ffprobe, str(source), str(proxy))
            self.assertAlmostEqual(linked.duration, 2., places=2)
            with self.assertRaisesRegex(ValueError, '时长'):
                matching.validate_existing_proxy(ffprobe, str(source), str(trim))


if __name__ == '__main__':
    unittest.main()
