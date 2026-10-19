import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from studio import app as module
from studio import sourcing, store
from tests.test_video_domain import make_video

FAKE = Path(__file__).with_name('fake_shop_search.py')
LINK = 'https://www.instagram.com/reel/ABC123/'


class ShopSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = self.folder = Path(self.temp.name)
        self.patches = [patch('studio.app.job_path', lambda i: folder / 'jobs' / i),
                        patch('studio.store.job_path', lambda i: folder / 'jobs' / i),
                        patch('studio.paths.DATA', folder), patch('studio.store.DATA', folder)]
        for item in self.patches:
            item.start()
        self.client = TestClient(module.app)
        self.headers = {'X-Studio-Token': module.TOKEN}
        self.clips = []
        for index, color in enumerate(['#3366aa', '#aa6633']):
            clip = folder / f'0{index + 1}_clip_vmake.mp4'
            make_video(clip, 2.0 + index, color)
            self.clips.append(clip)

    def tearDown(self):
        self.client.close()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def fake(self, *extra):
        (self.folder / 'shop-search.json').write_text(json.dumps(
            {'root': str(self.folder), 'command': [sys.executable, '-u', str(FAKE), '{url}', '--delay', '0', *extra]}),
            encoding='utf-8')

    def start(self, **body):
        response = self.client.post('/api/shop-searches', headers=self.headers, json={'url': LINK, **body})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def finish(self, search_id):
        asyncio.run(sourcing.run(sourcing.read(search_id), module.deliver_search))
        return self.client.get('/api/shop-searches', params={'job_id': sourcing.read(search_id)['job_id']}).json()[0]

    def test_found_videos_become_the_new_shopping_jobs_uploaded_videos(self):
        self.fake(*map(str, self.clips))
        value = self.start()
        self.assertEqual(value['job']['domain'], 'shopping')
        self.assertEqual(value['search']['state'], 'waiting')
        search = self.finish(value['search']['id'])
        self.assertEqual(search['state'], 'done', search)
        self.assertEqual((search['added'], search['product']), (2, '지문 인식 USB 메모리'))
        job = store.read(value['job']['id'])
        self.assertEqual([p['name'] for p in job.photos], [c.name for c in self.clips])
        self.assertTrue(all(p['kind'] == 'video' and p['thumb'] and p['crop'] for p in job.photos))
        self.assertEqual(job.photos[0]['found']['how'], 'vmake 자막 삭제')
        self.assertEqual(job.photo_order, [0, 1])
        self.assertIn('2개', job.message)

    def test_videos_already_in_the_job_are_not_added_twice(self):
        response = self.client.post('/api/jobs', headers=self.headers, data={'domain': 'shopping'},
                                    files=[('files', (self.clips[0].name, self.clips[0].read_bytes(), 'video/mp4'))])
        self.fake(*map(str, self.clips))
        value = self.start(job_id=response.json()['id'])
        search = self.finish(value['search']['id'])
        self.assertEqual((search['state'], search['added'], len(search['skipped'])), ('done', 1, 1))
        self.assertEqual(len(store.read(value['job']['id']).photos), 2)

    def test_failures_are_explained_in_korean(self):
        self.fake('--fail', '받은 영상이 없어요.')
        search = self.finish(self.start()['search']['id'])
        self.assertEqual((search['state'], search['error']), ('failed', '받은 영상이 없어요.'))
        self.fake()
        search = self.finish(self.start(job_id=search['job_id'])['search']['id'])
        self.assertEqual(search['state'], 'failed')
        self.assertIn('0개', search['error'])

    def test_bad_links_and_photo_jobs_are_refused(self):
        response = self.client.post('/api/shop-searches', headers=self.headers, json={'url': 'https://youtube.com/x'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('인스타', response.json()['detail'])
        response = self.client.post('/api/shop-searches', headers=self.headers, json={'url': LINK, 'domain': 'stay'})
        self.assertEqual(response.status_code, 400)

    def test_one_job_waits_for_its_running_search(self):
        self.fake(*map(str, self.clips))
        job_id = self.start()['job']['id']
        response = self.client.post('/api/shop-searches', headers=self.headers, json={'url': LINK, 'job_id': job_id})
        self.assertEqual(response.status_code, 400)
        second = self.start()['search']
        self.assertEqual(second['ahead'], 1)


if __name__ == '__main__':
    unittest.main()
