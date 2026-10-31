"""Real Tk routes for the combined export workspace and settings task logs."""
from __future__ import annotations

import copy
from pathlib import Path
import tkinter as tk
import unittest
from unittest import mock

import test_delivery_ui as fixture
from multicam_core import CameraClip, Cut, MulticamProject
from delivery_panel import make_delivery_settings
from exporter_core import MediaInfo, build_video_filter
from studio_state import collect_okk, restore_okk


class NavigationTkTests(unittest.TestCase):
    setUp = fixture.DeliveryTkTests.setUp
    tearDown = fixture.DeliveryTkTests.tearDown
    _create_app = fixture.DeliveryTkTests._create_app
    _destroy_app = fixture.DeliveryTkTests._destroy_app
    _settle = fixture.DeliveryTkTests._settle
    _assert_inside = fixture.DeliveryTkTests._assert_inside

    def select_section(self, name):
        self.app.export_notebook.select(self.app.export_sections[name])
        self._settle()

    def test_one_export_navigation_entry_contains_both_original_workflows(self):
        app = self.app
        self.assertEqual(set(app.page_frames), {'editor', 'multicam', 'color', 'delivery'})
        self.assertEqual(set(app.page_buttons), set(app.page_frames))
        self.assertEqual(app.page_buttons['delivery'].cget('text'), '转码与导出')
        self.assertEqual([app.export_notebook.tab(tab, 'text') for tab in app.export_notebook.tabs()],
                         ['视频转码', '工程导出'])
        app.page_buttons['delivery'].invoke()
        self._settle()
        self.assertEqual(app.page_title_var.get(), '转码与导出')
        self.assertTrue(self.workspace.delivery_export_button.winfo_viewable())
        self.assertTrue(self.workspace.finishing_workspace.queue_host.winfo_viewable())
        self.select_section('basic')
        self.assertEqual(app.active_page, 'delivery')
        self.assertTrue(app.codec_combo.winfo_viewable())
        self.assertFalse(self.workspace.delivery_export_button.winfo_viewable())
        variables = (app.input_var, app.output_var, app.codec_var, app.preset_var,
                     app.transcode_resolution_var, app.depth_var, app.audio_var, app.decode_var,
                     app.split_var, app.maxrate_var, app.quality_var, app.parallel_var)
        widgets = list(fixture.descendants(app.export_sections['basic']))
        for variable in variables:
            with self.subTest(variable=str(variable)):
                controls = [widget for widget in widgets
                            if any(option in widget.keys() and
                                   str(widget.cget(option)) == str(variable)
                                   for option in ('textvariable', 'variable'))]
                self.assertTrue(controls)
                self.assertTrue(all(widget.winfo_viewable() for widget in controls))

    def test_context_export_dispatches_each_section_and_existing_delivery_shortcut(self):
        app, ws = self.app, self.workspace
        with mock.patch.object(app, '_start_export') as transcode, \
                mock.patch.object(ws.finishing_workspace, 'start_queue') as render:
            app._select_page('basic')
            self._settle()
            self.assertEqual(app.active_page, 'delivery')
            self.assertEqual(app.export_button.cget('text'), '开始转码')
            app.export_button.invoke()
            transcode.assert_called_once()
            render.assert_not_called()
            ws.show_delivery()
            self._settle()
            self.assertEqual(app._export_section(), 'delivery')
            self.assertEqual(app.export_button.cget('text'), '渲染所有')
            app.export_button.invoke()
            render.assert_called_once()
            for section in ('basic', 'delivery'):
                self.select_section(section)
                app._set_working(True)
                app._context_export()
                self.assertEqual(str(app.export_button.cget('state')), 'disabled')
                self.assertEqual(str(app.cancel_button.cget('state')), 'normal')
                app._set_working(False)
            self.assertEqual(transcode.call_count, 1)
            self.assertEqual(render.call_count, 1)
        app._select_page('editor')
        app.export_button.invoke()
        self._settle()
        self.assertEqual((app.active_page, app._export_section()), ('delivery', 'basic'))

    def test_one_click_crop_keeps_project_resolution_and_shows_supported_transcode_choice(self):
        app, ws = self.app, self.workspace
        ws.project = MulticamProject(clips=[CameraClip('one', 1, 'one.mp4', 0, 6)],
                                     cuts=[Cut(0, 1)], fps=30,
                                     project_settings=dict(width=160, height=120, sample_rate=48000, bit_depth='24bit'))
        ws._refresh(reconfigure=False)
        ws.select_program_range(0, 6)
        self.assertTrue(ws.clip_inspector.fill_16_9())
        self.assertEqual(app.resolution_var.get(), '时间线分辨率')
        app._select_page('basic')
        self._settle()
        control = next(widget for widget in fixture.descendants(app.export_sections['basic'])
                       if widget.winfo_class() == 'TCombobox'
                       and str(widget.cget('textvariable')) == str(app.transcode_resolution_var))
        self.assertEqual(control.get(), '保持原分辨率')
        self.assertEqual(control.current(), 0)
        self.assertEqual(app._transcode_settings().resolution, 'original')
        self.assertEqual(make_delivery_settings(ws)[0].resolution, 'timeline')
        app.transcode_resolution_var.set('1080p')
        for _ in range(2):
            ws.show_delivery()
            self._settle()
            self.assertEqual(app.resolution_var.get(), '时间线分辨率')
            self.assertEqual(make_delivery_settings(ws)[0].resolution, 'timeline')
            self.select_section('basic')
            self.assertEqual(control.get(), '1080p')
            self.assertEqual(app._transcode_settings().resolution, '1080p')
        self.assertEqual((ws.project.project_settings['width'], ws.project.project_settings['height']), (160, 90))

    def test_transcode_validation_and_command_use_its_visible_resolution(self):
        app = self.app
        source = Path(self.session_directory.name) / 'input.mp4'
        source.write_bytes(b'fixture path only')
        app.input_var.set(str(source))
        app.output_var.set(str(source.with_name('output.mp4')))
        app.media = MediaInfo(str(source), duration=2, width=160, height=120, fps=30, pixel_format='yuv420p')
        app.resolution_var.set('时间线分辨率')
        app._select_page('basic')
        app.transcode_resolution_var.set('1080p')
        validated = app._validate()
        self.assertIsNotNone(validated)
        _source, _output, settings, media = validated
        self.assertEqual(settings.resolution, '1080p')
        self.assertIn('1080', ' '.join(build_video_filter(settings, media, app.caps)))
        self.assertEqual(app.resolution_var.get(), '时间线分辨率')
        self.assertIn('1080p', app.editor_join_shared_settings_var.get())
        app.transcode_resolution_var.set('时间线分辨率')
        with self.assertRaisesRegex(ValueError, '视频转码输出分辨率'):
            app._transcode_settings()

    def test_session_restores_independent_resolution_choices_without_tab_side_effects(self):
        app, ws = self.app, self.workspace
        app.resolution_var.set('4K')
        app.transcode_resolution_var.set('1080p')
        app._select_page('basic')
        saved = collect_okk(app)
        self.assertEqual(saved['settings']['delivery_transcode_resolution_var'], '1080p')
        app.transcode_resolution_var.set('8K')
        restore_okk(app, saved)
        self.assertEqual(app.transcode_resolution_var.get(), '1080p')
        for page in ('color', 'multicam', 'delivery', 'basic'):
            app._select_page(page)
            self._settle()
            self.assertEqual(app.resolution_var.get(), '4K')
            self.assertEqual(make_delivery_settings(ws)[0].resolution, '4k')
            self.assertEqual(app._transcode_settings().resolution, '1080p')

    def test_switching_sections_keeps_settings_project_queue_and_native_surfaces(self):
        app, ws = self.app, self.workspace
        ws.project = MulticamProject(clips=[CameraClip('one', 1, 'one.mp4', 0, 6)],
                                     cuts=[Cut(0, 1)], fps=30)
        ws.output_var.set(str(Path(self.session_directory.name) / 'queued.mp4'))
        ws._refresh(reconfigure=False)
        app.quality_var.set(27)
        ws.delivery_container_var.set('MKV')
        ws.delivery_audio_format_var.set('FLAC')
        finish = ws.finishing_workspace
        with mock.patch('finishing_workspace.messagebox.showerror') as error:
            finish.add_job()
        error.assert_not_called()
        self.assertEqual(len(finish.jobs), 1)
        job = finish.jobs[0]
        project, content = ws.project, copy.deepcopy(ws.project)
        surfaces = [ws.program_surface, *ws.program_views, *ws.camera_surfaces.values()]
        handles = [surface.winfo_id() for surface in surfaces]
        for _ in range(2):
            self.select_section('basic')
            app._select_page('color')
            self._settle()
            app._select_page('multicam')
            self._settle()
            app.page_buttons['delivery'].invoke()
            self._settle()
            self.assertIs(ws.project, project)
            self.assertEqual(ws.project, content)
            self.assertEqual([surface.winfo_id() for surface in surfaces], handles)
            self.assertIs(finish.jobs[0], job)
            self.assertEqual(job.settings.quality, 27)
            self.assertEqual(app.quality_var.get(), 27)
            self.assertEqual(ws.delivery_audio_format_var.get(), 'FLAC')
            self.assertTrue(ws.delivery_export_button.winfo_viewable())

    def test_settings_logs_keep_startup_live_and_closed_dialog_messages(self):
        app, ws = self.app, self.workspace
        app._log('before settings opens')
        app.settings_button.invoke()
        self._settle()
        window = ws._project_settings_window
        log_button = next(widget for widget in fixture.descendants(window)
                          if isinstance(widget, tk.Button) and widget.cget('text') == '任务日志')
        log_button.invoke()
        self._settle()
        self.assertTrue(app.log.winfo_viewable())
        self.assertIn('应用启动。', app.log.get('1.0', 'end'))
        self.assertIn('before settings opens', app.log.get('1.0', 'end'))
        app.events.put(('log', 'live render message'))
        app._drain_events()
        self.assertIn('live render message', app.log.get('1.0', 'end'))
        window.destroy()
        app._log('after closing settings')
        app._select_page('log')
        self._settle()
        self.assertIsNot(ws._project_settings_window, window)
        self.assertTrue(app.log.winfo_viewable())
        text = app.log.get('1.0', 'end')
        for message in ('before settings opens', 'live render message', 'after closing settings'):
            self.assertEqual(text.count(message), 1)
        app._show_settings('video')
        self._settle()
        log_button = next(widget for widget in fixture.descendants(ws._project_settings_window)
                          if isinstance(widget, tk.Button) and widget.cget('text') == '任务日志')
        self.assertEqual(log_button.cget('bg'), '#18191b')
        app._show_settings('log')
        self._settle()
        self.assertEqual(log_button.cget('bg'), '#34434f')
        self.assertTrue(app.log.winfo_viewable())
        self.assertEqual(app.active_page, 'delivery')

    def test_logs_are_accessible_during_tasks_without_applying_staged_settings(self):
        app, ws = self.app, self.workspace
        original = copy.deepcopy(ws.project)
        app._set_working(True)
        app.settings_button.invoke()
        self._settle()
        self.assertTrue(app.log.winfo_viewable())
        ws._project_settings_vars['width'].set('1080')
        ws._project_settings_apply()
        self.assertEqual(ws.project, original)
        self.assertTrue(ws._project_settings_window.winfo_exists())
        self.assertIn('当前任务仍在运行', ws._project_settings_hint.get())
        app.events.put(('log', 'task still running'))
        app._drain_events()
        self.assertIn('task still running', app.log.get('1.0', 'end'))
        app._set_working(False)

    def test_navigation_and_both_export_sections_fit_standard_and_scaled_windows(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                if scale != 1:
                    self._destroy_app()
                    self._create_app(scale=scale)
                for section in ('basic', 'delivery'):
                    self.select_section(section)
                    self._assert_inside(self.app.export_notebook)
                    self._assert_inside(self.app.export_sections[section])
                    self._assert_inside(self.app.export_button)
                    self._assert_inside(self.app.settings_button)
                    self._assert_inside(self.app.cancel_button)
                    for button in self.app.page_buttons.values():
                        self._assert_inside(button)


if __name__ == '__main__':
    unittest.main()
