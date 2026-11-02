"""Hardware eligibility must see interlace and automatic display transforms."""
from copy import deepcopy
import unittest

from exporter_core import MediaInfo, _media_info_from_payload, _media_probe_command


class GpuSourceMetadataTests(unittest.TestCase):
    def media(self, **changes):
        stream = dict(codec_type='video', codec_name='hevc', width=7680, height=4320,
                      pix_fmt='yuv420p10le', avg_frame_rate='30000/1001', duration='2',
                      color_primaries='bt709', color_transfer='bt709', color_space='bt709',
                      color_range='tv')
        stream.update(deepcopy(changes))
        return _media_info_from_payload('camera.mov', {'streams': [stream]})

    def test_probe_requests_display_matrix_rotation_and_field_order(self):
        command = _media_probe_command('ffprobe', 'camera.mov')
        entries = command[command.index('-show_entries')+1]
        self.assertIn('duration,field_order:', entries)
        self.assertIn('stream_tags=timecode,alpha_mode,rotate:', entries)
        self.assertIn('stream_side_data=side_data_type,rotation,displaymatrix:', entries)

    def test_old_media_defaults_remain_progressive_eligible(self):
        self.assertEqual(MediaInfo('camera.mov').field_order, 'unknown')
        self.assertFalse(MediaInfo('camera.mov').has_display_transform)
        self.assertEqual(self.media().field_order, 'unknown')
        self.assertFalse(self.media().has_display_transform)
        self.assertEqual(self.media(field_order='progressive').field_order, 'progressive')
        for order in ('tt', 'bb', 'tb', 'bt'):
            with self.subTest(order=order):
                self.assertEqual(self.media(field_order=order).field_order, order)

    def test_nonzero_or_malformed_rotate_tag_requires_compatible_path(self):
        for rotation in ('90', '-90', '180', '360', 'oops', 'nan', 'inf', None, ''):
            with self.subTest(rotation=rotation):
                self.assertTrue(self.media(tags={'rotate': rotation}).has_display_transform)
        for rotation in ('0', '-0.0', 0):
            with self.subTest(rotation=rotation):
                self.assertFalse(self.media(tags={'rotate': rotation}).has_display_transform)

    def test_display_matrix_catches_reflection_and_identity_conservatively(self):
        for item in (
            {'side_data_type': 'Display Matrix', 'rotation': 0},
            {'displaymatrix': '65536 0 0\n0 65536 0\n0 0 1073741824', 'rotation': 0},
            {'displaymatrix': '-65536 0 0\n0 65536 0\n0 0 1073741824', 'rotation': 0},
            {'rotation': -90}, {'rotation': 'bad'},
        ):
            with self.subTest(item=item):
                self.assertTrue(self.media(side_data_list=[item]).has_display_transform)
        self.assertFalse(self.media(side_data_list=[{'rotation': 0}]).has_display_transform)
        self.assertFalse(self.media(side_data_list=[{'side_data_type': 'Mastering display metadata'}])
                         .has_display_transform)

    def test_malformed_side_data_is_conservative_and_audio_only_keeps_defaults(self):
        for side_data in ({'rotation': 90}, ['bad']):
            self.assertTrue(self.media(side_data_list=side_data).has_display_transform)
        media = _media_info_from_payload('audio.wav', {'streams': [
            dict(codec_type='audio', codec_name='pcm_s16le', duration='2')]}, allow_audio_only=True)
        self.assertFalse(media.has_display_transform)
        self.assertEqual(media.field_order, 'unknown')


if __name__ == '__main__':
    unittest.main()
