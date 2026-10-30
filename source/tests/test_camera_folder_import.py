"""Folder/Explorer inputs must preserve one camera's sequential clip layout."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from camera_import import collect_video_groups, grouped_import
from exporter_core import ExportCancelled, MediaInfo
try:
    from .test_batch_camera_ui import BatchCameraWorkspaceTests
except ImportError:
    from test_batch_camera_ui import BatchCameraWorkspaceTests


class FolderImportCoreTests(unittest.TestCase):
    def test_natural_order_recursive_sidecars_and_distinct_camera_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            for camera in ('A', 'B'):
                root = base / camera / 'DCIM'
                root.mkdir(parents=True)
                for name in ('part10.MP4', 'part2.mp4', 'part1.mov', 'part1.SRT'):
                    (root / name).write_bytes(b'original')
            groups = collect_video_groups([base/'A', base/'B'])
            self.assertEqual(len(groups), 2)
            self.assertEqual([[Path(p).name for p in group] for group in groups],
                             [['part1.mov', 'part2.mp4', 'part10.MP4']]*2)
            separate = collect_video_groups([base/'A'], separate_folder_files=True)
            self.assertEqual([len(group) for group in separate], [1, 1, 1])

    def test_resolve_derived_subfolders_are_not_duplicate_camera_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            original = base/'clip.mp4'
            original.write_bytes(b'original')
            for name in ('Proxy', 'CacheClip', 'ProxyMedia'):
                (base/name).mkdir()
                (base/name/'clip.mov').write_bytes(b'proxy')
            self.assertEqual(collect_video_groups([base]), [[str(original)]])
            # An explicit folder selection remains valid, including a proxy
            # folder the user deliberately chooses as editable source media.
            self.assertEqual(collect_video_groups([base/'Proxy']), [[str(base/'Proxy'/'clip.mov')]])

    def test_cancellation_empty_folder_and_bad_media_publish_no_partial_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            with self.assertRaisesRegex(ValueError, '没有支持的视频'):
                collect_video_groups([base])
            paths = [base/'1.mp4', base/'2.mp4']
            for path in paths:
                path.write_bytes(b'original')
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(ExportCancelled):
                collect_video_groups([base], cancel_event=cancel)
            with patch('camera_import.probe_media', side_effect=[
                    MediaInfo(path=str(paths[0]), duration=2, width=128, height=72),
                    MediaInfo(path=str(paths[1]), duration=0)]):
                with self.assertRaises(ValueError):
                    grouped_import('ffprobe', [base], [1, 2])
            self.assertTrue(all(path.read_bytes() == b'original' for path in paths))


class FolderImportTkTests(unittest.TestCase):
    setUp = BatchCameraWorkspaceTests.setUp
    _create_workspace = BatchCameraWorkspaceTests._create_workspace
    _destroy_workspace = BatchCameraWorkspaceTests._destroy_workspace
    tearDown = BatchCameraWorkspaceTests.tearDown
    wait_for = BatchCameraWorkspaceTests.wait_for
    media = staticmethod(BatchCameraWorkspaceTests.media)
    populate = BatchCameraWorkspaceTests.populate

    def test_folder_default_places_parts_in_one_track_before_any_alignment(self):
        with patch('camera_import_ui.filedialog.askdirectory', return_value=str(self.folder)), \
                patch('camera_import.probe_media', side_effect=self.media):
            self.workspace.add_camera_folder()
            self.wait_for(lambda: not self.workspace.busy)
        self.assertEqual(self.workspace.project.cameras, [1])
        self.assertEqual(self.workspace.project.alignment_reference_camera, 1)
        self.assertEqual([c.start for c in self.workspace.project.clips], [12.*i for i in range(20)])
        self.assertEqual([c.path for c in self.workspace.project.clips], self.paths)
        self.assertEqual(len(self.workspace.undo_stack), 1)
        self.assertEqual(self.workspace.media_tree.selection(), (self.workspace.project.clips[0].id,))
        self.assertFalse(self.root.working)

    def test_explicit_full_master_overrides_auto_camera_reference(self):
        self.populate(count=2)
        self.workspace.project.alignment_reference_camera = 1
        self.workspace.jobs.put(('master', ('full-concert.wav', 3600.)))
        self.workspace._poll()
        self.assertIsNone(self.workspace.project.alignment_reference_camera)
        self.assertEqual(self.workspace.project.master_audio, 'full-concert.wav')

    def test_auto_reference_skips_silent_folder_for_later_audible_camera(self):
        folders = [self.folder/'silent-camera', self.folder/'audible-camera']
        for folder in folders:
            folder.mkdir()
            for name in ('part1.mp4', 'part2.mp4'):
                (folder/name).write_bytes(b'original fixture')
        def media(executable, path):
            info = self.media(executable, path)
            info.has_audio = Path(path).parent == folders[1]
            return info
        with patch('camera_import.probe_media', side_effect=media):
            self.workspace._start_camera_import(folders)
            self.wait_for(lambda: not self.workspace.busy)
        self.assertEqual(self.workspace.project.alignment_reference_camera, 2)
        self.assertEqual(self.workspace.project.master_audio, str(folders[1]/'part1.mp4'))
        self.assertIn('机位 2 整轨作为对齐参考', self.workspace.status.get())
        self.assertEqual([(clip.camera, clip.start) for clip in self.workspace.project.clips],
                         [(1, 0.), (1, 12.), (2, 0.), (2, 12.)])

    def test_all_silent_parts_do_not_claim_an_audio_reference(self):
        def media(executable, path):
            info = self.media(executable, path)
            info.has_audio = False
            return info
        with patch('camera_import.probe_media', side_effect=media):
            self.workspace._start_camera_import(self.paths[:2])
            self.wait_for(lambda: not self.workspace.busy)
        self.assertIsNone(self.workspace.project.alignment_reference_camera)
        self.assertFalse(self.workspace.project.master_audio)
        self.assertNotIn('整轨作为对齐参考', self.workspace.status.get())

    def test_explorer_drop_folder_creates_new_camera_and_preserves_existing(self):
        self.populate(count=2)
        original = copy.deepcopy(self.workspace.project.clips)
        data = self.root.tk.call('list', str(self.folder))
        # TkinterDnD provides Tcl list text; Tcl's format preserves spaces.
        if not isinstance(data, str):
            data = self.root.tk.call('format', '%s', data)
        with patch('camera_import.probe_media', side_effect=self.media):
            self.assertEqual(self.workspace._camera_explorer_drop(SimpleNamespace(data=data)), 'copy')
            self.wait_for(lambda: not self.workspace.busy)
        self.assertEqual(self.workspace.project.clips[:2], original)
        imported = self.workspace.project.clips[2:]
        self.assertEqual({c.camera for c in imported}, {3})
        self.assertEqual([c.start for c in imported], [12.*i for i in range(20)])

    def test_append_parts_to_selected_camera_keeps_its_end_offset(self):
        self.populate(count=2)
        with patch('camera_import.probe_media', side_effect=self.media):
            self.workspace._import(self.paths[2:4], 2)
            self.wait_for(lambda: not self.workspace.busy)
        clips = [c for c in self.workspace.project.clips if c.camera == 2]
        self.assertEqual([c.start for c in clips], [0., 12., 24.])
        self.assertEqual(self.workspace.project.cameras, [1, 2])


if __name__ == '__main__':
    unittest.main()
