"""Applying a synced camera run must not introduce sub-frame gaps."""
import copy
import math
import unittest

from editor_core import AlignmentResult
from multicam_core import CameraClip
from reference_sync import quantized_alignment_targets


class AlignmentTargetTests(unittest.TestCase):
    def test_continuous_fractional_durations_share_one_quantized_translation(self):
        clips = [CameraClip('a', 1, 'a.mp4', 0, 10.01),
                 CameraClip('b', 1, 'b.mp4', 10.01, 7.007),
                 CameraClip('c', 1, 'c.mp4', 17.017, 5)]
        pairs = [(clip.id, AlignmentResult(-(clip.start + 12.01), .9, 50, 'any text')) for clip in clips]
        before = copy.deepcopy((clips, pairs))
        targets = quantized_alignment_targets(clips, pairs, 30)
        self.assertAlmostEqual(targets['a'], 12)
        self.assertAlmostEqual(targets['b'] - targets['a'], clips[0].duration)
        self.assertAlmostEqual(targets['c'] - targets['b'], clips[1].duration)
        self.assertEqual((clips, pairs), before)

    def test_fractional_track_origin_and_negative_shift_keep_relative_positions(self):
        clips = [CameraClip('a', 1, 'a.mp4', 1.007, 10.01),
                 CameraClip('b', 1, 'b.mp4', 11.017, 20)]
        pairs = [(clip.id, AlignmentResult(-(clip.start - 4.013), .7, 50)) for clip in clips]
        targets = quantized_alignment_targets(clips, pairs, 30000 / 1001)
        delta = round(-4.013 * 30000 / 1001) / (30000 / 1001)
        self.assertAlmostEqual(targets['a'], clips[0].start + delta)
        self.assertAlmostEqual(targets['b'], clips[1].start + delta)
        self.assertAlmostEqual(targets['b'] - targets['a'], 10.01)

    def test_disconnected_or_different_camera_clips_snap_independently(self):
        clips = [CameraClip('a', 1, 'a.mp4', 1.007, 10.01),
                 CameraClip('b', 1, 'b.mp4', 12.017, 20),
                 CameraClip('c', 2, 'c.mp4', 11.017, 20)]
        pairs = [(clip.id, AlignmentResult(-(clip.start + 12.01), .9, 50)) for clip in clips]
        targets = quantized_alignment_targets(clips, pairs, 30)
        for clip in clips:
            self.assertEqual(targets[clip.id], round((clip.start + 12.01) * 30) / 30)

    def test_different_offsets_are_not_coerced_into_a_common_shift(self):
        clips = [CameraClip('a', 1, 'a.mp4', 0, 10.01), CameraClip('b', 1, 'b.mp4', 10.01, 10)]
        targets = quantized_alignment_targets(clips, [('a', AlignmentResult(-12.01, .9, 50)),
                                                     ('b', AlignmentResult(-22.04, .9, 50))], 30)
        self.assertEqual(targets, {'a': 12., 'b': 22.033333333333335})

    def test_low_confidence_and_nonfinite_results_are_omitted(self):
        clips = [CameraClip(str(index), index + 1, f'{index}.mp4', 0, 10) for index in range(5)]
        pairs = [('0', AlignmentResult(-2, .19, 50)), ('1', AlignmentResult(math.nan, .9, 50)),
                 ('2', AlignmentResult(-2, math.nan, 50)), ('3', AlignmentResult(-2.01, .9, 50))]
        self.assertEqual(quantized_alignment_targets(clips, pairs, 30), {'3': 2.})

    def test_unknown_result_ids_do_not_create_targets(self):
        self.assertEqual(quantized_alignment_targets([], [('missing', AlignmentResult(-1, .9, 50))], 30), {})


if __name__ == '__main__':
    unittest.main()
