from __future__ import annotations

import asyncio
import hashlib
import shutil
import uuid
from pathlib import Path

from . import assets, codex, composition, domains, enhance, performance, recommendation, store
from .media import crop_photo
from .models import MIN_MOTION_SIDE, Scene, motion_fingerprint, stable_hash
from .paths import job_path
from .render import publish_preview, render, video_size


BATCH_EXPORTS = {'file': 'reel.mp4', 'video': 'video.mp4', 'audio': 'voice.mp3', 'script': 'script.txt', 'srt': 'reel.srt'}


def use_option_thumbnail(job, option):
    """Puts a script candidate's cover title and still on the job's thumbnail, keeping the title style."""
    if option.thumbnail_text:
        job.options.thumbnail.text = option.thumbnail_text
    if option.thumbnail_source is not None:
        job.options.thumbnail.source, job.options.thumbnail.time = option.thumbnail_source, option.thumbnail_time


async def parallel(items, action, limit):
    """Bound in-flight work and settle every submitted request before surfacing errors."""
    gate = asyncio.Semaphore(limit)
    async def one(item):
        async with gate:
            return await action(item)
    outcomes = await asyncio.gather(*(one(item) for item in items), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    return outcomes


def capability(motion):
    return 'i2v_ref' if len(motion.photos) > 1 else 'i2v'


def clip_parameters(job, motion):
    return dict(job.video_parameters, duration=f'{motion.seconds}s')


class Pipeline:
    def __init__(self, fish, drama):
        self.fish, self.drama = fish, drama
        self.tasks = {}

    def launch(self, job_id, operation, *args):
        if job_id in self.tasks and not self.tasks[job_id].done():
            raise ValueError('이 작업은 이미 실행 중입니다.')
        self.tasks[job_id] = asyncio.create_task(self.guarded(job_id, operation, *args))

    def busy(self, job_id):
        return job_id in self.tasks and not self.tasks[job_id].done()

    async def guarded(self, job_id, operation, *args):
        try:
            await operation(job_id, *args)
        except Exception as error:
            # The MCP client wraps failures like an expired login in a TaskGroup error; show the real one.
            while isinstance(error, ExceptionGroup) and error.exceptions:
                error = error.exceptions[0]
            job = store.read(job_id)
            job.state = 'error'
            job.error = str(error) or type(error).__name__
            job.message = '작업을 중단했습니다. 저장된 결과는 유지됩니다.'
            store.save(job)

    def update(self, job, state, message):
        job.state, job.message, job.error = state, message, None
        store.save(job)

    @performance.operation
    async def enhance(self, job_id, indices=None, preparing=False):
        """Remakes photos with Codex before analysis; each finished photo is saved as it lands."""
        job = store.read(job_id)
        if job.storyboard or job.generated or job.narration or job.result:
            raise ValueError('고화질 변환은 대본 분석 전에만 할 수 있습니다.')
        if indices is None:
            indices = [i for i, p in enumerate(job.photos) if (p.get('enhanced') or {}).get('state') != 'done']
        total, counts = len(indices), {'done': 0, 'failed': 0}
        for index in indices:
            job.photos[index]['enhancing'] = True
        state = 'preparing' if preparing else 'enhancing'
        prefix = '고화질 변환·대본 동시 작업' if preparing else '고화질 변환 중'
        self.update(job, state, f'{prefix} · 0/{total}장')
        folder = job_path(job_id)
        gate = asyncio.Semaphore(enhance.CONCURRENCY)
        domain = job.domain

        async def one(index):
            async with gate:
                photo = store.read(job_id).photos[index]
                original = photo.get('original_file', photo['file'])
                target = f'{Path(original).stem}-enhanced.jpg'
                try:
                    await performance.wait('photo.enhance', enhance.enhance_photo(folder / original, folder / target, domain=domain), photo=index)
                    outcome = {'file': target}
                except enhance.UsageLimit:
                    raise
                except enhance.EnhanceError as error:
                    outcome = {'error': str(error)}
                # No await between read and save, so concurrent photos never overwrite each other.
                job = store.read(job_id)
                enhance.record(job, index, **outcome)
                counts['done' if 'file' in outcome else 'failed'] += 1
                job.message = f'{prefix} · {counts["done"] + counts["failed"]}/{total}장'
                store.save(job)

        tasks = [asyncio.create_task(one(index)) for index in indices]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            job = store.read(job_id)
            enhance.settle(job)
            store.save(job)
            raise
        self.update(store.read(job_id), state if preparing else 'uploaded',
                    f'고화질 변환 완료 · 성공 {counts["done"]}장 · 실패 {counts["failed"]}장'
                    + (' · 대본 분석 완료 대기 중' if preparing else ''))

    @performance.operation
    async def prepare(self, job_id):
        """Analyze a stable photo snapshot while enhancements are saved independently."""
        job = store.read(job_id)
        domain = domains.load(job.domain)
        if domain.media != 'photo' or len(job.photos) < domain.min_media:
            raise ValueError(f'사진을 {domain.min_media}장 이상 준비해 주세요.')
        if job.domain == 'stay-silent' and len(job.photos) > 10:
            raise ValueError('음성X는 사용할 사진 8~10장을 먼저 골라 주세요.')
        assets.prepare_allowed(job)
        assets.capture(job)
        assets.checkpoint(job)
        job.storyboard = None
        job.generated, job.narration = {}, {}
        assets.invalidate(job)
        self.update(job, 'preparing', '고화질 변환과 대본 분석을 함께 시작합니다.')
        outcomes = await asyncio.gather(
            self.enhance(job_id, preparing=True),
            performance.wait('script.analyze', codex.analyze(job, removing_people=True)),
            return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        # Never save the analysis snapshot: it predates every completed enhancement.
        job = store.read(job_id)
        job.storyboard = outcomes[1]
        for focus in job.storyboard.crop_focus:
            photo = job.photos[focus.photo]
            if (photo.get('enhanced') or {}).get('active'):
                # Enhanced output is already 1080x1920; original-image coordinates don't apply.
                focus.x = focus.y = .5
        assets.invalidate(job)
        failed = sum((p.get('enhanced') or {}).get('state') == 'failed' for p in job.photos)
        self.update(job, 'uploaded', '고화질 사진과 대본을 준비했습니다.'
                    + (f' 변환 실패 {failed}장은 원본을 사용합니다.' if failed else ''))

    @performance.operation
    async def analyze(self, job_id):
        job = store.read(job_id)
        if assets.pending(job):
            raise ValueError('제출한 생성 작업을 먼저 완료하거나 복구해 주세요.')
        if job.workflow_version < 2 and (job.generated or assets.voice_started(job)):
            raise ValueError('생성 결과가 있는 작업은 대본을 다시 분석할 수 없습니다. 새 작업을 만들어 주세요.')
        self.update(job, 'analyzing', 'Codex가 영상을 보고 대본과 장면 구간, 자막 가림을 고르고 있습니다.' if domains.load(job.domain).media == 'video'
                    else 'Codex가 사진을 보고 대본과 움직임 장면을 고르고 있습니다.')
        assets.capture(job)
        assets.checkpoint(job)
        video = domains.load(job.domain).media == 'video'
        job.storyboard = await performance.wait('script.analyze', codex.analyze_video(job) if video else codex.analyze(job))
        if video and job.storyboard.script_options:
            use_option_thumbnail(job, job.storyboard.script_options[job.storyboard.script_choice])
        assets.restore(job)
        assets.invalidate(job)
        self.update(job, 'uploaded', '대본을 준비했습니다. 자막을 조절하고 크레딧 견적을 확인해 주세요.')

    @performance.operation
    async def recommend(self, job_id):
        job = store.read(job_id)
        self.update(job, 'recommending', domains.load(job.domain).text('recommend_progress', '전체 후보 사진을 평가하고 구성을 추천합니다.'))
        working = job.model_copy(deep=True)
        def progress(message):
            job.message = message
            store.save(job)
        selection = await recommendation.choose(working, progress)
        composition.select(working, selection['ids'])
        working.composition_mode = 'ai'
        # Stops at the pick so photos can be enhanced before the script is written from them.
        reasons = {r['id']: r['reason'] for r in selection['reasons']}
        working.selection_reasons = {p['sha256']: reasons[i] for i, p in zip(selection['ids'], working.photos)}
        self.update(working, 'uploaded', 'AI 추천 구성을 준비했습니다. 선정 이유를 확인하고 필요하면 고화질 변환 후 대본을 만들어 주세요.')

    @performance.operation
    async def quote(self, job_id, workspace):
        job = store.read(job_id)
        if not job.storyboard:
            raise ValueError('사진 분석을 먼저 실행해 주세요.')
        if job.domain == 'stay-silent':
            job.storyboard.validate_silent(len(job.photos))
        narrated = domains.load(job.domain).features.narration
        self.update(job, 'quoting', '움직임과 Drama3 음성의 실제 크레딧 견적을 확인하고 있습니다.' if narrated
                    else '움직임의 실제 크레딧 견적을 확인하고 있습니다. 음성 비용은 없습니다.')
        job.approval = None
        store.save(job)
        if job.workflow_version >= 2:
            assets.capture(job)
            assets.restore(job)
        if job.storyboard.motion:
            model = await self.fish.call('get_media_model', {'model_id': job.video_model})
            for motion in job.storyboard.motion:
                schema = model.get('capabilities', {}).get(capability(motion), {}).get('parameter_schema', {}).get('properties', {})
                if any(value not in schema.get(key, {}).get('enum', []) for key, value in clip_parameters(job, motion).items()):
                    raise RuntimeError(f'Fish 모델 {job.video_model}이 선택한 화질·비율·길이 또는 여러 사진 클립을 지원하지 않습니다. 영상 모델을 다시 골라 주세요.')
        folder = job_path(job.id)
        focus = {item.photo: (item.x, item.y) for item in job.storyboard.crop_focus}
        videos, voices = [], []
        for motion in job.storyboard.motion:
            index = str(motion.photo)
            saved = job.generated.get(index, {})
            if saved.get('idempotency_key') and saved.get('workspace') != workspace:
                raise RuntimeError('이미 제출한 생성 요청의 작업 공간은 바꿀 수 없습니다. 기존 작업 공간에서 재개해 주세요.')
            if saved.get('file') and (folder / saved['file']).exists():
                continue
            if saved.get('generation_id'):
                continue  # Already submitted: resume polling without purchasing it again.
            images = []
            for photo in motion.photos:
                image = folder / f'motion-source-{photo:03}.jpg'
                crop_photo(folder / job.photos[photo]['file'], *focus.get(photo, (0.5, 0.5))).save(image, quality=96)
                images.append(image)
            hashes = [hashlib.sha256(image.read_bytes()).hexdigest() for image in images]
            parameters = clip_parameters(job, motion)
            fingerprint = motion_fingerprint(hashes[0] if len(hashes) == 1 else stable_hash(hashes), motion.prompt,
                                             job.video_model, parameters)
            if saved.get('fingerprint') != fingerprint or saved.get('workspace') != workspace:
                keys = [await performance.wait('motion.upload', self.fish.upload(image, workspace), photo=motion.photo) for image in images]
                saved = {'fingerprint': fingerprint, 'workspace': workspace,
                         'asset_key': assets.cache_key(job, assets.motion_key(job, motion)),
                         'photo_sha': job.photos[motion.photo]['sha256'], 'prompt': motion.prompt,
                         'object_key': keys[0], 'object_keys': keys}
                job.generated[index] = saved
                store.save(job)
            request = {'workspace_id': workspace, 'model_id': job.video_model,
                       'parameters': parameters, 'prompt': motion.prompt}
            if len(motion.photos) > 1:
                request['inputs'] = [{'role': 'ref', 'object_key': key} for key in saved['object_keys']]
            else:
                request.update(operation='i2v', input_object_key=saved['object_key'])
            value = await performance.wait('motion.quote', self.fish.call('estimate_video_generation', request), photo=motion.photo)
            if not value.get('can_generate'):
                raise RuntimeError('Fish 움직임 생성에 필요한 크레딧이 부족합니다.')
            videos.append({'photo': motion.photo, 'request': request, 'credits': value['credits'],
                           'pricing_version': value['pricing_version'], 'balance': value.get('balance')})
        for scene_index, scene in enumerate(job.storyboard.scenes if narrated else []):
            saved = job.narration.get(str(scene_index), {})
            if saved.get('file') and (folder / saved['file']).exists():
                continue
            if saved.get('state') == 'submitted' and not saved.get('url'):
                if saved.get('project_id') and saved.get('block'):
                    continue  # Recover the submitted MCP block without another purchase.
                raise RuntimeError('응답을 확인하지 못한 기존 브라우저 음성이 있습니다. Fish History 음성을 복구해 주세요.')
            if saved.get('url'):
                continue
            asset_key = assets.cache_key(job, assets.voice_key(job, scene))
            saved = job.narration.setdefault(str(scene_index), {})
            saved.update(asset_key=asset_key, text=scene.text)
            async def persist(data):
                saved.update(data)
                assets.capture(job)
                store.save(job)
            name = 'Stay Studio ' + job.id + ' ' + stable_hash(asset_key)
            voice_quote = await performance.wait('voice.quote', self.drama.quote(scene.text, name, saved, persist), scene=scene_index)
            voices.append({**voice_quote, 'scene': scene_index, 'asset_key': asset_key})
        assets.capture(job)
        total = sum(item['credits'] for item in videos + voices)
        balances = [item['balance'] for item in videos + voices if item.get('balance') is not None]
        if balances and total > min(balances):
            raise RuntimeError('영상과 음성 전체 견적이 남은 Fish 크레딧보다 큽니다.')
        job.quote = {'id': uuid.uuid4().hex, 'total': total, 'videos': videos, 'voices': voices,
                     'workspace': workspace, 'plan_hash': store.plan_hash(job)}
        self.update(job, 'awaiting_approval', '견적을 확인했습니다. 승인하면 Fish 크레딧을 사용해 생성합니다.')

    @performance.operation
    async def generate(self, job_id):
        job = store.read(job_id)
        store.generation_allowed(job)
        narrated = domains.load(job.domain).features.narration
        self.update(job, 'generating', '승인한 크레딧으로 움직임과 Drama3 음성을 만들고 있습니다.' if narrated
                    else '승인한 크레딧으로 움직임을 만들고 있습니다. 음성 없이 자막으로 합성합니다.')
        folder = job_path(job.id)
        for item in job.quote['videos']:
            key = str(item['photo'])
            saved = job.generated[key]
            if saved.get('generation_id') or saved.get('file'):
                continue
            idempotency = saved.setdefault('idempotency_key', stable_hash({'job': job.id, 'asset': saved.get('asset_key', saved['fingerprint'])}))
            saved['state'] = 'submitting'
            store.save(job)
            value = await performance.wait('motion.submit', self.fish.call('generate_video', {**item['request'],
                'expected_credits': item['credits'], 'pricing_version': item['pricing_version'],
                'idempotency_key': idempotency}), photo=item['photo'])
            generation_id = value.get('generation_id') or value.get('generation', {}).get('generation_id')
            if not generation_id:
                raise RuntimeError('Fish 생성 ID를 확인하지 못했습니다. 저장한 동일 요청 키로만 재개할 수 있습니다.')
            saved.update(generation_id=generation_id, state='submitted')
            store.save(job)
        # Videos render on Fish servers for minutes, so voices are made meanwhile instead of after.
        async def video(motion):
            key = str(motion.photo)
            saved = job.generated[key]
            if saved.get('file') and (folder / saved['file']).exists():
                return
            generation_id = saved.get('generation_id')
            if not generation_id:
                raise RuntimeError('움직임 생성 ID가 없습니다. 견적 단계로 돌아가 주세요.')
            for _ in range(120):
                value = await performance.wait('motion.status', self.fish.call('get_generation_status', {'generation_id': generation_id}), photo=motion.photo)
                status = value.get('status')
                if status in {'failed', 'cancelled'}:
                    saved['state'] = status
                    assets.capture(job)
                    store.save(job)
                    raise RuntimeError('Fish 움직임 생성이 실패했습니다. 저장된 ID: ' + generation_id)
                if status in {'completed', 'succeeded', 'finished'}:
                    result = await performance.wait('motion.result', self.fish.call('get_generation_result', {'generation_id': generation_id}), photo=motion.photo)
                    results = result.get('results', [])
                    media = result.get('media') or (results[0].get('media', results[0]) if results else {})
                    url = media.get('url')
                    if not url:
                        raise RuntimeError('완료된 Fish 결과의 다운로드 주소를 찾지 못했습니다.')
                    saved.update(url=url, state='completed')
                    store.save(job)
                    output = folder / (f"motion-{stable_hash(saved['asset_key'])}.mp4" if job.workflow_version >= 2 else f'motion-{motion.photo:03}.mp4')
                    await performance.wait('motion.download', self.fish.download(url, output), photo=motion.photo)
                    if min(await asyncio.to_thread(video_size, output)) < MIN_MOTION_SIDE:
                        raise RuntimeError(f'Fish 결과가 {MIN_MOTION_SIDE}p보다 작습니다. 이 결과는 영상에 사용하지 않습니다.')
                    saved['file'] = output.name
                    store.save(job)
                    break
                await performance.wait('motion.poll_wait', asyncio.sleep(min(max(value.get('poll_after_seconds', 60), 5), 60)), photo=motion.photo)
            else:
                raise RuntimeError('움직임 생성 대기 시간이 초과되었습니다. 저장된 ID로 재개할 수 있습니다.')

        async def voice(scene_index):
            scene = job.storyboard.scenes[scene_index]
            key = str(scene_index)
            saved = job.narration.setdefault(key, {})
            if saved.get('file') and (folder / saved['file']).exists():
                return
            asset_key = assets.cache_key(job, assets.voice_key(job, scene))
            if job.workflow_version >= 2 and job.assets.get(asset_key, {}).get('file'):
                job.narration[key] = dict(job.assets[asset_key])
                return
            saved['asset_key'] = asset_key
            saved['text'] = scene.text
            output = folder / (f'voice-{stable_hash(asset_key)}.mp3' if job.workflow_version >= 2 else f'voice-{scene_index:03}.mp3')
            async def submitted(data):
                saved.update(data)
                assets.capture(job)
                store.save(job)
            if saved.get('url'):
                await performance.wait('voice.download', self.fish.download(saved['url'], output), scene=scene_index)
            elif saved.get('state') == 'submitted':
                if not saved.get('project_id') or not saved.get('block'):
                    raise RuntimeError('중단된 음성 생성은 자동 재요청하지 않습니다. Fish History 음성을 복구해 주세요.')
                await performance.wait('voice.recover', self.drama.recover(saved, output, submitted), scene=scene_index)
            else:
                item = next(item for item in job.quote['voices'] if item['scene'] == scene_index)
                await performance.wait('voice.generate', self.drama.generate(scene.text, item['credits'], output, submitted, item), scene=scene_index)
            saved['file'] = output.name
            assets.capture(job)
            store.save(job)

        async def voices():
            if not narrated:
                return
            groups = {}
            for index, scene in enumerate(job.storyboard.scenes):
                key = assets.cache_key(job, assets.voice_key(job, scene)) if job.workflow_version >= 2 else str(index)
                groups.setdefault(key, []).append(index)
            async def group(indices):
                def priority(index):
                    record = job.narration.get(str(index), {})
                    return (record.get('state') == 'submitted',
                            bool(record.get('file') and (folder / record['file']).is_file()),
                            bool(record.get('url')))
                leader = max(indices, key=priority)
                record = job.narration.setdefault(str(leader), {})
                # Alias before submission: capture() must not overwrite a pending shared
                # asset with another scene's older, merely quoted record.
                for index in indices:
                    job.narration[str(index)] = record
                await voice(leader)
                for index in indices:
                    job.narration[str(index)] = dict(job.narration[str(leader)])
                assets.capture(job)
                store.save(job)
            await parallel(groups.values(), group, 3)

        # Both run to the end even if one fails, so no paid request is cut off mid-way.
        outcomes = await asyncio.gather(
            performance.wait('motion.total', parallel(job.storyboard.motion, video, 3)),
            performance.wait('voice.total', voices()), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        assets.capture(job)
        store.save(job)
        if job.workflow_version >= 2:
            await self.preview(job_id)
        else:
            await self.render(job_id)

    async def batch_quote(self, job_id, workspace):
        """Free Drama3 quote for every script candidate's voices; lines already voiced are not bought again."""
        job = store.read(job_id)
        options = job.storyboard.script_options if job.storyboard else []
        if len(options) < 2:
            raise ValueError('대본 후보가 2개 이상일 때 여러 편을 만들 수 있어요.')
        previous = job.state if job.state not in {'error', 'interrupted'} else 'uploaded'
        self.update(job, 'quoting', f'대본 후보 {len(options)}개의 Drama3 음성 견적을 확인하고 있습니다.')
        folder = job_path(job.id)
        voices = dict(job.batch.get('voices', {}))  # Voice asset key -> its Story Studio block record.
        items, seen = [], set()
        for option in options:
            for text in option.texts:
                key = assets.cache_key(job, assets.voice_key(job, Scene(photos=[0], text=text)))
                cached = job.assets.get(key, {})
                if key in seen or (cached.get('file') and (folder / cached['file']).is_file()):
                    continue
                seen.add(key)
                record = voices.setdefault(key, {})
                async def persist(data, record=record):
                    record.update(data)
                    job.batch['voices'] = voices
                    store.save(job)
                name = 'Stay Studio ' + job.id + ' ' + stable_hash(key)
                quoted = await performance.wait('voice.quote', self.drama.quote(text, name, record, persist))
                items.append({**quoted, 'key': key})
        total = sum(item['credits'] for item in items)
        balances = [item['balance'] for item in items if item.get('balance') is not None]
        if balances and total > min(balances):
            raise RuntimeError('영상 여러 편의 음성 견적이 남은 Fish 크레딧보다 큽니다.')
        job.batch = {'id': uuid.uuid4().hex, 'total': total, 'items': items, 'voices': voices, 'workspace': workspace,
                     'texts': [option.texts for option in options], 'state': 'awaiting_approval', 'results': []}
        self.update(job, previous, f'영상 {len(options)}편 견적: {total:,} 크레딧. 승인하면 음성을 만들고 영상을 뽑습니다.')

    @performance.operation
    async def batch_generate(self, job_id, quote_id, expected_credits):
        """Buys the quoted voices, then renders one final reel per script candidate with its own cover."""
        job = store.read(job_id)
        batch, options = job.batch, job.storyboard.script_options if job.storyboard else []
        if batch.get('id') != quote_id or batch.get('total') != expected_credits:
            raise ValueError('견적이 바뀌었습니다. 영상 여러 편 견적을 다시 확인해 주세요.')
        if batch.get('texts') != [option.texts for option in options]:
            raise ValueError('대본이 바뀌었습니다. 영상 여러 편 견적을 다시 확인해 주세요.')
        previous = job.state if job.state not in {'error', 'interrupted'} else 'uploaded'
        self.update(job, 'generating', f'승인한 크레딧으로 대본 {len(options)}개의 Drama3 음성을 만들고 있습니다.')
        folder = job_path(job.id)

        async def voice(item):
            key, record = item['key'], batch['voices'][item['key']]
            output = folder / f'voice-{stable_hash(key)}.mp3'
            async def submitted(data):
                record.update(data)
                store.save(job)
            if not output.is_file():
                if record.get('state') == 'submitted':  # Paid before a restart: collect it, never buy it twice.
                    await self.drama.recover(record, output, submitted)
                else:
                    await self.drama.generate(item['text'], item['credits'], output, submitted, item)
            job.assets[key] = {**record, 'file': output.name, 'asset_key': key}
            store.save(job)
        await parallel(batch['items'], voice, 3)
        results = []
        for index, option in enumerate(options):
            variant = job.model_copy(deep=True)
            variant.storyboard.script_choice = index
            for scene, text in zip(variant.storyboard.scenes, option.texts):
                scene.text = text
            variant.narration = {str(i): dict(job.assets[assets.cache_key(job, assets.voice_key(job, scene))])
                                 for i, scene in enumerate(variant.storyboard.scenes)}
            use_option_thumbnail(variant, option)
            def progress(message, number=index + 1):
                job.message = f'{number}/{len(options)}편 · {message}'
                store.save(job)
            self.update(job, 'rendering', f'{index + 1}/{len(options)}편을 1080p로 합성하고 있습니다.')
            output = await asyncio.to_thread(render, variant, progress, True)
            files = {}
            for kind, name in BATCH_EXPORTS.items():
                if output.get(kind):
                    files[kind] = f'option-{index + 1}-{name}'
                    shutil.copyfile(folder / output[kind], folder / files[kind])
            results.append({'title': option.title, 'duration': output['duration'], 'warnings': output['warnings'], **files})
        job.batch.update(state='complete', results=results)
        self.update(job, previous, f'대본 후보 {len(results)}개로 영상 {len(results)}편을 만들었습니다. 아래에서 저장하세요.')

    @performance.operation
    async def preview(self, job_id):
        job = store.read(job_id)
        if not assets.ready(job, job_path(job.id)):
            raise ValueError('미리보기에 필요한 음성·움직임을 먼저 생성해 주세요.')
        revision = assets.render_revision(job)
        self.update(job, 'previewing', '음성·움직임·자막을 1080p 미리보기로 합성하고 있습니다.'
                    if domains.load(job.domain).features.narration else '움직임·자막을 음성 없이 1080p 미리보기로 합성하고 있습니다.')
        def progress(message):
            job.message = message
            store.save(job)
        if not job.preview or job.preview.get('revision') != revision or not (job_path(job.id) / job.preview['file']).is_file():
            job.preview = await asyncio.to_thread(render, job, progress, True)
            job.preview['revision'] = revision
        self.update(job, 'preview_ready', '1080p 미리보기를 확인한 뒤 최종 영상을 내보내세요.')

    @performance.operation
    async def render(self, job_id):
        job = store.read(job_id)
        if job.workflow_version >= 2:
            if not job.preview or job.preview.get('revision') != assets.render_revision(job):
                raise ValueError('변경 내용을 반영한 미리보기를 먼저 만들어 주세요.')
            self.update(job, 'rendering', '확인한 미리보기를 최종 파일로 내보내고 있습니다.')
            job.result = await performance.wait('export.copy', asyncio.to_thread(publish_preview, job))
            self.update(job, 'complete', '확인한 1080×1920 미리보기를 최종 영상으로 내보냈습니다.')
            return
        self.update(job, 'rendering', '저장된 영상과 음성을 합성하고 있습니다. 추가 크레딧을 사용하지 않습니다.')
        def progress(message):
            job.message = message
            store.save(job)
        job.result = await asyncio.to_thread(render, job, progress)
        self.update(job, 'complete', '1080×1920 영상이 완성되었습니다.')
