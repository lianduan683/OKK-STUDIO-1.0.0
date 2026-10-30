"""Real page routing, layout restoration and immutable render queue tests."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import time
import subprocess
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_delivery_ui as fixture
from creative_core import ColorGrade, replace_curve
from exporter_core import Capabilities, CREATE_NO_WINDOW
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import set_grade_on_range


class FinishingPageTests(unittest.TestCase):
    setUp = fixture.DeliveryTkTests.setUp
    tearDown = fixture.DeliveryTkTests.tearDown
    _create_app = fixture.DeliveryTkTests._create_app
    _destroy_app = fixture.DeliveryTkTests._destroy_app
    _settle = fixture.DeliveryTkTests._settle
    _assert_inside = fixture.DeliveryTkTests._assert_inside

    def seed_project(self):
        ws = self.workspace
        ws.project = MulticamProject(
            clips=[CameraClip('one', 1, 'one.mp4', 0, 6), CameraClip('two', 2, 'two.mp4', 0, 6)],
            cuts=[Cut(0, 1), Cut(3, 2)], fps=30)
        ws.fps_var.set('30')
        ws.output_var.set(str(Path(self.session_directory.name) / 'first.mp4'))
        ws._refresh(reconfigure=False)
        return ws.finishing_workspace

    def test_bottom_pages_share_project_video_surfaces_and_restore_editing_layout(self):
        ws = self.workspace
        finish = self.seed_project()
        self.app._select_page('multicam')
        self._settle()
        original_project = ws.project
        video_frames = [*ws.program_views, *ws.camera_surfaces.values()]
        windows = tuple(frame.winfo_id() for frame in video_frames)
        before = copy.deepcopy(ws.dock_manager.serialize())
        for _ in range(2):
            for page in ('color', 'delivery', 'multicam'):
                self.app.page_buttons[page].invoke()
                self._settle()
                self.assertIs(ws.project, original_project)
                self.assertEqual(tuple(frame.winfo_id() for frame in video_frames), windows)
                self.assertEqual(self.app.active_page, page)
                self._assert_inside(ws.program_viewport)
                if page == 'color':
                    for wheel in ws.color_workspace.wheels.values():
                        self._assert_inside(wheel.canvas)
                        self._assert_inside(wheel.y_scale)
                    self._assert_inside(ws.color_workspace.curve_editor.canvas)
                    # Mapped widgets can still be covered by their newer native
                    # sibling hosts. Check stacking as well as geometry.
                    self.app.lift()
                    self.app.update()
                    for control in (ws.color_workspace.wheels['lift'].canvas,
                                    ws.color_workspace.curve_editor.canvas):
                        hit = self.app.winfo_containing(control.winfo_rootx() + control.winfo_width() // 2,
                                                        control.winfo_rooty() + control.winfo_height() // 2)
                        self.assertIs(hit, control)
                elif page == 'delivery':
                    self._assert_inside(ws.delivery_export_button)
                    self._assert_inside(finish.queue_host)
                    self._assert_inside(ws.timeline)
        after = ws.dock_manager.serialize()
        self.assertEqual(after, before)

    def test_color_entry_with_empty_project_and_return_button(self):
        ws = self.workspace
        self.app._select_page('color')
        self._settle()
        self.assertTrue(ws._color_mode)
        self.assertTrue(ws.color_workspace.curve_editor.winfo_ismapped())
        ws.exit_color_workspace()
        self._settle()
        self.assertEqual(self.app.active_page, 'multicam')
        self.assertFalse(ws._color_mode)

    def test_color_wheels_curves_and_navigation_fit_4k_at_200_percent(self):
        self._destroy_app()
        self._create_app(scale=2)
        self.seed_project()
        self.app._select_page('color')
        self._settle()
        ws = self.workspace
        for wheel in ws.color_workspace.wheels.values():
            self._assert_inside(wheel.canvas)
            self._assert_inside(wheel.y_scale)
        self._assert_inside(ws.color_workspace.curve_editor.canvas)
        for button in self.app.page_buttons.values():
            self._assert_inside(button)

    def test_settings_and_grade_are_frozen_when_added_and_duplicate_outputs_are_rejected(self):
        finish = self.seed_project()
        ws = self.workspace
        set_grade_on_range(ws.project, 0, 3, ColorGrade(exposure=.4))
        ws.start_var.set('00:00:01.000')
        ws.end_var.set('00:00:05.000')
        finish.add_job()
        self.assertEqual(len(finish.jobs), 1)
        job = finish.jobs[0]
        expected_grade = copy.deepcopy(job.project.clip_grades)
        self.app.quality_var.set(31)
        ws.start_var.set('00:00:02.000')
        ws.project.clip_grades.clear()
        self.assertEqual(job.settings.quality, 20)
        self.assertEqual(job.project.range_start, 1)
        self.assertEqual(job.project.range_end, 5)
        self.assertEqual(job.project.clip_grades, expected_grade)
        with mock.patch('finishing_workspace.messagebox.showerror') as error:
            finish.add_job()
        self.assertEqual(len(finish.jobs), 1)
        self.assertIn('相同输出文件', error.call_args.args[1])

    def test_queue_dispatches_snapshots_sequentially_and_keeps_completed_cards(self):
        finish = self.seed_project()
        ws = self.workspace
        finish.add_job()
        ws.output_var.set(str(Path(self.session_directory.name) / 'second.mp4'))
        self.app.quality_var.set(27)
        finish.add_job()
        captures = []

        def render(project, output, _ffmpeg, _ffprobe, settings, *_args, **kwargs):
            captures.append((output, settings.quality))
            kwargs['metrics'](dict(stage='encoding', progress_percent=40, phase='编码中', fps=82))

        with mock.patch('multicam_ui.render_multicam', side_effect=render), \
                mock.patch('multicam_ui.messagebox.showinfo') as info, \
                mock.patch('multicam_ui.messagebox.showerror') as error:
            finish.start_queue()
            deadline = time.monotonic() + 8
            while finish.running and time.monotonic() < deadline:
                self.app.update()
                time.sleep(.02)
            self._settle()
        error.assert_not_called()
        info.assert_not_called()
        self.assertEqual([quality for _, quality in captures], [20, 27])
        self.assertEqual([j.state for j in finish.jobs], ['已完成', '已完成'])
        self.assertTrue(all(j.progress == 100 for j in finish.jobs))
        self.assertFalse(ws.busy)
        self.assertFalse(self.app.working)

    def test_failure_and_cancellation_stop_the_queue_and_leave_later_jobs_waiting(self):
        finish = self.seed_project()
        ws = self.workspace
        for index in range(2):
            ws.output_var.set(str(Path(self.session_directory.name) / f'failure-{index}.mp4'))
            finish.add_job()
        with mock.patch.object(ws, 'export', return_value=True):
            finish.start_queue()
        self.assertIs(finish.active_job, finish.jobs[0])
        finish.complete(error='编码失败')
        self.assertEqual([j.state for j in finish.jobs], ['失败', '等待渲染'])
        self.assertFalse(finish.running)
        finish.selected_job.set(finish.jobs[0].identifier)
        finish.retry_selected()
        with mock.patch.object(ws, 'export', return_value=True):
            finish.start_queue()
        with mock.patch.object(self.app, '_cancel') as cancel:
            finish.stop_queue()
            cancel.assert_called_once()
        finish.complete(error='cancelled', cancelled=True)
        self.assertEqual([j.state for j in finish.jobs], ['已取消', '等待渲染'])

    def test_declining_overwrite_leaves_task_waiting_without_starting_render(self):
        finish = self.seed_project()
        target = Path(self.workspace.output_var.get())
        target.write_bytes(b'keep existing file')
        finish.add_job()
        with mock.patch('multicam_ui.messagebox.askyesno', return_value=False), \
                mock.patch('multicam_ui.render_multicam') as render:
            finish.start_queue()
        render.assert_not_called()
        self.assertFalse(finish.running)
        self.assertEqual(finish.jobs[0].state, '等待渲染')
        self.assertEqual(target.read_bytes(), b'keep existing file')

    def test_saving_layout_from_deliver_saves_the_editing_panels(self):
        finish = self.seed_project()
        original = copy.deepcopy(finish.saved_layout)
        self.assertTrue(self.workspace.save_workspace_layout())
        import json
        saved = json.loads(Path(self.workspace._layout_file()).read_text('utf-8'))
        saved.pop('workspace_chrome')
        self.assertEqual(saved, original)

    def test_actual_cpu_queue_renders_wheels_and_curve_into_output_pixels(self):
        bin_dir = Path(__file__).resolve().parents[1] / 'bin'
        ffmpeg, ffprobe = bin_dir / 'ffmpeg.exe', bin_dir / 'ffprobe.exe'
        if not ffmpeg.is_file() or not ffprobe.is_file():
            self.skipTest('Bundled FFmpeg unavailable')
        folder = Path(self.session_directory.name)
        source = folder / 'gray.mp4'

        def command(args):
            result = subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', *args],
                                    capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            return result.stdout

        command(['-y', '-f', 'lavfi', '-i', 'color=c=gray:s=160x90:r=24:d=0.5',
                 '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an', str(source)])
        ws = self.workspace
        ws.project = MulticamProject(clips=[CameraClip('gray', 1, str(source), 0, .5, has_audio=False)],
                                     cuts=[Cut(0, 1)], fps=24)
        ws.fps_var.set('24')
        self.app.caps = Capabilities(ffmpeg_path=str(ffmpeg), ffprobe_path=str(ffprobe))
        ws.export_mode.set('CPU H.264（兼容模式）')
        ws.delivery_audio_format_var.set('无音频')
        finish = ws.finishing_workspace
        ws.output_var.set(str(folder / 'neutral.mp4'))
        finish.add_job()
        grade = replace_curve(ColorGrade(exposure=.6, gain_y=.15), 'master',
                              ((0., 0.), (.5, .68), (1., 1.)))
        set_grade_on_range(ws.project, 0, .5, grade)
        ws.output_var.set(str(folder / 'graded.mp4'))
        finish.add_job()
        with mock.patch('multicam_ui.messagebox.showerror') as error:
            finish.start_queue()
            deadline = time.monotonic() + 25
            while finish.running and time.monotonic() < deadline:
                self.app.update()
                time.sleep(.02)
        error.assert_not_called()
        self.assertEqual([job.state for job in finish.jobs], ['已完成', '已完成'])
        means = []
        for job in finish.jobs:
            pixels = command(['-i', job.output, '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])
            self.assertEqual(len(pixels), 160 * 90 * 3)
            means.append(sum(pixels) / len(pixels))
        self.assertGreater(means[1], means[0] + 15)


if __name__ == '__main__':
    unittest.main()
