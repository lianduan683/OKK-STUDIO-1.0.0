"""Atlas geometry avoids unused transmitted pixels and invalid GPU textures."""
import unittest

from stack_gpu import atlas_layout


class AtlasLayoutTests(unittest.TestCase):
    def test_three_720p_layers_have_no_fourth_cell(self):
        layout = atlas_layout(3, 1280, 720)
        self.assertEqual((layout['columns'], layout['rows']), (3, 1))
        self.assertEqual(layout['columns']*layout['rows']*1280*720*4, 11059200)

    def test_full_36_layer_grid_stays_compact(self):
        layout = atlas_layout(36, 1280, 720)
        self.assertEqual((layout['columns'], layout['rows']), (6, 6))

    def test_large_cells_choose_an_orientation_within_texture_limits(self):
        layout = atlas_layout(3, 8192, 2160)
        self.assertEqual((layout['columns'], layout['rows']), (1, 3))

    def test_prime_layer_count_uses_padding_when_exact_layout_does_not_fit(self):
        layout = atlas_layout(23, 1280, 720)
        self.assertEqual(layout['columns']*layout['rows'], 24)
        self.assertLessEqual(layout['columns']*1280, 16384)
        self.assertLessEqual(layout['rows']*720, 16384)

    def test_all_supported_layer_counts_cover_every_layer_within_limits(self):
        for count in range(1, 37):
            with self.subTest(count=count):
                layout = atlas_layout(count, 1280, 720)
                self.assertGreaterEqual(layout['columns']*layout['rows'], count)
                self.assertLessEqual(layout['columns']*1280, 16384)
                self.assertLessEqual(layout['rows']*720, 16384)

    def test_impossible_layout_and_invalid_dimensions_fail_before_upload(self):
        for count, width, height in ((36,4096,4096), (1,0,720), (1,1280,True),
                                     (1,1280.5,720), (1,16385,720)):
            with self.subTest(count=count,width=width,height=height):
                with self.assertRaises(ValueError): atlas_layout(count,width,height)


if __name__ == '__main__': unittest.main()
