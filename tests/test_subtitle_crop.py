import unittest

from studio.subtitle_crop import overlays, window


def frame(*items):
    return [{'text': text, 'box': box} for text, box in items]


class SubtitleCropTests(unittest.TestCase):
    def test_changing_chinese_lines_at_one_height_are_subtitles_and_get_zoomed_out(self):
        lines = ['滑进来的朋友你们好', '家里无论是厨房台面', '还是大理石', '甚至是顽固铁锈']
        frames = [frame((line, (0.12, 0.81, 0.87, 0.85)), ('REC', (0.9, 0.03, 0.98, 0.06)),
                        ('老抽', (0.1 + n * 0.1, 0.3, 0.2 + n * 0.1, 0.33))) for n, line in enumerate(lines)]
        hidden = overlays(frames)
        self.assertEqual(sorted(item['kind'] for item in hidden), ['subtitle', 'watermark'])  # The moving label stays.
        crop = window(hidden, 9 / 16)
        self.assertTrue(1.0 < crop['zoom'] <= 1.6)
        visible_bottom = crop['y'] * (1 - 1 / crop['zoom']) + 1 / crop['zoom']
        self.assertLessEqual(visible_bottom, 0.81)  # The subtitle strip is below the frame.

    def test_clean_clips_are_left_alone_and_centre_text_cannot_be_zoomed_away(self):
        self.assertEqual(overlays([frame(), frame(), frame()]), [])
        centre = [{'kind': 'subtitle', 'sample': '中', 'box': (0.1, 0.45, 0.9, 0.55)}]
        self.assertIsNone(window(centre, 9 / 16))


if __name__ == '__main__':
    unittest.main()
