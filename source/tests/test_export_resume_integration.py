"""Real cancelled export resumes finalized chunks and reproduces full output."""
from pathlib import Path
import json
import subprocess
import tempfile
import threading
import unittest

from exporter_core import Capabilities, ExportCancelled, ExportSettings, CREATE_NO_WINDOW
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam


class ResumeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='okk-resume-integration-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        app = Path(__file__).resolve().parents[1]
        self.ffmpeg = str(app/'bin/ffmpeg.exe')
        self.ffprobe = str(app/'bin/ffprobe.exe')
        self.source = self.folder/'source.mp4'
        self.run_command([self.ffmpeg,'-v','error','-y','-f','lavfi','-i',
            'testsrc2=s=160x90:r=30:d=0.3','-an','-c:v','libx264','-preset','ultrafast',
            '-threads','1',str(self.source)])
        self.project = MulticamProject(fps=30.,clips=[
            CameraClip('inside',1,str(self.source),0.,.3)],
            cuts=[Cut(0.,1),Cut(.1,1),Cut(.2,1)],edit_points=[.1,.2],
            video_stack_enabled=True,multicam_outer_order=[0])
        self.settings = ExportSettings(codec='libx264',preset='ultrafast',quality=0,
            audio='none',decode_mode='cpu',bit_depth='8-bit',parallel_workers=1,
            extra_args=['-threads','1'])
        self.commands = []

    def run_command(self, command):
        process = subprocess.run(command,capture_output=True,timeout=30,
                                 creationflags=CREATE_NO_WINDOW)
        self.assertEqual(process.returncode,0,process.stderr.decode('utf8','replace')[-2500:])
        return process.stdout

    def runner(self, command, callback):
        self.commands.append(command)
        self.run_command(command)
        return 0,''

    def render(self, output, **kwargs):
        return render_multicam(self.project,output,self.ffmpeg,self.ffprobe,
            self.settings,Capabilities(),kwargs.pop('runner',self.runner),
            work_directory=self.folder,**kwargs)

    def test_cancel_restart_reuses_only_complete_chunks_and_keeps_frame_clock(self):
        output = self.folder/'delivery.mp4'
        output.write_bytes(b'previous delivery remains on cancel')
        cancelled = threading.Event()
        def stop_on_second(command, report):
            if str(command[-1]).endswith('camera_000001.mp4'):
                Path(command[-1]).write_bytes(b'incomplete mp4')
                cancelled.set()
                return 1,'cancelled for test'
            return self.runner(command,report)
        with self.assertRaises(ExportCancelled):
            self.render(output,runner=stop_on_second,cancel_event=cancelled)
        self.assertEqual(output.read_bytes(),b'previous delivery remains on cancel')
        self.commands.clear()
        snapshots = []
        self.render(output,metrics=snapshots.append)
        encodes = [item for item in self.commands if '-frames:v' in item]
        self.assertEqual(len(encodes),2)
        self.assertEqual(snapshots[-1]['reused_segments'],1)
        self.assertEqual(snapshots[-1]['reused_frames'],3)
        trace = [json.loads(line) for line in Path(snapshots[-1]['diagnostic_path']).read_text('utf8').splitlines()]
        self.assertEqual([event['task'] for event in trace if event['event']=='checkpoint_reused'],[1])
        stream = json.loads(self.run_command([self.ffprobe,'-v','error','-count_frames',
            '-show_streams','-of','json',str(output)]))['streams'][0]
        self.assertEqual((stream['nb_read_frames'],stream['avg_frame_rate']),('9','30/1'))
        self.assertAlmostEqual(float(stream['duration']),.3,places=5)
        reference = self.folder/'reference.mp4'
        self.render(reference)
        def pixels(path):
            return self.run_command([self.ffmpeg,'-v','error','-threads','1','-i',str(path),
                '-pix_fmt','yuv420p','-threads','1','-f','rawvideo','pipe:1'])
        self.assertEqual(pixels(output),pixels(reference))


if __name__ == '__main__': unittest.main(verbosity=2)
