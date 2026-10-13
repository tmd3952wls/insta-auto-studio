import tempfile
import unittest
from pathlib import Path

from PIL import Image


class StudioCoreTests(unittest.TestCase):
    def test_caption_default_matches_reference_coordinate(self):
        from studio.models import CaptionStyle
        style = CaptionStyle()
        self.assertEqual(style.center(1080, 1920), (540, 710))
        self.assertEqual(style.font_size, 64)
        self.assertEqual(style.background_opacity, 0.2)

    def test_caption_shows_one_short_phrase_per_cue(self):
        from studio.media import caption_phrases, caption_segments
        from studio.models import CaptionStyle
        self.assertEqual(caption_phrases('여기 한국 맞아? 사진만 보고, 해외 숙소인 줄 알았어요.'),
                         ['여기 한국', '맞아?', '사진만 보고', '해외 숙소인', '줄 알았어요'])
        # Unpunctuated narration (e.g. edited by hand) still splits at word boundaries.
        self.assertEqual(caption_phrases('바다 바로 앞에 이런 독채들이 쭉 놓여 있는데'),
                         ['바다 바로 앞에', '이런 독채들이 쭉', '놓여 있는데'])
        segments = caption_segments('주방도 넓어서, 음식 해 먹기 좋고.', CaptionStyle())
        self.assertEqual(segments, ['주방도 넓어서', '음식 해', '먹기 좋고'])
        self.assertEqual(caption_phrases('안녕하세요 남승진입니다'), ['안녕하세요', '남승진입니다'])

    def test_long_shots_are_split_into_punch_in_cuts(self):
        from studio.media import cut_zoom, motion_cuts
        self.assertEqual(motion_cuts(60), [0, 30])  # 2s shot -> wide + punch-in.
        self.assertEqual(motion_cuts(45), [0])  # 1.5s shot stays one cut.
        self.assertEqual(cut_zoom(60), 'if(lt(on,30),1.0,1.15)')
        self.assertEqual(cut_zoom(45), '1.0')
        # Video domains split a shot only when it would stay on screen past the limit.
        self.assertEqual(motion_cuts(60, longest=2.0), [0])
        self.assertEqual(motion_cuts(75, longest=2.0), [0, 37])
        self.assertEqual(cut_zoom(150, longest=2.0), 'if(lt(on,50),1.0,if(lt(on,100),1.15,1.0))')

    def test_clip_plan_groups_three_to_four_photos_into_25_seconds(self):
        from studio.models import plan_clips
        expected = {10: [(4, 10), (3, 8), (3, 7)], 12: [(4, 9), (4, 8), (4, 8)], 13: [(4, 7), (3, 6), (3, 6), (3, 6)],
                    15: [(4, 7), (4, 7), (4, 6), (3, 5)], 17: [(4, 6), (4, 6), (3, 5), (3, 4), (3, 4)]}
        for count, clips in expected.items():
            plan = plan_clips(list(range(count)))
            self.assertEqual([(len(c['photos']), c['seconds']) for c in plan], clips)
            self.assertEqual([p for c in plan for p in c['photos']], list(range(count)))

    def test_speech_end_ignores_trailing_silence(self):
        from studio.render import run, speech_end
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'voice.wav'
            run('-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.6', '-af', 'apad=pad_dur=0.4',
                '-ar', '48000', '-ac', '2', audio)
            self.assertAlmostEqual(speech_end(audio), 0.6, delta=0.02)

    def test_shot_bounds_follow_hard_cuts_in_a_generated_clip(self):
        from studio.render import run, shot_bounds
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory) / 'clip.mp4'
            run('-f', 'lavfi', '-i', 'color=red:s=320x568:d=1.4:r=24', '-f', 'lavfi', '-i', 'color=blue:s=320x568:d=1.2:r=24',
                '-f', 'lavfi', '-i', 'color=green:s=320x568:d=1.4:r=24', '-filter_complex', '[0][1][2]concat=n=3:v=1', clip)
            bounds = shot_bounds(clip, 3)
            self.assertEqual(len(bounds), 3)
            self.assertAlmostEqual(bounds[0][1], 1.4, delta=0.1)
            self.assertAlmostEqual(bounds[1][1], 2.6, delta=0.1)
            self.assertAlmostEqual(bounds[2][1], 4.0, delta=0.1)

    def test_crop_fills_portrait_without_stretching(self):
        from studio.media import crop_photo
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'landscape.jpg'
            Image.new('RGB', (1600, 900), '#ff3300').save(source)
            result = crop_photo(source, focus_x=0.5, focus_y=0.5)
            self.assertEqual(result.size, (1080, 1920))
            self.assertEqual(result.getpixel((5, 5)), result.getpixel((540, 960)))

    def test_fingerprint_changes_for_motion_but_not_captions(self):
        from studio.models import motion_fingerprint
        self.assertEqual(motion_fingerprint('image-hash', 'Water ripples'),
                         motion_fingerprint('image-hash', 'Water ripples'))
        self.assertNotEqual(motion_fingerprint('image-hash', 'Water ripples'),
                            motion_fingerprint('image-hash', 'Person walks'))

    def test_storyboard_rejects_missing_and_repeated_photos(self):
        from studio.models import Storyboard
        with self.assertRaises(ValueError):
            Storyboard.validate_plan({'scenes': [{'photos': [0], 'text': '숙소입니다'}],
                                     'motion': []}, photo_count=2, max_motion=3)
        with self.assertRaises(ValueError):
            Storyboard.validate_plan({'scenes': [{'photos': [0, 0], 'text': '숙소입니다'}],
                                     'motion': []}, photo_count=2, max_motion=3)

    def test_timing_uses_audio_and_keeps_every_photo(self):
        from studio.media import allocate_frames
        durations = allocate_frames(3.6, [False, True, False], fps=30)
        self.assertEqual(sum(durations), 108)
        self.assertEqual(len(durations), 3)
        self.assertGreater(durations[1], durations[0])
        self.assertTrue(all(value > 0 for value in durations))

    def test_codex_schema_is_strict_for_every_nested_object(self):
        from studio.codex import strict_schema
        from studio.models import Storyboard
        schema = strict_schema(Storyboard.model_json_schema())
        self.assertFalse(schema['additionalProperties'])
        for definition in schema['$defs'].values():
            self.assertFalse(definition['additionalProperties'])
            self.assertEqual(set(definition['required']),set(definition['properties']))


if __name__ == '__main__':
    unittest.main()
