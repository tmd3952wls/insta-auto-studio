import asyncio
import io
import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from studio import app as module
from studio import codex, domains, enhance, recommendation, store
from studio.models import Job, Storyboard, plan_clips

SHOPPING_EXAMPLES = ('저 이것 때문에 남편이랑 끝날 뻔했어요', '여러분 다이소 가시면 이건 꼭 사오세요',
                     '여러분 저 드디어 찾았어요', '궁금하시면 나도 남겨주세요')


def plan_answer(count=10):
    order = list(range(count))
    groups = [order[:5], order[5:]]
    return {'scenes': [{'id': '', 'photos': g, 'text': '문장.'} for g in groups],
            'script_options': [{'title': f'후보{n}', 'texts': ['첫 문장.', '끝 문장.']} for n in range(3)],
            'script_choice': 0,
            'motion': [{'photo': c['photos'][0], 'prompt': 'Shot 1 push.', 'photos': c['photos'], 'seconds': 4}
                       for c in plan_clips(order)],
            'crop_focus': [{'photo': i, 'x': 0.5, 'y': 0.5} for i in order],
            'motion_recommendations': [{'photo': i, 'recommended': True, 'reason': '좋음', 'prompt': '', 'explanation': ''}
                                       for i in order],
            'selection_reasons': {}}


def analysis_prompt(job):
    seen = {}
    async def fake(folder, prompt, photos, schema, name):
        seen['prompt'] = prompt
        return plan_answer(len(job.photos))
    with patch('studio.codex.run_structured', fake), patch('studio.codex.job_path', return_value=Path('.')):
        asyncio.run(codex.analyze(job))
    return seen['prompt']


def job(domain='stay', **values):
    return Job(id='d' * 32, domain=domain, workflow_version=2, photo_order=list(range(10)),
               photos=[{'file': f'{i}.jpg', 'labels': []} for i in range(10)], **values)


class DomainSchemaTests(unittest.TestCase):
    def test_both_schemas_load_and_describe_their_objects(self):
        stay, shopping = domains.load('stay'), domains.load('shopping')
        self.assertIn('Accommodation', domains.schema_block(stay))
        self.assertIn('간판', enhance.prompt())
        block = domains.schema_block(shopping)
        self.assertTrue(block.startswith('<ontology-schema domain="shopping" version=') and block.endswith('</ontology-schema>'))
        for name in ('Product', 'Feature', 'UseScene', 'SourceVideo', 'Segment', 'name(상품명, 사용자 입력)'):
            self.assertIn(name, block)
        self.assertTrue(stay.features.link_collection)
        self.assertFalse(shopping.features.link_collection)
        self.assertEqual([p.name for _, p in shopping.facts()], ['name', 'reference', 'memo', 'script'])
        self.assertEqual(stay.facts(), [])
        self.assertTrue(shopping.disclosure)
        self.assertEqual((stay.media, shopping.media), ('photo', 'video'))
        self.assertEqual((shopping.min_media, shopping.max_media), (3, 15))
        self.assertTrue(stay.disclosure)
        with self.assertRaises(ValueError):
            domains.load('travel')

    def test_stay_prompt_keeps_every_skill_line_and_only_adds_the_schema(self):
        prompt = analysis_prompt(job())
        self.assertTrue(prompt.startswith('<ontology-schema domain="stay" version='))
        for name in codex.SKILL_FILES:
            self.assertIn(domains.load('stay').skill(name), prompt)
        self.assertIn('서로 다른 객실 종류는 다른 장면으로 구분한다', prompt)
        self.assertNotIn('사용자가 입력한', prompt)

    def test_old_jobs_without_a_domain_open_as_stays(self):
        value = json.loads(Job(id='f' * 32).model_dump_json())
        del value['domain'], value['facts']
        old = Job.model_validate(value)
        self.assertEqual((old.domain, old.facts), ('stay', {}))


