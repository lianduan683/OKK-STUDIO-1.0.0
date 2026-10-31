"""Independent framing ranges, persistence and timeline metadata semantics."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clip_transform import (ClipTransform, ClipTransformRange, clip_transforms_from_dict,
                            framing_filter, set_transform_on_range, transform_at,
                            transform_is_identity, validate_transform)
from creative_core import ColorGrade, Transition, grade_filter
from creative_render import (TransitionSegment, build_transition_command, has_pixel_effects,
                             plan_creative_segments)
from exporter_core import ExportSettings
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import grade_at, program_spans, set_grade_on_range
from timeline_edits import (delete_source_clips, finalize_source_edit, move_source_clips,
                            remove_program_boundaries)


def project(**changes):
    values = dict(clips=[CameraClip('a', 1, 'a.mov', 0, 6)], fps=30)
    values.update(changes)
    return MulticamProject(**values)


class ClipTransformTests(unittest.TestCase):
    def test_default_and_lookup_are_independent_values(self):
        value = project()
        self.assertEqual(transform_at(value, 0), ClipTransform())
        self.assertTrue(transform_is_identity(transform_at(value, 2)))
        framing = ClipTransform('fill', 2, -.5, .25)
        set_transform_on_range(value, 1, 3, framing)
        framing.zoom = 4
        self.assertEqual(transform_at(value, 1).zoom, 2)
        looked_up = transform_at(value, 2)
        looked_up.x = 1
        self.assertEqual(transform_at(value, 2).x, -.5)
        for time in (-1, .5, 3, 6, 20):
            self.assertTrue(transform_is_identity(transform_at(value, time)))

    def test_replace_and_reset_do_not_change_neighbours_colour_or_camera(self):
        value = project(cuts=[Cut(0, 1)])
        set_grade_on_range(value, 0, 6, ColorGrade(exposure=.5))
        grades, cuts = deepcopy(value.clip_grades), deepcopy(value.cuts)
        set_transform_on_range(value, 1, 5, ClipTransform('fill', 1.5))
        set_transform_on_range(value, 2, 3, ClipTransform('fit', 2, -.5, .5))
        self.assertEqual([(r.start, r.end, r.transform.zoom) for r in value.clip_transforms],
                         [(1, 2, 1.5), (2, 3, 2), (3, 5, 1.5)])
        set_transform_on_range(value, 2.5, 4, ClipTransform())
        self.assertEqual([(r.start, r.end, r.transform.zoom) for r in value.clip_transforms],
                         [(1, 2, 1.5), (2, 2.5, 2), (4, 5, 1.5)])
        self.assertEqual((value.clip_grades, value.cuts), (grades, cuts))
        self.assertEqual(grade_at(value, 3).exposure, .5)
        self.assertEqual(transform_at(value, 4).zoom, 1.5)
        self.assertTrue(transform_is_identity(transform_at(value, 3)))
        value.validate()

    def test_frame_quantization_loaded_boundaries_and_phase(self):
        value = project(clip_transforms=[ClipTransformRange(1, 2, ClipTransform('fill'))])
        self.assertEqual([(s.start, s.end) for s in program_spans(value)], [(0, 1), (1, 2), (2, 6)])
        self.assertEqual([s.frames for s in value.segments()], [30, 30, 120])
        self.assertEqual([s.phase_origin for s in value.segments()], [0, 0, 0])
        self.assertEqual(transform_at(value, 1.999).mode, 'fit')
        self.assertEqual(transform_at(value, 1.98).mode, 'fill')
        set_transform_on_range(value, 2.009, 3.008, ClipTransform('fill', 2))
        self.assertEqual([(r.start, r.end) for r in value.clip_transforms], [(1, 2), (2, 3)])
        value.range_start, value.range_end = 1.5, 2.5
        self.assertEqual([s.frames for s in value.segments()], [15, 15])

    def test_validation_rejects_bad_values_and_ranges_atomically(self):
        value = project()
        before = deepcopy(value)
        for transform in (None, {}, ClipTransform('other'), ClipTransform(zoom=True),
                          ClipTransform(zoom=.99), ClipTransform(zoom=4.01), ClipTransform(x=1.01),
                          ClipTransform(y=-1.01), ClipTransform(x=float('nan')),
                          ClipTransform(y=float('inf')), ClipTransform(zoom='2')):
            with self.subTest(transform=transform), self.assertRaises(ValueError):
                set_transform_on_range(value, 1, 2, transform)
            self.assertEqual(value, before)
        for start, end in ((-1, 2), (1, 7), (2, 1), (2, 2.001), (True, 2), (1, float('nan'))):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                set_transform_on_range(value, start, end, ClipTransform('fill'))
            self.assertEqual(value, before)
        for records in ({}, [dict(start=1)], [ClipTransformRange(1, 2, ClipTransform()),
                                             ClipTransformRange(1.5, 3, ClipTransform())],
                        [ClipTransformRange(1, 1.001, ClipTransform())],
                        [ClipTransformRange(1, 7, ClipTransform())]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                project(clip_transforms=records).validate()
        validate_transform(ClipTransform('fill', 4, -1, 1))

    def test_version18_save_load_and_all_previous_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            value = project(clips=[CameraClip('a', 1, str(root / 'a.mov'), 0, 6)])
            original = value.to_payload(root)
            self.assertEqual(original['version'], 8)
            self.assertNotIn('clip_transforms', original)
            for version in range(1, 18):
                old = dict(original, version=version)
                loaded = MulticamProject.from_payload(old, root)
                self.assertEqual(loaded.clip_transforms, [])
                self.assertEqual(loaded.to_payload(root), original)
            set_transform_on_range(value, 1, 3, ClipTransform('fill', 1.5, -.25, .75))
            path = root / 'framing.okkmulti'
            value.save(path)
            raw = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(raw['version'], 18)
            self.assertEqual(raw['clip_transforms'], [dict(start=1, end=3,
                transform=dict(mode='fill', zoom=1.5, x=-.25, y=.75))])
            self.assertEqual(MulticamProject.load(path), value)
            set_transform_on_range(value, 0, 6, ClipTransform())
            reset = value.to_payload(root)
            self.assertEqual(reset['version'], 8)
            self.assertNotIn('clip_transforms', reset)

    def test_unknown_serialized_fields_are_rejected(self):
        for raw in (None, {}, [None], [dict(start=1, end=2)],
                    [dict(start=1, end=2, transform=None)],
                    [dict(start=1, end=2, transform=dict(rotation=1))],
                    [dict(start=1, end=2, transform=dict(mode='fill'), camera=1)]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                clip_transforms_from_dict(raw)

    def test_identity_keeps_previous_fit_normalization_and_validates_dimensions(self):
        self.assertEqual(framing_filter(ClipTransform(), 160, 90),
            'scale=160:90:force_original_aspect_ratio=decrease:force_divisible_by=2,'
            'pad=160:90:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1')
        self.assertEqual(framing_filter(ClipTransform(), 160, 90, pad_color='black@0'),
            'scale=160:90:force_original_aspect_ratio=decrease:force_divisible_by=2,'
            'pad=160:90:(ow-iw)/2:(oh-ih)/2:color=black@0,setsar=1')
        for width, height in ((0, 90), (161, 90), (160, 0), (True, 90), (160., 90)):
            with self.subTest(width=width, height=height), self.assertRaises(ValueError):
                framing_filter(ClipTransform('fill'), width, height)
        with self.assertRaises(ValueError):
            framing_filter(ClipTransform(), 160, 90, pad_color='black,drawbox')

    def test_effect_routing_respects_local_boundaries(self):
        value = project()
        self.assertFalse(has_pixel_effects(value, 1))
        set_transform_on_range(value, 1, 2, ClipTransform('fill'))
        self.assertTrue(has_pixel_effects(value, 1))
        for time, expected in ((0, False), (1, True), (1.5, True), (2, False)):
            self.assertEqual(has_pixel_effects(value, 1, time), expected)

    def test_transition_sides_sample_actual_cut_for_full_and_partial_exports(self):
        value = project(fps=10)
        set_transform_on_range(value, 0, 3, ClipTransform('fill', 1, 0, -1))
        set_transform_on_range(value, 3, 6, ClipTransform('fill', 2, 1, 1))
        set_grade_on_range(value, 3, 6, ColorGrade(exposure=.5))
        value.transitions = [Transition(3, 'fade', 1)]
        graphs = []
        for start, end in ((0, 6), (2.8, 3.2)):
            value.range_start, value.range_end = start, end
            job = next(s for s in plan_creative_segments(value, value.segments())
                       if isinstance(s, TransitionSegment))
            command = build_transition_command(job, value, ['ffmpeg'], ExportSettings(),
                                              160, 90, 'yuv420p', '10/1', [], 'out.mp4')
            graph = command[command.index('-filter_complex') + 1]
            sides = graph.split(';')[:2]
            self.assertIn(framing_filter(transform_at(value, 2.9), 160, 90), sides[0])
            self.assertIn(framing_filter(transform_at(value, 3), 160, 90), sides[1])
            self.assertLess(sides[1].index('crop='), sides[1].index(grade_filter(value.clip_grades[0].grade)))
            graphs.append(sides)
        self.assertEqual(graphs[0], graphs[1])

    def test_tail_delete_and_source_move_follow_grade_clock(self):
        value = project(clips=[CameraClip('a', 1, 'a.mov', 0, 3),
                               CameraClip('b', 1, 'b.mov', 3, 3)])
        set_transform_on_range(value, 1, 5, ClipTransform('fill'))
        set_grade_on_range(value, 1, 5, ColorGrade(exposure=.5))
        delete_source_clips(value, ['b'])
        self.assertEqual([(r.start, r.end) for r in value.clip_transforms], [(1, 3)])
        self.assertEqual([(r.start, r.end) for r in value.clip_grades], [(1, 3)])
        move_source_clips(value, ['a'], 1)
        finalize_source_edit(value)
        self.assertEqual([(r.start, r.end) for r in value.clip_transforms], [(1, 3)])
        self.assertEqual([(r.start, r.end) for r in value.clip_grades], [(1, 3)])
        delete_source_clips(value, ['a'])
        self.assertEqual((value.clip_transforms, value.clip_grades), ([], []))

    def test_program_boundary_merge_inherits_left_framing_and_restores_next(self):
        value = project()
        set_transform_on_range(value, 0, 2, ClipTransform('fill', 1, 0, -1))
        set_transform_on_range(value, 2, 4, ClipTransform('fill', 2))
        set_transform_on_range(value, 4, 6, ClipTransform('fill', 3))
        self.assertEqual(remove_program_boundaries(value, [2]), 1)
        self.assertEqual([(r.start, r.end, r.transform.zoom) for r in value.clip_transforms],
                         [(0, 4, 1), (4, 6, 3)])
        value.validate()


class ClipTransformGpuRoutingTests(unittest.TestCase):
    def test_neutral_open_gate_export_uses_exact_timeline_size_and_never_cuda_bypass(self):
        from tests.test_gpu_export_routing import GpuExportRoutingTests
        import multicam_core

        fixture = GpuExportRoutingTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        value = fixture.project()
        value.camera_grades.clear()
        value.global_grade = ColorGrade()
        value.project_settings = dict(width=160, height=90, sample_rate=48000, bit_depth='24bit')
        fixture.media = replace(fixture.media, width=160, height=120, sample_aspect_ratio='4:3')
        set_transform_on_range(value, 0, value.duration, ClipTransform('fill'))
        with patch.object(multicam_core, '_cuda_segment_filter') as fast:
            fixture.export(value, settings=replace(fixture.settings, resolution='timeline'))
        fast.assert_not_called()
        self.assertEqual(fixture.plans, [])
        command = fixture.commands[0]
        chain = fixture.option(command, '-vf')
        self.assertIn(framing_filter(ClipTransform('fill'), 160, 90), chain)
        self.assertEqual(fixture.option(command, '-c:v'), 'hevc_nvenc')

    def test_normal_and_transition_cannot_bypass_framing_in_gpu_plans(self):
        # Reuse the established external-process/probe fixture, while running
        # the real programme, transition and export orchestration code.
        from tests.test_gpu_export_routing import GpuExportRoutingTests

        for transition in (False, True):
            with self.subTest(transition=transition):
                fixture = GpuExportRoutingTests(methodName='runTest')
                fixture.setUp()
                try:
                    value = fixture.project(transition=transition)
                    set_transform_on_range(value, 0, 3, ClipTransform('fill', 2, -1, 1))
                    fixture.export(value)
                    self.assertEqual(fixture.plans, [])
                    self.assertEqual([fixture.route(c) for c in fixture.commands], ['cpu', 'mux'])
                    command = fixture.commands[0]
                    chain = fixture.option(command, '-filter_complex' if transition else '-vf')
                    self.assertIn('crop=', chain)
                    if not transition:
                        self.assertIn('-hwaccel', command)
                    self.assertNotIn('-hwaccel_output_format', command)
                    self.assertEqual(fixture.option(command, '-c:v'), 'hevc_nvenc')
                    self.assertLess(chain.index('crop='), chain.index(grade_filter(value.camera_grades[1])))
                finally:
                    fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
