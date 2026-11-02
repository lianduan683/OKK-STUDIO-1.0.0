"""Checkpoint adoption and invalidation without launching media processes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from creative_core import escape_filter_path
from export_checkpoint import CheckpointStore, capture_legacy


class ExportCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='okk-checkpoint-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'original.mov'
        self.source.write_bytes(b'original camera bytes')
        self.output = self.root/'delivery.mov'
        self.chunk = self.root/'camera_000000.mp4'
        self.chunk.write_bytes(b'finalized-picture-fixture'*100)
        self.graph_path = self.root/'stack_000000.filtergraph'
        self.graph = ('color=c=black:s=3840x2160:r=30000/1001,format=yuv444p10le,'
            'trim=end_frame=30[stack0];[0:v]format=yuva444p10le[layer0];'
            '[stack0][layer0]overlay=format=yuv444p10[stack1];'
            '[stack1]format=yuv420p10le[picture]')
        self.graph_path.write_text(self.graph, 'utf-8')
        self.command = ['ffmpeg', '-hide_banner', '-y', '-threads', '6', '-ss', '1.001000000',
            '-i', str(self.source), '-/filter_complex', str(self.graph_path), '-map', '[picture]',
            '-an', '-c:v', 'hevc_nvenc', '-preset', 'p1', '-tune', 'hq', '-rc', 'vbr',
            '-b:v', '80M', '-multipass', 'disabled', '-g', '120', '-bf', '2',
            '-profile:v', 'main10', '-split_encode_mode', '2', '-fps_mode', 'cfr', '-gpu', '0',
            '-pix_fmt', 'yuv420p10le', '-r', '30000/1001', '-frames:v', '30',
            '-map_metadata', '-1', '-video_track_timescale', '30000', str(self.chunk)]
        self.expected = dict(expected_frames=30, width=3840, height=2160,
                             pixel_format='yuv420p10le', rate='30000/1001')
        self.probe = dict(codec_name='hevc', width=3840, height=2160, pix_fmt='yuv420p10le',
            avg_frame_rate='30000/1001', r_frame_rate='30000/1001', nb_frames='30',
            duration='1.001000', color_range='tv')
        self.mock_probe = patch('export_checkpoint.subprocess.run', side_effect=lambda *a, **k:
            SimpleNamespace(returncode=0, stdout=json.dumps({'streams': [self.probe]}).encode()))
        self.runner = self.mock_probe.start()
        self.addCleanup(self.mock_probe.stop)
        self.store = CheckpointStore(self.root/'checkpoints', self.output, 'fixture-ffprobe')

    def commit(self, command=None, **kwargs):
        return self.store.commit(command or self.command, self.chunk, **self.expected, **kwargs)

    def reuse(self, command=None, destination=None, **kwargs):
        return self.store.reuse(command or self.command, destination or self.root/'resumed.mp4',
                                **(self.expected | kwargs))

    def test_completed_chunk_survives_original_cleanup_and_is_reused_without_reencoding(self):
        self.assertTrue(self.commit())
        expected = self.chunk.read_bytes()
        self.chunk.unlink()
        self.assertTrue(self.reuse())
        self.assertEqual((self.root/'resumed.mp4').read_bytes(), expected)
        self.assertEqual(self.runner.call_count, 2)
        self.assertEqual(self.source.read_bytes(), b'original camera bytes')

    def test_source_graph_encoder_seek_and_dimensions_change_invalidate(self):
        self.assertTrue(self.commit())
        for flag, value in (('-b:v', '40M'), ('-ss', '2.002'), ('-bf', '0'), ('-r', '30')):
            with self.subTest(flag=flag):
                command = list(self.command)
                command[command.index(flag)+1] = value
                self.assertFalse(self.reuse(command))
        self.assertFalse(self.reuse(width=1920))
        self.graph_path.write_text(self.graph.replace('overlay=', 'eq=brightness=0.1,overlay='), 'utf-8')
        self.assertFalse(self.reuse())

    def test_bitrate_change_exposes_composed_pixels_but_never_direct_reuse(self):
        self.assertTrue(self.commit())
        command = list(self.command)
        command[command.index('-b:v')+1] = '20M'
        self.assertFalse(self.reuse(command))
        found = self.store.transcode_candidate(command, **self.expected)
        self.assertIsNotNone(found)
        self.assertEqual(found['previous_bitrate'], '80M')
        self.assertTrue(Path(found['path']).is_file())
        for flag, value in (('-bf','0'),('-ss','3.0'),('-r','30')):
            changed = list(command)
            changed[changed.index(flag)+1] = value
            self.assertIsNone(self.store.transcode_candidate(changed, **self.expected))
        self.source.write_bytes(b'updated original camera')
        self.assertIsNone(self.store.transcode_candidate(command, **self.expected))
        self.graph_path.write_text(self.graph, 'utf-8')
        self.source.write_bytes(b'changed original camera data')
        self.assertFalse(self.reuse())

    def test_gpu_execution_options_and_ephemeral_graph_path_do_not_change_picture_identity(self):
        self.graph = self.graph.replace('yuv420p10le[picture]',
            'yuv420p10le,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=limited[picture]')
        self.graph_path.write_text(self.graph, 'utf-8')
        gpu = list(self.command)
        gpu[:1] = ['gpu-ffmpeg', '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda']
        index = gpu.index('-/filter_complex')
        gpu[index:index+2] = ['-vf', 'scale_cuda=3840:2160']
        gpu[gpu.index('-gpu')+1] = '2'
        index = gpu.index('-pix_fmt')
        del gpu[index:index+2]
        gpu[-1:-1] = ['-color_primaries', 'bt709', '-color_range', 'tv']
        self.assertTrue(self.commit(gpu, graph_override=self.graph_path))
        other_graph = self.root/'different-working-directory.filtergraph'
        other_graph.write_text(self.graph, 'utf-8')
        original = list(self.command)
        original[original.index('-/filter_complex')+1] = str(other_graph)
        self.assertTrue(self.reuse(original))

    def test_explicit_different_output_color_range_invalidates(self):
        self.assertTrue(self.commit())
        command = list(self.command)
        command[-1:-1] = ['-color_range', 'pc']
        self.assertFalse(self.reuse(command))

    def test_input_alpha_decoder_option_does_not_replace_output_encoder_identity(self):
        command = list(self.command)
        index = command.index('-i')
        command[index:index] = ['-c:v', 'libvpx-vp9']
        signature = self.store.signature(command)
        self.assertIsNotNone(signature)
        self.assertEqual(dict(signature['render']['encoder'])['-c:v'], 'hevc_nvenc')
        self.assertEqual(signature['render']['sources'][0]['decoder'], 'libvpx-vp9')
        self.assertTrue(self.commit(command))
        self.assertTrue(self.reuse(command))
        self.assertFalse(self.reuse(self.command, destination=self.root/'without-alpha-decoder.mp4'))

    def test_partial_wrong_frame_count_codec_depth_and_duration_are_rejected(self):
        for field, value in (('nb_frames', '29'), ('codec_name', 'h264'), ('pix_fmt', 'yuv420p'),
                             ('avg_frame_rate', '30/1'), ('duration', '1.1')):
            with self.subTest(field=field):
                original = self.probe[field]
                self.probe[field] = value
                self.store._probe_cache.clear()
                self.assertFalse(self.commit())
                self.probe[field] = original
        self.chunk.write_bytes(b'partial')
        self.assertFalse(self.commit())
        self.assertFalse(list(self.store.root.glob('*.json')))

    def test_full_range_checkpoint_keeps_its_signal_and_rejects_changed_probe(self):
        self.probe['color_range'] = 'pc'
        self.probe['color_space'] = 'bt709'
        self.assertTrue(self.commit())
        self.assertTrue(self.reuse())
        self.probe['color_range'] = 'tv'
        self.store._probe_cache.clear()
        self.assertFalse(self.reuse(destination=self.root/'range-changed.mp4'))

    def test_cpu_stack_signal_normalization_remains_part_of_picture_signature(self):
        normalized = self.graph.replace('yuv420p10le[picture]',
            'yuv420p10le,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=limited[picture]')
        self.graph_path.write_text(normalized, 'utf-8')
        self.assertTrue(self.commit())
        self.assertTrue(self.reuse())
        self.graph_path.write_text(normalized.replace('range=limited', 'range=full'), 'utf-8')
        self.assertFalse(self.reuse(destination=self.root/'changed-signal-graph.mp4'))

    def test_probe_failure_does_not_publish_checkpoint(self):
        self.runner.side_effect = lambda *a, **k: SimpleNamespace(returncode=1, stdout=b'')
        self.assertFalse(self.commit())
        self.assertFalse(self.reuse())

    def test_lut_contents_are_signed_and_identical_regenerated_paths_are_reusable(self):
        lut = self.root/'look.cube'
        lut.write_text('LUT_3D_SIZE 2\n0 0 0\n', 'utf-8')
        graph = self.graph.replace('[0:v]format=',
            '[0:v]lut3d=file='+escape_filter_path(str(lut))+':interp=tetrahedral,format=')
        self.graph_path.write_text(graph, 'utf-8')
        self.assertTrue(self.commit())
        regenerated = self.root/'regenerated.cube'
        regenerated.write_bytes(lut.read_bytes())
        self.graph_path.write_text(graph.replace(escape_filter_path(str(lut)),
                                  escape_filter_path(str(regenerated))), 'utf-8')
        self.assertTrue(self.reuse())
        regenerated.write_text('LUT_3D_SIZE 2\n1 0 0\n', 'utf-8')
        self.assertFalse(self.reuse(destination=self.root/'changed-lut.mp4'))

    def test_unknown_effect_inputs_or_output_options_do_not_claim_a_safe_signature(self):
        self.graph_path.write_text(self.graph.replace('[picture]', ',subtitles=secret.ass[picture]'), 'utf-8')
        self.assertIsNone(self.store.signature(self.command))
        self.graph_path.write_text(self.graph, 'utf-8')
        command = list(self.command)
        command[-1:-1] = ['-unmodeled-output-option', '42']
        self.assertIsNone(self.store.signature(command))

    def test_existing_destination_and_changed_cached_file_are_not_overwritten(self):
        self.assertTrue(self.commit())
        destination = self.root/'resumed.mp4'
        destination.write_bytes(b'previous user result')
        self.assertFalse(self.reuse())
        self.assertEqual(destination.read_bytes(), b'previous user result')
        record = json.loads(next(self.store.root.glob('*.json')).read_text('utf-8'))
        (self.store.root/record['chunk']).write_bytes(b'corrupted cached bytes')
        self.assertFalse(self.reuse(destination=self.root/'second.mp4'))

    def test_source_change_during_probe_rejects_commit(self):
        def changing_probe(*a, **k):
            self.source.write_bytes(b'changed during encoding')
            return SimpleNamespace(returncode=0, stdout=json.dumps({'streams': [self.probe]}).encode())
        self.runner.side_effect = changing_probe
        self.assertFalse(self.commit())

    def test_parallel_same_key_commit_is_atomic_and_has_one_manifest(self):
        other = CheckpointStore(self.root/'checkpoints', self.output, 'fixture-ffprobe')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda store: store.commit(self.command, self.chunk, **self.expected),
                                    (self.store, other)))
        self.assertEqual(results, [True, True])
        self.assertEqual(len(list(self.store.root.glob('*.json'))), 1)
        self.assertTrue(self.reuse())

    def trace(self, events):
        trace = self.root/'legacy.jsonl'
        trace.write_text('\n'.join(json.dumps(item) for item in [
            dict(event='export_start', output=str(self.output), width=3840, height=2160,
                 frames=90, rate='30000/1001'), *events])+'\n', 'utf-8')
        return trace

    def test_legacy_capture_uses_only_latest_successful_finalized_attempt(self):
        events = [dict(event='attempt_start', task=1, frames=30, command=self.command),
                  dict(event='attempt_finish', task=1, returncode=0),
                  dict(event='attempt_start', task=2, frames=30, command=self.command),
                  dict(event='attempt_finish', task=2, returncode=1),
                  dict(event='attempt_start', task=3, frames=30, command=self.command)]
        trace = self.trace(events)
        with trace.open('a', encoding='utf-8') as stream:
            stream.write('{"event": "unfinished')
        report = self.store.capture_legacy(trace, self.root)
        self.assertEqual((report['captured'], report['skipped'], report['pending']), (1, 1, 1))
        self.assertTrue(self.reuse())
        second = capture_legacy(trace, self.root, 'fixture-ffprobe', root=self.root/'second-store')
        self.assertEqual(second['captured'], 1)

    def test_legacy_restart_without_finish_and_modified_source_are_not_adopted(self):
        start = dict(event='attempt_start', task=1, frames=30, command=self.command)
        trace = self.trace([start, dict(event='attempt_finish', task=1, returncode=0), start])
        self.assertEqual(self.store.capture_legacy(trace, self.root)['pending'], 1)
        trace = self.trace([start, dict(event='attempt_finish', task=1, returncode=0)])
        stat = trace.stat()
        started = getattr(stat, 'st_birthtime_ns', stat.st_ctime_ns)
        os.utime(self.source, ns=(started+10_000_000_000, started+10_000_000_000))
        report = self.store.capture_legacy(trace, self.root)
        self.assertEqual((report['captured'], report['skipped']), (0, 1))


if __name__ == '__main__':
    unittest.main()