class DomainApiTests(unittest.TestCase):
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

    def tearDown(self):
        self.client.close()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def upload(self, domain=None):
        data = io.BytesIO()
        Image.new('RGB', (800, 600), 'orange').save(data, format='JPEG')
        return self.client.post('/api/jobs', headers=self.headers, data={'domain': domain} if domain else None,
                                files=[('files', ('p.jpg', data.getvalue(), 'image/jpeg'))])

    def test_upload_creates_a_job_in_the_chosen_domain(self):
        stay = self.upload().json()
        self.assertEqual(stay['domain'], 'stay')
        self.assertEqual(self.upload('travel').status_code, 400)
        self.assertEqual(self.upload('shopping').status_code, 400)  # Shopping takes videos, not photos.
        listed = {item['id']: item['domain'] for item in self.client.get('/api/jobs', headers=self.headers).json()}
        self.assertEqual(listed[stay['id']], 'stay')
        names = [d['id'] for d in self.client.get('/api/domains', headers=self.headers).json()]
        self.assertEqual(names, ['stay', 'stay-silent', 'shopping'])

    def test_stay_categories_and_silent_upload(self):
        categories = self.client.get('/api/domains').json()
        self.assertEqual([d['label'] for d in categories], ['숙소(음성O)', '숙소(음성X)', '쇼핑'])
        silent = self.upload('stay-silent')
        self.assertEqual(silent.status_code, 200)
        self.assertEqual(silent.json()['domain'], 'stay-silent')
        self.assertFalse(domains.load('stay-silent').features.narration)
        self.assertEqual(domains.load('stay-silent').skill('SKILL_MOTION.md'), domains.load('stay').skill('SKILL_MOTION.md'))

def tone(frames):
    return b''.join(struct.pack('<h', round(8000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(frames))


class DisclosureRenderTests(unittest.TestCase):
    def render(self, domain):
        from studio.render import render, run
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        Image.new('RGB', (360, 640), '#228844').save(folder / 'photo.jpg')
        with wave.open(str(folder / 'voice.wav'), 'wb') as audio:
            audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            audio.writeframes(tone(24000))
        work = Job(id='f' * 32, domain=domain, photos=[{'file': 'photo.jpg'}],
                   storyboard=Storyboard(scenes=[{'photos': [0], 'text': '초록빛 쉼표.'}]),
                   narration={'0': {'file': 'voice.wav'}})
        with patch('studio.render.job_path', return_value=folder):
            result = render(work)
        def corner(key):
            run('-i', folder / result[key], '-frames:v', '1', folder / f'{key}.png')
            with Image.open(folder / f'{key}.png') as image:
                return max(high - low for low, high in image.crop((820, 30, 1060, 180)).convert('RGB').getextrema())
        return corner('file'), corner('video')

    def test_shopping_reel_shows_the_disclosure_but_the_clean_video_does_not(self):
        final, clean = self.render('shopping')
        self.assertGreater(final, 100)
        self.assertLess(clean, 10)

    def test_stay_reel_shows_top_right_advertisement(self):
        final, clean = self.render('stay')
        self.assertGreater(final, 100)
        self.assertLess(clean, 10)

    def test_disclosure_text_changes_render_revision(self):
        from studio.assets import render_revision
        stay = Job(id='f' * 32)
        domain = domains.load('stay').model_copy(deep=True)
        with patch('studio.assets.domains.load', return_value=domain):
            before = render_revision(stay)
            domain.disclosure = '다른 광고 표시'
            self.assertNotEqual(before, render_revision(stay))

    def test_advertisement_badge_stays_in_top_right_safe_margin(self):
        from studio.media import badge_image
        # The default spot is the one the user asked for (39 px from the right, 44 px down); it is adjustable per job.
        left, top, right, bottom = badge_image('광고').getbbox()
        self.assertGreater(left, 800)
        self.assertGreaterEqual(top, 40)
        self.assertLessEqual(right, 1050)
        self.assertLess(bottom, 180)


if __name__ == '__main__':
    unittest.main()
