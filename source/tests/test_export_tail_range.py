"""Rounded UI clocks must keep the final graded/framed export frame valid."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from clip_transform import ClipTransform, ClipTransformRange
from creative_core import ColorGrade
from exporter_core import Capabilities, CREATE_NO_WINDOW, ExportSettings
from multicam_core import CameraClip, MulticamProject, render_multicam
from multicam_frame_clock import canonical_frame_rate
from program_edits import ClipGrade, _frame, _range


DURATION = 10771.40004337671


def tail_project(path='camera.mp4', fps=29.97):
    stop = _frame(DURATION, fps) / fps
    return MulticamProject(
        clips=[CameraClip('tail', 1, str(path), DURATION - 1, 1)], fps=fps,
        range_start=DURATION - .2,
        clip_grades=[ClipGrade(DURATION - .3, stop, ColorGrade(exposure=.5))],
        clip_transforms=[ClipTransformRange(DURATION - .3, stop,
                                           ClipTransform('fill', 1.1))])


class TailRangeTests(unittest.TestCase):
    def test_final_grade_and_framing_survive_exact_ntsc_export_clock(self):
        for fps in (29.97, 59.94):
            with self.subTest(fps=fps):
                original = tail_project(fps=fps)
                original.validate()
                frozen = deepcopy(original)
                exported = replace(original, fps=float(canonical_frame_rate(fps)))
                exported.validate()
                self.assertEqual(original, frozen)
                for item in (*exported.clip_grades, *exported.clip_transforms):
                    _, stop, _ = _range(exported, item.start, item.end)
                    self.assertEqual(stop, _frame(exported.duration, exported.fps))

    def test_subframe_tolerance_still_rejects_a_real_frame_overrun(self):
        project = tail_project()
        project.fps = float(canonical_frame_rate(project.fps))
        for start, end in ((-1, 1), (DURATION - 1, DURATION + 1/project.fps),
                           (DURATION - 1, (_frame(DURATION, project.fps) + 1)/project.fps),
                           (DURATION, DURATION), (DURATION, DURATION + .001)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                _range(project, start, end)


class TailRangeMediaTests(unittest.TestCase):
    def test_short_render_at_end_of_three_hour_timeline_keeps_tail_and_effects(self):
        root = Path(__file__).resolve().parents[1]
        ffmpeg, ffprobe = root/'bin/ffmpeg.exe', root/'bin/ffprobe.exe'
        if not ffmpeg.is_file() or not ffprobe.is_file():
            self.skipTest('bundled FFmpeg unavailable')
        with tempfile.TemporaryDirectory(prefix='okk_export_tail_') as folder:
            folder = Path(folder)
            source, output = folder/'source.mp4', folder/'tail.mov'
            subprocess.run([str(ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            'color=c=gray:s=64x36:r=30000/1001:d=1', '-an',
                            '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1', str(source)],
                           check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            original = tail_project(source)
            before = deepcopy(original)

            def runner(command, report):
                result = subprocess.run(command, capture_output=True, timeout=30,
                                        creationflags=CREATE_NO_WINDOW)
                return result.returncode, result.stderr.decode('utf-8', 'replace')

            settings = ExportSettings(codec='libx264', preset='ultrafast', quality=0,
                audio='none', decode_mode='cpu', parallel_workers=1,
                extra_args=['-threads', '1'])
            render_multicam(original, output, str(ffmpeg), str(ffprobe), settings,
                            Capabilities(), runner, work_directory=folder)
            self.assertEqual(original, before)
            probe = subprocess.run([str(ffprobe), '-v', 'error', '-count_frames',
                '-select_streams', 'v:0', '-show_entries',
                'stream=width,height,r_frame_rate,nb_read_frames', '-of', 'json', str(output)],
                capture_output=True, check=True, creationflags=CREATE_NO_WINDOW)
            stream = json.loads(probe.stdout)['streams'][0]
            fps = float(canonical_frame_rate(original.fps))
            self.assertEqual(stream['r_frame_rate'], '30000/1001')
            self.assertEqual(int(stream['nb_read_frames']),
                             _frame(original.duration, fps) - _frame(original.range_start, fps))
            self.assertEqual((stream['width'], stream['height']), (64, 36))
            pixels = subprocess.run([str(ffmpeg), '-v', 'error', '-i', str(output),
                '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'], capture_output=True,
                check=True, creationflags=CREATE_NO_WINDOW).stdout
            self.assertGreater(min(pixels), 140, 'The final frame must retain its local exposure grade')


if __name__ == '__main__':
    unittest.main()
