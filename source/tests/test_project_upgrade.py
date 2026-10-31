"""Backed-up, atomic and repeatable upgrades through the actual open paths."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from multicam_core import MulticamProject
from project_file import build_project_payload, read_project_file
from project_upgrade import ProjectUpgradeError, write_project_upgrade
from . import test_timeline_drag_merge as fixture
from . import test_studio_session as session_fixture


class ProjectUpgradeWriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / 'original.multicam.json'
        self.original = b'\xef\xbb\xbf' + json.dumps(MulticamProject().to_payload(self.folder)).encode()
        self.path.write_bytes(self.original)
        project = MulticamProject(timeline_view=dict(position=0., zoom=1., x_fraction=0., y_fraction=0.))
        self.upgraded = project.to_payload(self.folder)

    def test_success_keeps_exact_original_bytes_and_writes_the_current_format(self):
        backup = write_project_upgrade(self.path, self.original, self.upgraded)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(MulticamProject.load(self.path).timeline_view['zoom'], 1.)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['version'], 19)

    def test_existing_unrelated_backup_is_preserved(self):
        first = self.path.with_name(self.path.name + '.pre-upgrade.bak')
        first.write_bytes(b'existing user backup')
        backup = write_project_upgrade(self.path, self.original, self.upgraded)
        self.assertNotEqual(backup, first)
        self.assertEqual(first.read_bytes(), b'existing user backup')
        self.assertEqual(backup.read_bytes(), self.original)

    def test_atomic_replace_failure_retains_original_and_retry_reuses_the_backup(self):
        with patch('project_file.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(OSError):
                write_project_upgrade(self.path, self.original, self.upgraded)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(len(list(self.folder.glob('*.bak'))), 1)
        self.assertEqual(list(self.folder.glob('*.tmp')), [])
        write_project_upgrade(self.path, self.original, self.upgraded)
        self.assertEqual(len(list(self.folder.glob('*.bak'))), 1)

    def test_backup_failure_never_writes_the_source(self):
        with patch('project_upgrade._backup_original', side_effect=PermissionError('backup denied')):
            with self.assertRaises(OSError):
                write_project_upgrade(self.path, self.original, self.upgraded)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_externally_changed_source_is_not_replaced(self):
        self.path.write_bytes(b'changed by another editor')
        with self.assertRaises(ProjectUpgradeError):
            write_project_upgrade(self.path, self.original, self.upgraded)
        self.assertEqual(self.path.read_bytes(), b'changed by another editor')
        self.assertEqual(list(self.folder.glob('*.bak')), [])

    def test_partial_backup_failure_removes_only_the_incomplete_backup(self):
        with patch('project_upgrade.os.fsync', side_effect=OSError('backup write failed')):
            with self.assertRaises(OSError):
                write_project_upgrade(self.path, self.original, self.upgraded)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.folder.glob('*.bak')), [])

    def test_invalid_destination_payload_is_rejected_before_backup_or_write(self):
        self.upgraded['fps'] = 0
        with self.assertRaises(ValueError):
            write_project_upgrade(self.path, self.original, self.upgraded)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.folder.glob('*.bak')), [])


class StandaloneProjectUpgradeTests(unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.warning_patch = patch('multicam_ui.messagebox.showwarning')
        self.warning = self.warning_patch.start()

    def tearDown(self):
        fixture.TimelineDropTests.tearDown(self)
        self.warning_patch.stop()
        self.temporary.cleanup()

    def old_project(self, version=18):
        path = self.folder / f'old-{version}.multicam.json'
        project = deepcopy(self.workspace.project)
        for clip in project.clips:
            clip.path = str(Path(clip.path).resolve())
        payload = project.to_payload(self.folder)
        payload['version'] = version
        original = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
        path.write_bytes(original)
        return path, original, project

    def test_open_upgrades_in_place_and_preserves_media_and_edits(self):
        path, original, project = self.old_project()
        self.workspace.load_project(path)
        upgraded = MulticamProject.load(path)
        self.assertEqual(upgraded, project)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['version'], 19)
        self.assertEqual(next(self.folder.glob('*.bak')).read_bytes(), original)
        self.assertEqual(self.workspace.project_path, path)
        self.assertIsNone(self.workspace._project_upgrade_blocked_path)
        self.assertIn('自动升级', self.workspace.status.get())
        self.assertEqual(self.errors, [])

    def test_older_version8_upgrades_and_reopening_current_does_not_repeat_it(self):
        path, original, _ = self.old_project(8)
        self.workspace.load_project(path)
        saved = path.read_bytes()
        modified = path.stat().st_mtime_ns
        self.workspace.load_project(path)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertEqual(len(list(self.folder.glob('*.bak'))), 1)
        self.assertEqual(next(self.folder.glob('*.bak')).read_bytes(), original)

    def test_failed_upgrade_opens_for_viewing_and_blocks_later_view_writes(self):
        path, original, project = self.old_project()
        with patch('project_upgrade.write_project_file', side_effect=PermissionError('locked')):
            self.workspace.load_project(path)
        self.assertEqual(self.workspace.project, project)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.workspace._project_upgrade_blocked_path, str(path.resolve()))
        self.workspace.current, self.workspace.zoom = 5., 8.
        self.workspace._persist_timeline_view()
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(self.workspace.save())
        self.assertIsNone(self.workspace._project_upgrade_blocked_path)
        self.assertEqual(MulticamProject.load(path).timeline_view['zoom'], 8.)

    def test_bad_and_newer_projects_are_not_modified(self):
        current = self.workspace.project
        for text in ('{broken', '{"format":"okk-multicam","version":999}'):
            path = self.folder / 'invalid.multicam.json'
            path.write_text(text, encoding='utf-8')
            self.workspace.load_project(path)
            self.assertIs(self.workspace.project, current)
            self.assertEqual(path.read_text(encoding='utf-8'), text)
        self.assertEqual(list(self.folder.glob('*.bak')), [])


class SessionProjectUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session_fixture.StudioSessionTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        session_fixture.StudioSessionTests.tearDownClass.__func__(cls)

    setUp = session_fixture.StudioSessionTests.setUp
    tearDown = session_fixture.StudioSessionTests.tearDown
    populate = session_fixture.StudioSessionTests.populate

    def old_project(self):
        self.populate()
        path = self.directory / 'legacy.rtxproj'
        data = build_project_payload(self.app, project_path=str(path), app_version='1.0.0')
        data['schema'] = 1
        data['custom_metadata'] = {'preserve': ['original', 7]}
        data['state']['custom_workspace'] = {'notes': 'preserve'}
        project = data['state']['okk_multicam']['project']
        project.pop('timeline_view', None)
        project['version'] = 18
        original = b'\xef\xbb\xbf' + json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        path.write_bytes(original)
        return path, data, original

    def test_full_project_open_upgrades_schema_and_preserves_all_other_sections(self):
        path, data, original = self.old_project()
        self.app._project_open(path)
        saved = read_project_file(path)
        self.assertEqual(saved['schema'], 2)
        self.assertEqual(saved['state']['okk_multicam']['project']['version'], 19)
        self.assertEqual(saved['custom_metadata'], data['custom_metadata'])
        for name, section in data['state'].items():
            if name != 'okk_multicam':
                self.assertEqual(saved['state'][name], section)
        old_model = MulticamProject.from_payload(data['state']['okk_multicam']['project'],
                                                data['state']['okk_multicam']['base_directory'])
        new_model = MulticamProject.from_payload(saved['state']['okk_multicam']['project'],
                                                saved['state']['okk_multicam']['base_directory'])
        self.assertEqual(new_model, old_model)
        self.assertEqual(next(self.directory.glob('*.bak')).read_bytes(), original)
        self.assertFalse(self.app._project_is_dirty())
        self.assertEqual(self.errors, [])

    def test_current_project_reopen_does_not_write_or_create_more_backups(self):
        path, _, _ = self.old_project()
        self.app._project_open(path)
        saved, modified = path.read_bytes(), path.stat().st_mtime_ns
        self.app._project_open(path)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertEqual(len(list(self.directory.glob('*.bak'))), 1)

    def test_failed_upgrade_cannot_be_overwritten_by_autosave_or_view_persistence(self):
        path, _, original = self.old_project()
        with patch('project_upgrade.write_project_file', side_effect=PermissionError('locked')):
            self.app._project_open(path)
        ws = self.app.multicam_workspace
        self.assertEqual(ws._project_upgrade_blocked_path, str(path.resolve()))
        self.assertEqual(path.read_bytes(), original)
        ws.current, ws.zoom = 3., 4.
        self.app._write_project_autosave(save_current=True)
        ws._persist_timeline_view()
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(self.app._project_save())
        self.assertIsNone(ws._project_upgrade_blocked_path)
        self.assertEqual(read_project_file(path)['state']['okk_multicam']['project']['version'], 19)

    def test_legacy_companion_sources_convert_to_embedded_current_edit(self):
        path = self.directory / 'companion.rtxproj'
        data = dict(format='rtx-studio-project', schema=1, state={
            'multicam': {'sources': [{'path': str(self.media), 'offset': .5}]}})
        path.write_text(json.dumps(data), encoding='utf-8')
        original = path.read_bytes()
        self.app._project_open(path)
        saved = read_project_file(path)
        self.assertEqual(saved['state']['multicam'], data['state']['multicam'])
        self.assertEqual(saved['state']['okk_multicam']['project']['version'], 19)
        self.assertEqual(len(self.app.multicam_workspace.project.clips), 1)
        self.assertEqual(next(self.directory.glob('*.bak')).read_bytes(), original)
        self.assertEqual(self.errors, [])

    def test_incomplete_legacy_media_conversion_keeps_the_source_file(self):
        path = self.directory / 'offline.rtxproj'
        data = dict(format='rtx-studio-project', schema=1, state={
            'multicam': {'sources': [{'path': str(self.directory / 'missing.mp4'), 'offset': 0}]}})
        original = json.dumps(data).encode()
        path.write_bytes(original)
        self.app._project_open(path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob('*.bak')), [])
        self.assertEqual(self.app.multicam_workspace._project_upgrade_blocked_path, str(path.resolve()))

    def test_null_legacy_multicam_section_can_upgrade_as_an_empty_edit(self):
        path = self.directory / 'empty.rtxproj'
        path.write_text(json.dumps(dict(format='rtx-studio-project', schema=1,
                                       state={'okk_multicam': None})), encoding='utf-8')
        self.app._project_open(path)
        self.assertEqual(read_project_file(path)['state']['okk_multicam']['project']['version'], 19)
        self.assertEqual(self.errors, [])

    def test_newer_full_project_is_not_downgraded_or_loaded(self):
        path = self.directory / 'future.rtxproj'
        original = json.dumps(dict(format='rtx-studio-project', schema=999, state={})).encode()
        path.write_bytes(original)
        current = self.app.multicam_workspace.project
        self.app._project_open(path)
        self.assertIs(self.app.multicam_workspace.project, current)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob('*.bak')), [])


if __name__ == '__main__':
    unittest.main()
