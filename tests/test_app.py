import io
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from studio import app as module


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.folder = folder
        self.patches = [patch('studio.app.job_path', lambda i: folder/i),
                        patch('studio.store.job_path', lambda i: folder/i),
                        patch('studio.paths.DATA', folder),
                        patch('studio.app.codex.login_status', return_value=False)]
        for item in self.patches: item.start()
        self.client = TestClient(module.app)
        self.headers = {'X-Studio-Token': module.TOKEN}

    def tearDown(self):
        self.client.close()
        for item in self.patches: item.stop()
        self.temp.cleanup()

    def photo(self):
        value = io.BytesIO()
        Image.new('RGB',(800,600),'orange').save(value,format='JPEG')
        return value.getvalue()

    def upload(self):
        response = self.client.post('/api/jobs',headers=self.headers,
            files=[('files',('../../evil.jpg',self.photo(),'image/jpeg'))])
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_upload_and_preview_are_portrait_and_local_token_required(self):
        response = self.client.post('/api/jobs',files=[('files',('p.jpg',self.photo(),'image/jpeg'))])
        self.assertEqual(response.status_code,403)
        job = self.upload()
        self.assertEqual(job['photos'][0]['file'],'photo-000.jpg')
        response = self.client.post(f'/api/jobs/{job["id"]}/preview',headers=self.headers,
                                    json={'text':'우리의 숙소','caption':{'y':13}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(Image.open(io.BytesIO(response.content)).size,(1080,1920))
        self.assertEqual(self.client.get(f'/api/jobs/{job["id"]}/files/job.json').status_code,404)

    def test_invalid_photo_rejected(self):
        response = self.client.post('/api/jobs',headers=self.headers,
            files=[('files',('p.jpg',b'not an image','image/jpeg'))])
        self.assertEqual(response.status_code,400)

    def test_photo_preview_shows_top_right_advertisement_without_captions(self):
        job = self.upload()
        response = self.client.post(f'/api/jobs/{job["id"]}/preview', headers=self.headers, json={'text': ''})
        self.assertEqual(response.status_code, 200)
        image = Image.open(io.BytesIO(response.content)).convert('RGB')
        corner = image.crop((820, 60, 1060, 180))
        self.assertGreater(max(high - low for low, high in corner.getextrema()), 100)
        opposite = image.crop((20, 60, 260, 180))
        self.assertLess(max(high - low for low, high in opposite.getextrema()), 10)

    def test_performance_endpoint_handles_old_jobs_and_returns_recorded_runs(self):
        from studio import performance
        job = self.upload()
        url = f'/api/jobs/{job["id"]}/performance'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['runs'], [])
        with performance.run(self.folder / job['id'], 'generate'):
            with performance.span('motion.submit'):
                pass
        response = self.client.get(url)
        run = response.json()['runs'][0]
        self.assertEqual(run['operation'], 'generate')
        self.assertEqual(run['status'], 'ok')
        self.assertEqual(self.client.get('/api/jobs/' + 'f'*32 + '/performance').status_code, 404)

    def test_photo_preview_keeps_the_tail_of_long_screen_captions(self):
        job = self.upload()
        prefix = '가' * 160
        first = self.client.post(f'/api/jobs/{job["id"]}/preview', headers=self.headers,
            json={'text': prefix + '나' * 20})
        second = self.client.post(f'/api/jobs/{job["id"]}/preview', headers=self.headers,
            json={'text': prefix + '다' * 20})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(hashlib.sha256(first.content).hexdigest(), hashlib.sha256(second.content).hexdigest())

    def test_manual_motion_is_independent_of_ai_count_and_invalidates_quote(self):
        from studio import store
        from studio.models import Job, CreateOptions, Storyboard
        record = Job(id='d' * 32, photos=[{'sha256': str(i)} for i in range(10)],
            options=CreateOptions(max_motion=0), state='awaiting_approval',
            storyboard=Storyboard(scenes=[{'photos': list(range(10)), 'text': '숙소'}]),
            quote={'id': 'old'}, approval={'quote_id': 'old'})
        store.save(record)
        response = self.client.post(f'/api/jobs/{record.id}/storyboard', headers=self.headers,
            json={'scenes': [{'photos': list(range(10)), 'text': '숙소'}],
                  'motion': [{'photo': 1, 'prompt': '  물결이 움직여요  '}, {'photo': 2, 'prompt': 'Slow pan'}]})
        self.assertEqual(response.status_code, 200, response.text)
        saved = store.read(record.id)
        self.assertEqual([m.photo for m in saved.storyboard.motion], [1, 2])
        self.assertEqual(saved.storyboard.motion[0].prompt, '물결이 움직여요')
        self.assertIsNone(saved.quote)
        self.assertIsNone(saved.approval)
        self.assertEqual(saved.state, 'uploaded')
        for count in (6, 10):
            response = self.client.post(f'/api/jobs/{record.id}/storyboard', headers=self.headers,
                json={'scenes': [{'photos': list(range(10)), 'text': '숙소'}],
                      'motion': [{'photo': i, 'prompt': f'Pan {i}'} for i in range(count)]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(store.read(record.id).storyboard.motion), count)

    def test_invalid_motion_edit_keeps_existing_plan(self):
        from studio import store
        from studio.models import Job, Storyboard
        record = Job(id='e' * 32, photos=[{'sha256': str(i)} for i in range(10)],
            storyboard=Storyboard(scenes=[{'photos': list(range(10)), 'text': '숙소'}]))
        store.save(record)
        for motion in ([{'photo': 0, 'prompt': '   '}],
                       [{'photo': 0, 'prompt': 'x' * 2001}],
                       [{'photo': 10, 'prompt': 'Pan'}],
                       [{'photo': 0, 'prompt': 'Pan'}, {'photo': 0, 'prompt': 'Pan'}]):
            response = self.client.post(f'/api/jobs/{record.id}/storyboard', headers=self.headers,
                json={'scenes': [{'photos': list(range(10)), 'text': '숙소'}], 'motion': motion})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(store.read(record.id).storyboard.motion, [])

    def test_external_host_rejected(self):
        self.assertEqual(self.client.get('/',headers={'Host':'evil.example'}).status_code,400)
        self.assertEqual(self.client.get('/',headers={'Host':'ts.net.evil.example'}).status_code,400)

    def test_tailscale_serve_host_allowed(self):
        self.assertEqual(self.client.get('/',headers={'Host':'friend-pc.tail1234.ts.net'}).status_code,200)

    def test_legacy_mcp_quote_allows_script_edit_before_paid_submission(self):
        from studio import store
        from studio.models import Job, Storyboard
        record = Job(id='d' * 32, photos=[{'sha256': 'photo'}],
                     storyboard=Storyboard(scenes=[{'photos': [0], 'text': '숙소'}]),
                     narration={'0': {'project_id': 'project', 'block': 'block'}})
        store.save(record)
        response = self.client.post(f'/api/jobs/{record.id}/storyboard', headers=self.headers,
                                    json={'scenes': [{'photos': [0], 'text': '새 대본'}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(store.read(record.id).narration, {})

    def test_uncertain_paid_submission_blocks_storyboard_edits(self):
        from studio import store
        from studio.models import Storyboard
        job = self.upload()
        record = store.read(job['id'])
        record.storyboard = Storyboard(scenes=[{'photos':[0], 'text':'숙소'}])
        record.generated = {'0':{'state':'submitting','idempotency_key':'persist-me'}}
        store.save(record)
        response = self.client.post(f'/api/jobs/{record.id}/storyboard',headers=self.headers,
            json={'scenes':[{'photos':[0],'text':'다른 대본'}]})
        self.assertEqual(response.status_code,400)
        self.assertEqual(store.read(record.id).generated['0']['idempotency_key'],'persist-me')

    def test_append_deduplicates_without_replacing_existing_photos(self):
        job = self.upload()
        response = self.client.post(f'/api/jobs/{job["id"]}/photos', headers=self.headers,
            files=[('files', ('same.jpg', self.photo(), 'image/jpeg'))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['photos']), 1)
        other = io.BytesIO()
        Image.new('RGB', (640, 800), 'blue').save(other, format='PNG')
        response = self.client.post(f'/api/jobs/{job["id"]}/photos', headers=self.headers,
            files=[('files', ('blue.png', other.getvalue(), 'image/png'))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([p['file'] for p in response.json()['photos']], ['photo-000.jpg', 'photo-001.jpg'])

    def test_invalid_append_keeps_existing_job_and_files(self):
        job = self.upload()
        response = self.client.post(f'/api/jobs/{job["id"]}/photos', headers=self.headers,
            files=[('files', ('bad.jpg', b'bad', 'image/jpeg'))])
        self.assertEqual(response.status_code, 400, response.text)
        current = self.client.get('/api/jobs/' + job['id']).json()
        self.assertEqual(current['photos'], job['photos'])
        self.assertEqual(len(list((self.folder / job['id']).glob('photo-*.jpg'))), 1)

    def test_analyzed_job_rejects_additional_photos(self):
        from studio import store
        from studio.models import Storyboard
        job = self.upload()
        record = store.read(job['id'])
        record.storyboard = Storyboard(scenes=[{'photos': [0], 'text': '숙소'}])
        store.save(record)
        response = self.client.post(f'/api/jobs/{record.id}/photos', headers=self.headers,
            files=[('files', ('same.jpg', self.photo(), 'image/jpeg'))])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(store.read(record.id).storyboard.scenes[0].text, '숙소')

    def test_enhance_requires_codex_login_and_valid_photos(self):
        job = self.upload()
        response = self.client.post(f'/api/jobs/{job["id"]}/enhance', headers=self.headers, json={'photos': None})
        self.assertEqual(response.status_code, 400)
        self.assertIn('로그인', response.json()['detail'])
        with patch('studio.app.codex.login_status', return_value=True):
            response = self.client.post(f'/api/jobs/{job["id"]}/enhance', headers=self.headers, json={'photos': [5]})
        self.assertEqual(response.status_code, 400)

    def test_enhance_launches_requested_photos(self):
        job = self.upload()
        with patch('studio.app.codex.login_status', return_value=True),              patch.object(module.pipeline, 'launch') as launch:
            response = self.client.post(f'/api/jobs/{job["id"]}/enhance', headers=self.headers, json={'photos': [0]})
        self.assertEqual(response.status_code, 202, response.text)
        launch.assert_called_once_with(job['id'], module.pipeline.enhance, [0])

    def test_photo_source_toggle_and_files(self):
        from studio import enhance, store
        from studio.models import Storyboard
        job = self.upload()
        record = store.read(job['id'])
        response = self.client.post(f'/api/jobs/{record.id}/photos/0/source', headers=self.headers, json={'source': 'enhanced'})
        self.assertEqual(response.status_code, 400)
        Image.new('RGB', (1080, 1920), 'teal').save(self.folder / record.id / 'photo-000-enhanced.jpg')
        enhance.record(record, 0, file='photo-000-enhanced.jpg')
        store.save(record)
        response = self.client.post(f'/api/jobs/{record.id}/photos/0/source', headers=self.headers, json={'source': 'original'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['photos'][0]['file'], 'photo-000.jpg')
        for name in ('photo-000.jpg', 'photo-000-enhanced.jpg'):
            self.assertEqual(self.client.get(f'/api/jobs/{record.id}/files/{name}').status_code, 200)
        image_url = f'/api/jobs/{record.id}/files/photo-000-enhanced.jpg'
        self.assertNotIn('content-disposition', self.client.get(image_url).headers)
        download = self.client.get(image_url + '?download=true')
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers.get('content-disposition'),
                         'attachment; filename="photo-000-enhanced.jpg"')
        self.assertEqual(download.content,
                         (self.folder / record.id / 'photo-000-enhanced.jpg').read_bytes())
        with Image.open(io.BytesIO(download.content)) as image:
            self.assertEqual(image.size, (1080, 1920))
        self.assertEqual(self.client.get(
            f'/api/jobs/{record.id}/files/job.json?download=true').status_code, 404)
        record = store.read(record.id)
        record.storyboard = Storyboard(scenes=[{'photos': [0], 'text': '숙소'}])
        store.save(record)
        response = self.client.post(f'/api/jobs/{record.id}/photos/0/source', headers=self.headers, json={'source': 'enhanced'})
        self.assertEqual(response.status_code, 400)
        with patch('studio.app.codex.login_status', return_value=True):
            response = self.client.post(f'/api/jobs/{record.id}/enhance', headers=self.headers, json={'photos': None})
        self.assertEqual(response.status_code, 400)

    def test_saved_fish_login_reconnects_without_browser(self):
        import asyncio
        from unittest.mock import AsyncMock
        saved = dict(module.connection)
        try:
            module.connection.update(fish=False, workspaces=[], fish_busy=False)
            value = {'workspaces': [{'workspace_id': 'w', 'workspace_name': '기본'}]}
            with patch.object(module.fish, 'call', AsyncMock(return_value=value)) as call:
                asyncio.run(module.restore_fish())
            call.assert_awaited_once_with('list_my_workspaces', {})
            self.assertTrue(module.connection['fish'])
            self.assertEqual(module.connection['workspaces'], value['workspaces'])
            self.assertFalse(module.connection['fish_busy'])
        finally:
            module.connection.clear(); module.connection.update(saved)

    def test_missing_or_expired_fish_login_stays_disconnected(self):
        import asyncio
        from unittest.mock import AsyncMock
        saved = dict(module.connection)
        try:
            module.connection.update(fish=False, workspaces=[], fish_busy=False, message='')
            failure = AsyncMock(side_effect=RuntimeError('Fish 계정을 먼저 연결해 주세요.'))
            with patch.object(module.fish, 'call', failure):
                asyncio.run(module.restore_fish())
            self.assertFalse(module.connection['fish'])
            self.assertFalse(module.connection['fish_busy'])
            self.assertEqual(module.connection['message'], '')
        finally:
            module.connection.clear(); module.connection.update(saved)

    def seed_collection(self):
        collection_id = 'c' * 32
        content = self.photo()
        digest = hashlib.sha256(content).hexdigest()
        root = self.folder / 'collections' / collection_id
        (root / 'images').mkdir(parents=True)
        (root / 'images' / (digest + '.jpg')).write_bytes(content)
        entry = {'sha256': digest, 'file': 'images/' + digest + '.jpg', 'bytes': len(content),
                 'width': 800, 'height': 600, 'format': 'JPEG', 'url': 'https://image.withstatic.com/a.jpg',
                 'labels': [{'category': 'property', 'title': '수영장', 'room_name': None}]}
        manifest = {'name': '테스트 숙소', 'source_url': 'https://www.yeogi.com/domestic-accommodations/9816',
                    'status': 'partial', 'images': [entry, dict(entry)],
                    'failures': [{'url': 'https://image.withstatic.com/b.jpg', 'error': 'HTTP 403'}]}
        (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return collection_id, digest

    def test_collection_gallery_is_unique_and_partial_images_can_be_imported(self):
        collection_id, digest = self.seed_collection()
        response = self.client.get('/api/collections/' + collection_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['photos']), 1)
        self.assertEqual(response.json()['status'], 'partial')
        response = self.client.post('/api/jobs/import', headers=self.headers,
            json={'collection_id': collection_id, 'photos': [digest]})
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        self.assertEqual(job['sources'][0]['name'], '테스트 숙소')
        self.assertEqual(len(job['photos']), 1)
        self.assertEqual(job['photos'][0]['labels'][0]['title'], '수영장')
        thumbnail = self.client.get(f'/api/collections/{collection_id}/photos/{digest}')
        self.assertEqual(thumbnail.status_code, 200)
        self.assertEqual(Image.open(io.BytesIO(thumbnail.content)).size, (320, 240))
        self.assertEqual(self.client.get(f'/api/collections/{collection_id}/photos/job.json').status_code, 404)

    def test_import_to_existing_job_deduplicates_and_invalid_selection_is_atomic(self):
        collection_id, digest = self.seed_collection()
        job = self.upload()
        response = self.client.post('/api/jobs/import', headers=self.headers,
            json={'collection_id': collection_id, 'photos': [digest], 'job_id': job['id']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['photos']), 1)
        response = self.client.post('/api/jobs/import', headers=self.headers,
            json={'collection_id': collection_id, 'photos': [digest, '0' * 64], 'job_id': job['id']})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(self.client.get('/api/jobs/' + job['id']).json()['photos']), 1)

    def test_collection_rejects_untrusted_links_before_starting_browser(self):
        for url in ['https://evil.test/a', 'http://127.0.0.1/', 'https://www.yeogi.com/overseas-accommodations/1']:
            response = self.client.post('/api/collections', headers=self.headers, json={'url': url})
            self.assertEqual(response.status_code, 400, response.text)

    def test_all_export_files_are_downloadable_but_original_voice_is_private(self):
        job = self.upload()
        root = self.folder / job['id']
        for name in ['stay-video.mp4', 'stay-voice.mp3', 'stay-script.txt']:
            (root / name).write_bytes(b'fixture')
            response = self.client.get(f'/api/jobs/{job["id"]}/files/{name}')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('attachment', response.headers['content-disposition'])
        (root / 'voice-000.mp3').write_bytes(b'private')
        self.assertEqual(self.client.get(f'/api/jobs/{job["id"]}/files/voice-000.mp3').status_code, 404)

    def test_sixty_photo_limit_is_combined_and_failed_batch_keeps_existing_photos(self):
        files = []
        for index in range(60):
            data = io.BytesIO()
            Image.new('RGB', (300, 300), (index, 50, 60)).save(data, format='PNG')
            files.append(('files', (f'{index}.png', data.getvalue(), 'image/png')))
        response = self.client.post('/api/jobs', headers=self.headers, files=files)
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        self.assertEqual(len(job['photos']), 60)
        response = self.client.post(f'/api/jobs/{job["id"]}/photos', headers=self.headers,
            files=[('files', ('extra.jpg', self.photo(), 'image/jpeg'))])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.client.get('/api/jobs/' + job['id']).json()['photos']), 60)
        self.assertFalse((self.folder / job['id'] / 'photo-060.jpg').exists())

    def test_low_resolution_in_append_batch_does_not_publish_valid_files_either(self):
        job = self.upload()
        valid, small = io.BytesIO(), io.BytesIO()
        Image.new('RGB', (640, 800), 'blue').save(valid, format='PNG')
        Image.new('RGB', (299, 800), 'blue').save(small, format='PNG')
        response = self.client.post(f'/api/jobs/{job["id"]}/photos', headers=self.headers,
            files=[('files', ('valid.png', valid.getvalue(), 'image/png')),
                   ('files', ('small.png', small.getvalue(), 'image/png'))])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(list((self.folder / job['id']).glob('photo-*.jpg'))), 1)

    def test_combined_byte_limit_uses_existing_photo_sizes(self):
        from studio import store
        job = self.upload()
        saved = store.read(job['id'])
        saved.photos[0]['bytes'] = 300 * 1024 * 1024
        store.save(saved)
        other = io.BytesIO()
        Image.new('RGB', (640, 800), 'blue').save(other, format='PNG')
        response = self.client.post(f'/api/jobs/{job["id"]}/photos', headers=self.headers,
            files=[('files', ('blue.png', other.getvalue(), 'image/png'))])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(store.read(job['id']).photos), 1)
