"""Interchange timing, camera edits, offline relinking and real Tk regression."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from multicam_core import MulticamProject
from timeline_audio import audio_sources
from timeline_import import (ImportedClip, ImportedTimeline, build_project, read_timeline,
                             missing_media, relink_media, sequence_names, _timecode)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def read(self, content, ext='.xml', **options):
        path = self.base/('edit'+ext)
        path.write_text(content, encoding='utf-8')
        return read_timeline(path, **options)

    def test_fcp7_trim_rates_reference_and_audio(self):
        timeline = self.read('''<xmeml><sequence><name>Edit</name><rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate>
          <duration>300</duration><media><video><track><clipitem><name>one</name><start>30</start><end>90</end><in>48</in>
          <rate><timebase>24</timebase></rate><file id="f"><name>相机 A.mov</name><pathurl>file://localhost/C:/media/%E7%9B%B8%E6%9C%BA%20A.mov</pathurl></file></clipitem>
          <clipitem><start>120</start><end>150</end><in>0</in><file id="f"/></clipitem></track></video>
          <audio><track><clipitem><start>30</start><end>90</end><in>0</in><file id="f"/></clipitem></track></audio></media></sequence></xmeml>''')
        self.assertAlmostEqual(timeline.fps, 30000/1001)
        self.assertAlmostEqual(timeline.clips[0].start, 1.001)
        self.assertEqual(timeline.clips[0].source_in, 2.)
        self.assertEqual(timeline.clips[0].path, r'C:\media\相机 A.mov')
        self.assertEqual(timeline.clips[0].path, timeline.clips[1].path)
        project = build_project(timeline)
        self.assertAlmostEqual(project.duration, 10.01)
        self.assertEqual(len(audio_sources(project)), 1)
        self.assertEqual(len(project.clips), 2)

    def test_nested_xml_trim_projects_child_source_to_parent_clock(self):
        timeline = self.read('''<xmeml><sequence><rate><timebase>25</timebase></rate><media><video><track>
        <clipitem><start>250</start><end>350</end><in>50</in><sequence><rate><timebase>25</timebase></rate><media><video><track>
        <clipitem><start>25</start><end>125</end><in>100</in><file><pathurl>a.mov</pathurl></file></clipitem>
        </track></video></media></sequence></clipitem></track></video></media></sequence></xmeml>''')
        self.assertTrue(timeline.multicam)
        c = timeline.clips[0]
        self.assertEqual((c.start, c.duration, c.source_in), (10., 3., 5.))

    def fcpx(self):
        return '''<?xml version="1.0"?><!DOCTYPE fcpxml SYSTEM "http://example.invalid/FCPXML.dtd">
        <fcpxml version="1.11"><resources>
        <format id="fmt" frameDuration="1/25s"/>
        <asset id="a" src="a.mov" start="100s" hasVideo="1" hasAudio="1"/>
        <asset id="b" start="200s" hasVideo="1" hasAudio="1"><media-rep kind="original-media" src="b.mov"/></asset>
        <media id="mc" name="Interview"><multicam format="fmt">
        <mc-angle angleID="A" name="Wide"><asset-clip ref="a" offset="0s" start="100s" duration="20s"/></mc-angle>
        <mc-angle angleID="B" name="Close"><asset-clip ref="b" offset="2s" start="200s" duration="18s"/></mc-angle>
        </multicam></media></resources><library><event><project name="Multi"><sequence format="fmt" duration="8s"><spine>
        <mc-clip ref="mc" offset="0s" start="4s" duration="4s"><mc-source angleID="A" srcEnable="all"/></mc-clip>
        <mc-clip ref="mc" offset="4s" start="8s" duration="4s"><mc-source angleID="B" srcEnable="video"/><mc-source angleID="A" srcEnable="audio"/></mc-clip>
        </spine></sequence></project></event></library></fcpxml>'''

    def test_fcpx_multicam_retains_both_angles_and_selected_edits(self):
        timeline = self.read(self.fcpx(), '.fcpxml')
        self.assertTrue(timeline.multicam)
        self.assertEqual(timeline.fps, 25.)
        project = build_project(timeline, collapsed=True)
        self.assertEqual(len(project.clips), 4)
        self.assertEqual(len(project.cameras), 2)
        wide, close = project.cameras
        self.assertEqual(project.camera_at(1), wide)
        self.assertEqual(project.camera_at(5), close)
        self.assertEqual(project.clip_at(close, 5).source_in, 6.)
        sources = audio_sources(project)
        self.assertTrue(all(s.track == wide for s in sources))
        self.assertEqual([(s.start, s.duration, s.source_start) for s in sources], [(0., 4., 4.), (4., 4., 8.)])
        restored = MulticamProject.from_payload(project.to_payload(self.base), self.base)
        self.assertTrue(restored.timeline_multicam_collapsed)
        self.assertEqual(restored.cuts, project.cuts)
        self.assertEqual(restored.clips, project.clips)
        self.assertEqual(audio_sources(restored), sources)

    def test_fcpx_connected_clip_source_clock(self):
        timeline = self.read('''<fcpxml><resources><format id="f" frameDuration="1/25s"/>
        <asset id="a" src="a.mov" start="100s"/><asset id="b" src="b.mov" start="0s"/></resources><project><sequence format="f"><spine>
        <asset-clip ref="a" offset="2s" start="105s" duration="10s"><asset-clip ref="b" lane="1" offset="108s" start="1s" duration="2s"/></asset-clip>
        </spine></sequence></project></fcpxml>''', '.fcpxml')
        self.assertEqual([(c.start, c.source_in, c.duration) for c in timeline.clips], [(2., 5., 10.), (5., 1., 2.)])
        project = build_project(timeline)
        self.assertEqual([(c.time, c.camera) for c in project.cuts], [(2., 1), (5., 2), (7., 1)])

    def test_multicam_angle_gap_does_not_switch_to_another_angle(self):
        xml = self.fcpx().replace('offset="0s" start="100s" duration="20s"',
            'offset="0s" start="100s" duration="6s"')
        timeline = self.read(xml, '.fcpxml')
        project = build_project(timeline)
        wide, close = project.cameras
        self.assertEqual(project.camera_at(3.), wide)
        self.assertIsNone(project.clip_at(wide, 3.))
        self.assertIsNotNone(project.clip_at(close, 3.))
        self.assertEqual(project.camera_at(5.), close)

    def test_otio_trim_available_range_and_parallel_audio(self):
        import json
        def rt(value, rate=24): return {'value': value, 'rate': rate}
        def span(start, length): return {'start_time': rt(start), 'duration': rt(length)}
        def clip(path): return {'OTIO_SCHEMA': 'Clip.2', 'source_range': span(86448, 48), 'media_references': {'DEFAULT_MEDIA': {
            'OTIO_SCHEMA': 'ExternalReference.1', 'target_url': path, 'available_range': span(86400, 240)}}}
        payload = {'OTIO_SCHEMA': 'Timeline.1', 'name': 'OTIO', 'global_start_time': rt(86400), 'tracks': {
            'OTIO_SCHEMA': 'Stack.1', 'children': [
                {'OTIO_SCHEMA': 'Track.1', 'kind': 'Video', 'children': [{'OTIO_SCHEMA': 'Gap.1', 'source_range': span(0, 24)}, clip('a.mov')]},
                {'OTIO_SCHEMA': 'Track.1', 'kind': 'Audio', 'children': [clip('a.wav')]}]}}
        timeline = self.read(json.dumps(payload), '.otio')
        self.assertEqual(timeline.fps, 24.)
        self.assertEqual([(c.start, c.duration, c.source_in, c.kind) for c in timeline.clips], [(1., 2., 2., 'video'), (0., 2., 2., 'audio')])

    def test_edl_origin_names_gaps_and_drop_frame(self):
        timeline = self.read('''TITLE: EDL\nFCM: NON-DROP FRAME
001 AX V C 00:00:02:00 00:00:04:00 01:00:00:00 01:00:02:00
* FROM CLIP NAME: 素材.mov
002 AX V C 00:00:06:00 00:00:07:00 01:00:04:00 01:00:05:00
* FROM CLIP NAME: 素材.mov''', '.edl', fps=25.)
        self.assertEqual([(c.start, c.duration, c.source_in) for c in timeline.clips], [(0., 2., 2.), (4., 1., 6.)])
        self.assertEqual(timeline.origin, 3600.)
        self.assertEqual(Path(timeline.clips[0].path).name, '素材.mov')
        self.assertAlmostEqual(_timecode('01:00:00;00', 30000/1001, True), 3599.9964)
        with self.assertRaises(ValueError): _timecode('00:01:00;00', 30000/1001, True)
        with self.assertRaises(ValueError): _timecode('01:00:00;00', 25., True)

    def test_mlt_inclusive_out_blank_and_track(self):
        timeline = self.read('''<mlt producer="tractor"><profile frame_rate_num="25" frame_rate_den="1"/>
        <chain id="a" out="249"><property name="resource">a.mov</property></chain>
        <playlist id="v"><blank length="25"/><entry producer="a" in="50" out="99"/></playlist>
        <tractor id="tractor"><track producer="v" hide="audio"/></tractor></mlt>''', '.mlt')
        self.assertEqual([(c.start, c.duration, c.source_in) for c in timeline.clips], [(1., 2., 2.)])

    def test_real_aaf_file_reads_through_bundled_adapter(self):
        import opentimelineio as otio
        rt = otio.opentime.RationalTime
        tr = otio.opentime.TimeRange
        clip = otio.schema.Clip(name='AAF camera', source_range=tr(rt(48, 24), rt(72, 24)),
                               media_reference=otio.schema.ExternalReference(
                                   target_url=(self.base/'camera.mov').as_uri(),
                                   available_range=tr(rt(0, 24), rt(240, 24))))
        timeline = otio.schema.Timeline(name='AAF Edit', global_start_time=rt(86400, 24))
        track = otio.schema.Track(name='V1', kind=otio.schema.TrackKind.Video)
        track.extend([otio.schema.Gap(source_range=tr(rt(0, 24), rt(24, 24))), clip])
        timeline.tracks.append(track)
        path = self.base/'real.aaf'
        otio.adapters.write_to_file(timeline, str(path), adapter_name='AAF', use_empty_mob_ids=True)
        imported = read_timeline(path)
        self.assertEqual(imported.fps, 24.)
        self.assertEqual(len(imported.clips), 1)
        self.assertEqual((imported.clips[0].start, imported.clips[0].duration, imported.clips[0].source_in), (1., 3., 2.))

    def test_multiple_sequences_and_fcpx_bundle(self):
        xml = '<xmeml><project><children><sequence><name>One</name></sequence><sequence><name>Two</name></sequence></children></project></xmeml>'
        path = self.base/'many.xml'; path.write_text(xml)
        self.assertEqual(sequence_names(path), ['One', 'Two'])
        bundle = self.base/'edit.fcpxmld'; bundle.mkdir()
        (bundle/'Info.fcpxml').write_text(self.fcpx())
        self.assertEqual(sequence_names(bundle), ['Multi'])
        self.assertEqual(read_timeline(bundle).name, 'Multi')

    def test_relink_unique_only_preserves_ambiguous_and_offsets(self):
        timeline = ImportedTimeline('Offline', clips=[ImportedClip(str(self.base/'old'/'a.mov'), 'V1', 1., 3., 4.),
                                                     ImportedClip(str(self.base/'old'/'b.mov'), 'V2', 2., 3.)])
        folder = self.base/'new'; folder.mkdir(); (folder/'a.mov').touch(); (folder/'b.mov').touch()
        sub = folder/'sub'; sub.mkdir(); (sub/'b.mov').touch()
        self.assertEqual(relink_media(timeline, folder), 1)
        self.assertEqual(len(missing_media(timeline)), 1)
        self.assertEqual(timeline.clips[0].source_in, 4.)

    def test_bad_entities_speeds_and_unknown_format_fail(self):
        with self.assertRaisesRegex(ValueError, '实体'):
            self.read('<!DOCTYPE x [<!ENTITY x "bad">]><xmeml/>')
        with self.assertRaises(ValueError): self.read('not a project', '.prproj')
        with self.assertRaisesRegex(ValueError, '变速'):
            self.read('''TITLE: X
001 AX V C 00:00:00:00 00:00:01:00 01:00:00:00 01:00:01:00
M2 AX 050.0 00:00:00:00''', '.edl')

    def test_disabled_video_cannot_leak_into_programme(self):
        timeline = ImportedTimeline('Disabled', clips=[ImportedClip('disabled.mov', 'V1', 0., 10., enabled=False),
                                                       ImportedClip('enabled.mov', 'V2', 2., 3.)], duration=12.)
        project = build_project(timeline)
        self.assertEqual(len(project.clips), 1)
        self.assertIsNone(project.clip_at(project.camera_at(0), 0))
        self.assertIsNone(project.clip_at(project.camera_at(8), 8))
        self.assertEqual(project.duration, 12.)

    def test_legacy_payload_does_not_require_new_view_version(self):
        project = build_project(ImportedTimeline('X', clips=[ImportedClip('a.mov', 'V1', 0., 1.)]))
        payload = project.to_payload(self.base)
        self.assertNotIn('timeline_multicam_collapsed', payload)
        self.assertFalse(MulticamProject.from_payload(payload, self.base).timeline_multicam_collapsed)
        project.timeline_multicam_collapsed = True
        self.assertEqual(project.to_payload(self.base)['version'], 16)


from tests import test_timeline_drag_merge as ui_fixture
from tests import test_multicam as render_fixture


class ImportedRenderTests(unittest.TestCase):
    setUpClass = classmethod(render_fixture.FFmpegMulticamTests.setUpClass.__func__)
    tearDownClass = classmethod(render_fixture.FFmpegMulticamTests.tearDownClass.__func__)
    run_command = staticmethod(render_fixture.FFmpegMulticamTests.run_command)
    process = classmethod(render_fixture.FFmpegMulticamTests.process.__func__)
    render = render_fixture.FFmpegMulticamTests.render
    assert_frames = render_fixture.FFmpegMulticamTests.assert_frames
    assert_color = render_fixture.FFmpegMulticamTests.assert_color
    assert_continuous_master = render_fixture.FFmpegMulticamTests.assert_continuous_master

    def test_xml_import_exports_selected_pictures_black_gap_and_continuous_audio(self):
        from xml.sax.saxutils import escape
        path = self.root/'real.xml'
        path.write_text(f'''<xmeml><sequence><rate><timebase>24</timebase></rate><duration>72</duration><media><video>
        <track><clipitem><start>0</start><end>36</end><in>25</in><file><rate><timebase>25</timebase></rate>
        <pathurl>{escape(self.red.as_uri())}</pathurl></file></clipitem></track>
        <track><clipitem><start>48</start><end>72</end><in>45</in><rate><timebase>30</timebase></rate>
        <file><pathurl>{escape(self.blue.as_uri())}</pathurl></file></clipitem></track></video><audio><track>
        <clipitem><start>0</start><end>72</end><in>0</in><file><pathurl>{escape(self.master.as_uri())}</pathurl></file></clipitem>
        </track></audio></media></sequence></xmeml>''', encoding='utf-8')
        project = build_project(read_timeline(path), collapsed=True)
        self.assertEqual(project.clips[0].source_in, 1.)
        output = self.render('imported.mp4', project)
        self.assert_frames(output, 72)
        self.assert_color(output, .5, (255, 0, 0))
        self.assert_color(output, 1.75, (0, 0, 0))
        self.assert_color(output, 2.5, (0, 0, 255))
        self.assert_continuous_master(output, 0., 3.)


class ImportUITests(unittest.TestCase):
    setUp = ui_fixture.TimelineDropTests.setUp
    tearDown = ui_fixture.TimelineDropTests.tearDown
    def test_multicam_view_retains_programme_source_audio_undo_and_save(self):
        workspace = self.workspace
        before = workspace.project.to_payload(Path.cwd())
        self.assertTrue(workspace.set_multicam_timeline_view(True))
        self.root.update()
        self.assertFalse(workspace.timeline_clip_items)
        self.assertTrue(workspace.timeline_program_items)
        self.assertTrue(workspace.timeline_camera_audio_items)
        self.assertEqual(len(workspace.project.clips), 2)
        workspace.undo()
        self.assertEqual(workspace.project.to_payload(Path.cwd()), before)
        workspace.redo()
        self.assertTrue(workspace.project.timeline_multicam_collapsed)
        workspace.set_multicam_timeline_view(False)
        self.assertTrue(workspace.timeline_clip_items)
        self.assertFalse(self.errors)

    def test_import_publication_is_undoable_and_preserves_old_edit(self):
        workspace = self.workspace
        old = workspace.project.to_payload(Path.cwd())
        timeline = ImportedTimeline('Imported', fps=25., clips=[ImportedClip('new.mov', 'A', 1., 4., 2.)])
        workspace._review_interchange(timeline, True)
        window = next(w for w in workspace.winfo_children() if w.winfo_class() == 'Toplevel')
        def buttons(widget):
            for child in widget.winfo_children():
                if child.winfo_class() == 'TButton': yield child
                yield from buttons(child)
        with patch.object(workspace, 'stop_monitors'):
            next(b for b in buttons(window) if b.cget('text') == '导入时间线').invoke()
        self.assertEqual(workspace.project.fps, 25.)
        self.assertEqual(workspace.project.clips[0].source_in, 2.)
        self.assertTrue(workspace.project.timeline_multicam_collapsed)
        workspace.undo()
        self.assertEqual(workspace.project.to_payload(Path.cwd()), old)
        workspace.redo()
        self.assertEqual(workspace.project.clips[0].source_in, 2.)
        self.assertFalse(self.errors)


if __name__ == '__main__': unittest.main()
