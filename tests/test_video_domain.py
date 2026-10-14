import asyncio
import io
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

from studio import app as module
from studio import codex, store
from studio.models import Job, Storyboard
from studio.render import run

SHOPPING_EXAMPLES = ('저 이거 몰라서 친정엄마한테 제대로 욕먹었어요.', '저 이것 때문에 시어머니한테 빵땡이 맞을 뻔했어요.',
                     '나도 남겨주시면 알려드릴게요.')


def make_video(path, seconds=3.0, color='#3366aa', band=True, size='360x640'):
    """A test source clip; `band` burns a white strip near the bottom like a hard-coded subtitle."""
    draw = ',drawbox=x=0:y=ih*0.82:w=iw:h=ih*0.06:color=white@1:t=fill' if band else ''
    run('-f', 'lavfi', '-i', f'color=c={color}:s={size}:r=30:d={seconds}', '-vf', f'format=yuv420p{draw}',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', path)


class VideoApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = self.folder = Path(self.temp.name)
        self.patches = [patch('studio.app.job_path', lambda i: folder / 'jobs' / i),
                        patch('studio.store.job_path', lambda i: folder / 'jobs' / i),
                        patch('studio.paths.DATA', folder), patch('studio.store.DATA', folder),
                        patch('studio.app.codex.login_status', return_value=False)]
        for item in self.patches:
            item.start()
        self.client = TestClient(module.app)
        self.headers = {'X-Studio-Token': module.TOKEN}
        self.clips = []
        for index, color in enumerate(['#3366aa', '#aa6633', '#33aa66', '#888888']):
            clip = folder / f'clip{index}.mp4'
            make_video(clip, 2.0 + index, color)
            self.clips.append(clip)

    def tearDown(self):
        self.client.close()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def files(self, clips):
        return [('files', (clip.name, clip.read_bytes(), 'video/mp4')) for clip in clips]

    def upload(self, clips, domain='shopping'):
        return self.client.post('/api/jobs', headers=self.headers, data={'domain': domain}, files=self.files(clips))

    def test_shopping_upload_keeps_videos_with_length_thumbnail_and_default_crop(self):
        response = self.upload(self.clips[:3])
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        self.assertEqual(job['domain'], 'shopping')
        self.assertEqual([p['kind'] for p in job['photos']], ['video'] * 3)
        self.assertEqual([round(p['duration']) for p in job['photos']], [2, 3, 4])
        self.assertEqual(job['photos'][0]['crop'], {'zoom': 1.0, 'x': 0.5, 'y': 0.5, 'user': False})
        thumb = self.client.get(f"/api/jobs/{job['id']}/files/{job['photos'][0]['thumb']}", headers=self.headers)
        self.assertEqual(thumb.status_code, 200)
        added = self.client.post(f"/api/jobs/{job['id']}/videos", headers=self.headers, files=self.files(self.clips[3:]))
        self.assertEqual(len(added.json()['photos']), 4)
        again = self.client.post(f"/api/jobs/{job['id']}/videos", headers=self.headers, files=self.files(self.clips[:1]))
        self.assertEqual(len(again.json()['photos']), 4)  # The same file is not added twice.

    def test_shopping_rejects_photos_and_non_videos(self):
        data = io.BytesIO()
        Image.new('RGB', (800, 600), 'orange').save(data, format='JPEG')
        photo = self.client.post('/api/jobs', headers=self.headers, data={'domain': 'shopping'},
                                 files=[('files', ('p.jpg', data.getvalue(), 'image/jpeg'))])
        self.assertEqual(photo.status_code, 400)
        broken = self.client.post('/api/jobs', headers=self.headers, data={'domain': 'shopping'},
                                  files=[('files', ('x.mp4', b'not a video', 'video/mp4'))])
        self.assertEqual(broken.status_code, 400)
        self.assertIn('읽을 수 없는 영상', broken.text)
        stay = self.client.post('/api/jobs', headers=self.headers,
                                files=[('files', ('p.jpg', data.getvalue(), 'image/jpeg'))]).json()
        refused = self.client.post(f"/api/jobs/{stay['id']}/videos", headers=self.headers, files=self.files(self.clips[:1]))
        self.assertEqual(refused.status_code, 400)

    def test_crop_is_validated_saved_as_the_users_and_previewed(self):
        job = self.upload(self.clips[:3]).json()
        url = f"/api/jobs/{job['id']}/sources/0/crop"
        saved = self.client.post(url, headers=self.headers, json={'zoom': 1.3, 'x': 0.5, 'y': 0.2})
        self.assertEqual(saved.json()['photos'][0]['crop'], {'zoom': 1.3, 'x': 0.5, 'y': 0.2, 'user': True})
        self.assertEqual(self.client.post(url, headers=self.headers, json={'zoom': 3}).status_code, 400)
        preview = self.client.post(f"/api/jobs/{job['id']}/preview", headers=self.headers,
                                   json={'photo': 0, 'time': 1.0, 'text': '자막', 'crop': {'zoom': 1.0, 'x': 0.5, 'y': 0.5}})
        self.assertEqual(preview.status_code, 200, preview.text)
        image = Image.open(io.BytesIO(preview.content)).convert('RGB')
        self.assertEqual(image.size, (1080, 1920))
        self.assertGreater(min(image.getpixel((540, 1632))), 240)  # The white strip shows without zoom.
        hidden = self.client.post(f"/api/jobs/{job['id']}/preview", headers=self.headers, json={'photo': 0, 'time': 1.0, 'text': ''})
        self.assertLess(min(Image.open(io.BytesIO(hidden.content)).convert('RGB').getpixel((540, 1632))), 200)

    def test_facts_accept_only_schema_fields_within_limit(self):
        job_id = self.upload(self.clips[:3]).json()['id']
        url = f'/api/jobs/{job_id}/facts'
        saved = self.client.post(url, headers=self.headers, json={'name': ' 얼룩 스프레이 ', 'memo': ''})
        self.assertEqual(saved.json()['facts'], {'name': '얼룩 스프레이'})
        self.assertEqual(self.client.post(url, headers=self.headers, json={'price': '1원'}).status_code, 400)
        self.assertEqual(self.client.post(url, headers=self.headers, json={'memo': 'x' * 301}).status_code, 400)

    def test_link_collection_is_refused_for_shopping_jobs(self):
        job_id = self.upload(self.clips[:3]).json()['id']
        for path, body in (('/api/jobs/import', {'job_id': job_id, 'collection_id': 'a' * 32, 'photos': ['x']}),
                           ('/api/composition', {'job_id': job_id, 'collection_id': 'a' * 32, 'photos': ['x'], 'mode': 'manual'})):
            response = self.client.post(path, headers=self.headers, json=body)
            self.assertEqual(response.status_code, 400)
            self.assertIn('링크로 사진을 가져올 수 없습니다', response.text)


def video_job(folder, durations=(3.0, 4.0, 5.0), **values):
    photos = []
    for index, seconds in enumerate(durations):
        make_video(folder / f'source-{index:03}.mp4', seconds)
        photos.append({'kind': 'video', 'file': f'source-{index:03}.mp4', 'duration': seconds, 'sha256': str(index),
                       'crop': {'zoom': 1.0, 'x': 0.5, 'y': 0.5, 'user': index == 2}})
    return Job(id='c' * 32, domain='shopping', workflow_version=2, photos=photos, **values)


def video_answer():
    return {'scenes': [{'id': '', 'photos': [0, 1], 'text': '가.'}, {'id': '', 'photos': [2], 'text': '나.'}],
            'script_options': [{'title': f'후보{n}', 'texts': [f'후보{n} 처음.', f'후보{n} 끝.']} for n in range(3)],
            'script_choice': 1,
            'segments': [{'source': 0, 'start': 0.2, 'end': 2.5}, {'source': 1, 'start': 1.0, 'end': 9.0},
                         {'source': 2, 'start': 0.0, 'end': 2.0}],
            'source_crops': [{'source': 0, 'zoom': 1.3, 'x': 0.5, 'y': 0.2, 'reason': '아래 자막'},
                             {'source': 1, 'zoom': 9, 'x': -1, 'y': 0.5, 'reason': '범위 밖'},
                             {'source': 2, 'zoom': 2.0, 'x': 0.5, 'y': 0.5, 'reason': '사용자 값 유지'}],
            'motion': [], 'crop_focus': [], 'motion_recommendations': [], 'selection_reasons': {}}


class VideoAnalysisTests(unittest.TestCase):
    def test_prompt_frames_crops_and_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            job = video_job(folder, facts={'name': '얼룩 제거 스프레이'})
            seen = {}
            async def fake(where, prompt, images, schema, name):
                seen.update(prompt=prompt, images=images, schema=schema)
                return video_answer()
            with patch('studio.codex.run_structured', fake), patch('studio.codex.job_path', return_value=folder):
                plan = asyncio.run(codex.analyze_video(job))
            prompt = seen['prompt']
            self.assertTrue(prompt.startswith('<ontology-schema domain="shopping" version='))
            for phrase in SHOPPING_EXAMPLES + ('썰 공식', '상품 영상 편집', '이미지 1: 소스 0 · 0.5초', '"상품명": "얼룩 제거 스프레이"'):
                self.assertIn(phrase, prompt)
            self.assertNotIn('숙소', prompt)
            self.assertEqual(len(seen['images']), 3 + 4 + 5)  # One frame per second for short clips.
            self.assertIn('source_crops', seen['schema']['properties'])
        self.assertEqual([s.text for s in plan.scenes], ['후보0 처음.', '후보0 끝.'])
        self.assertEqual(plan.script_choice, 0)
        self.assertEqual(plan.segments[1].end, 4.0)  # Clamped to the 4 s source.
        self.assertEqual(plan.motion, [])
        crops = [p['crop'] for p in job.photos]
        self.assertEqual(crops[0], {'zoom': 1.3, 'x': 0.5, 'y': 0.2, 'user': False})
        self.assertEqual(crops[1], {'zoom': 2.5, 'x': 0.0, 'y': 0.5, 'user': False})
        self.assertEqual(crops[2]['zoom'], 1.0)  # The user's own crop is kept.

    def test_users_own_script_keeps_its_words_and_only_picks_segments(self):
        script = '저 이거 몰라서 집주인한테 보증금 떼일 뻔했어요.\n바보냐고\n수세미로 아무리 밀어도 안 지워지더라고요.'
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            job = video_job(folder, facts={'name': '녹 제거 가루', 'script': script})
            seen = {}
            async def fake(where, prompt, images, schema, name):
                seen['prompt'] = prompt
                answer = video_answer()
                answer['scenes'] = [{'id': '', 'photos': [0], 'text': '바뀐 문장.'}]  # Ignored: the user's words win.
                answer['segments'] = [{'source': 0, 'start': 0.0, 'end': 1.5}, {'source': 1, 'start': 0.0, 'end': 1.5},
                                      {'source': 2, 'start': 0.0, 'end': 1.5}, {'source': 1, 'start': 2.0, 'end': 3.5}]
                return answer
            with patch('studio.codex.run_structured', fake), patch('studio.codex.job_path', return_value=folder):
                plan = asyncio.run(codex.analyze_video(job))
        self.assertIn('장면 1 (구간 2개): "저 이거 몰라서 집주인한테 보증금 떼일 뻔했어요."', seen['prompt'])
        self.assertNotIn('대본 후보 3개', seen['prompt'])
        self.assertNotIn('바보냐고"', seen['prompt'].split('사용자가 입력한')[-1])  # The script is not repeated as a fact.
        # "바보냐고" is too short for a cut of its own, so it joins the next line.
        self.assertEqual([(s.text, s.photos) for s in plan.scenes],
                         [('저 이거 몰라서 집주인한테 보증금 떼일 뻔했어요.', [0, 1]),
                          ('바보냐고, 수세미로 아무리 밀어도 안 지워지더라고요.', [2, 3])])
        self.assertEqual([o.title for o in plan.script_options], ['내 대본'])

    def test_video_plan_rejects_segments_outside_their_source(self):
        sources = [{'kind': 'video', 'duration': 3.0}]
        value = {'scenes': [{'photos': [0], 'text': '가.'}], 'segments': [{'source': 0, 'start': 1, 'end': 4}]}
        with self.assertRaisesRegex(ValueError, '3.0초까지만'):
            Storyboard.validate_video_plan(value, sources)
        value['segments'] = [{'source': 0, 'start': 1, 'end': 1.2}]
        with self.assertRaises(ValueError):
            Storyboard.validate_video_plan(value, sources)

    def test_quote_asks_only_for_voices(self):
        from studio.pipeline import Pipeline
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            job = video_job(folder, storyboard=Storyboard.validate_video_plan(
                {'scenes': [{'photos': [0], 'text': '가.'}], 'segments': [{'source': 0, 'start': 0, 'end': 2}]},
                [{'kind': 'video', 'duration': 3.0}]))
            fish = AsyncMock()
            fish.call.side_effect = AssertionError('no Fish video call for a video-domain job')
            drama = AsyncMock()
            drama.quote.return_value = {'credits': 7, 'balance': 100}
            with patch('studio.pipeline.store.read', return_value=job), patch('studio.pipeline.store.save'), \
                 patch('studio.pipeline.job_path', return_value=folder), patch('studio.store.job_path', return_value=folder):
                asyncio.run(Pipeline(fish, drama).quote(job.id, 'w'))
        self.assertEqual(job.quote['videos'], [])
        self.assertEqual(job.quote['total'], 7)


def tone(frames):
    return b''.join(struct.pack('<h', round(8000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(frames))


class VideoRenderTests(unittest.TestCase):
    def test_segments_are_zoomed_past_the_subtitle_strip_and_follow_the_voice(self):
        from studio.render import render
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            job = video_job(folder, durations=(3.0, 3.0))
            job.photos[0]['crop'] = {'zoom': 1.3, 'x': 0.5, 'y': 0.2, 'user': True}
            with wave.open(str(folder / 'voice.wav'), 'wb') as audio:
                audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                audio.writeframes(tone(96000))
            job.storyboard = Storyboard.validate_video_plan(
                {'scenes': [{'photos': [0, 1], 'text': '얼룩이, 싹 지워졌어요.'}],
                 'segments': [{'source': 0, 'start': 0.0, 'end': 2.0}, {'source': 1, 'start': 0.5, 'end': 2.5}]}, job.photos)
            job.narration = {'0': {'file': 'voice.wav'}}
            with patch('studio.render.job_path', return_value=folder):
                result = render(job)
            # 0.1 s thumbnail still, then 2 s of voice; fast-cut reels add no pause.
            self.assertAlmostEqual(result['duration'], 2.1, delta=0.05)
            def pixel(at):
                run('-ss', str(at), '-i', folder / result['video'], '-frames:v', '1', folder / 'f.png')
                with Image.open(folder / 'f.png') as image:
                    return min(image.convert('RGB').getpixel((540, 1632)))
            self.assertLess(pixel(0.3), 200)   # Source 0 is zoomed: no white strip.
            self.assertGreater(pixel(1.8), 240)  # Source 1 keeps its strip at zoom 1.0.
            self.assertEqual([t['photo'] for t in result_timeline(folder)], [0, 1])

    def test_reel_opens_on_a_titled_thumbnail_still(self):
        from studio.render import render
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            job = video_job(folder, durations=(3.0,))
            with wave.open(str(folder / 'voice.wav'), 'wb') as audio:
                audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                audio.writeframes(tone(96000))
            job.storyboard = Storyboard.validate_video_plan(
                {'scenes': [{'photos': [0], 'text': '가.'}], 'segments': [{'source': 0, 'start': 0.0, 'end': 2.0}]}, job.photos)
            job.narration = {'0': {'file': 'voice.wav'}}
            job.options.thumbnail.text = '녹이 싹'
            job.options.thumbnail.style.color = '#FF0000'
            def reds(name):  # Pure-red pixels of the first frame, where the title sits.
                run('-i', folder / name, '-frames:v', '1', folder / 'first.png')
                with Image.open(folder / 'first.png') as image:
                    return sum(r > 180 and g < 90 and b < 90 for r, g, b in image.convert('RGB').crop((200, 450, 880, 780)).getdata())
            with patch('studio.render.job_path', return_value=folder):
                result = render(job)
                self.assertGreater(reds(result['file']), 2000)  # The final reel's first frame carries the title.
                self.assertEqual(reds(result['video']), 0)  # The clean video opens on the same still without it.
                job.options.thumbnail.enabled = False
                self.assertAlmostEqual(render(job)['duration'], result['duration'] - 0.1, delta=0.01)


def result_timeline(folder):
    import json
    return json.loads((folder / 'verification.json').read_text(encoding='utf-8'))['timeline']


if __name__ == '__main__':
    unittest.main()
