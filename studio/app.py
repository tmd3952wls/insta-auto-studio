from __future__ import annotations

import asyncio
import hashlib
import io
import json
import secrets
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from PIL import Image, ImageOps, UnidentifiedImageError

from . import assets, codex, collector, composition, domains, enhance, performance, recommendation, store, video_models
from .drama import Drama
from .fish import Fish
from .media import badge_image, caption_image, caption_segments, crop_photo, crop_video_frame, preview_image
from .models import SCRIPT_CHARS, ApproveRequest, BadgeStyle, CaptionPresetRequest, CaptionStyle, Thumbnail, CreateOptions, CropBox, CropFocus, Job, Storyboard
from .paths import DATA, RESOURCES, initialize, job_path
from .pipeline import Pipeline

TOKEN = secrets.token_urlsafe(32)
EXPORTS = {'stay-reel.mp4': 'file', 'stay-video.mp4': 'video', 'stay-script.txt': 'script',
           'stay-reel.srt': 'srt', 'stay-voice.mp3': 'audio'}  # Download name -> result/preview key.


def style_file():
    from . import paths  # Read at call time so the data folder can be redirected.
    return paths.DATA / 'style.json'  # Last saved caption style and speed; new jobs start from it.


def saved_options():
    try:
        saved = json.loads(style_file().read_text(encoding='utf-8'))
        return CreateOptions(caption=saved['caption'], speed=saved['speed'],
                             **({'badge': saved['badge']} if 'badge' in saved else {}),
                             **({'thumbnail': {'style': saved['thumbnail_style']}} if 'thumbnail_style' in saved else {}))
    except (OSError, ValueError, KeyError, TypeError):
        return CreateOptions()


def new_job(domain='stay', **values):
    domains.load(domain)  # Rejects an unknown domain before anything is saved.
    options = saved_options()
    if domain == 'stay-silent':
        options.caption = CaptionStyle(color='#FFFFFF', align='left', x=-35, y=-30)
    return Job(id=uuid.uuid4().hex, options=options, domain=domain, **values)


def link_collection(job_or_domain):
    domain = domains.load(job_or_domain if isinstance(job_or_domain, str) else job_or_domain.domain)
    if not domain.features.link_collection:
        raise ValueError(f'{domain.label} 작업에서는 링크로 사진을 가져올 수 없습니다. 사진을 직접 넣어 주세요.')


def export_files(job):
    """Newest file on disk per download: the last final export, or a later preview when there is one."""
    folder, found = job_path(job.id), {}
    for name, key in EXPORTS.items():
        if key == 'audio' and not domains.load(job.domain).features.narration:
            continue
        paths = [folder / name] + ([folder / job.preview[key]] if job.preview and job.preview.get(key) else [])
        paths = [path for path in paths if path.is_file()]
        if paths:
            found[name] = max(paths, key=lambda path: path.stat().st_mtime).name
    return found
fish = Fish()
drama = Drama(fish)
pipeline = Pipeline(fish, drama)
pending_login = DATA / 'auth/codex-login-message.txt'
connection = {'fish': False, 'workspaces': [], 'message': '',
              'codex_login': pending_login.read_text(encoding='utf-8') if pending_login.exists() else ''}
background = set()


def error_description(error):
    if isinstance(error, BaseExceptionGroup):
        return '; '.join(error_description(item) for item in error.exceptions)
    import re
    message = re.sub(r'https?://[^\s)]+', lambda m: m.group().split('?')[0], str(error))
    return (type(error).__name__ + ': ' + (message if len(message)<2200 else message[:300]+' … '+message[-1800:]))


@asynccontextmanager
async def lifespan(app):
    initialize()
    from . import paths
    (paths.DATA / 'domains').mkdir(parents=True, exist_ok=True)  # Where new or overriding short-form kinds go.
    domains.reload()  # Logs any domain folder that cannot be used; the others still load.
    store.recover()
    collector.recover()
    spawn(restore_fish())
    yield
    tasks = list(background) + list(pipeline.tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver', '*.ts.net'])  # *.ts.net: 휴대폰 접속용 tailscale serve
app.mount('/static', StaticFiles(directory=RESOURCES / 'studio/web'), name='static')


@app.middleware('http')
async def local_security(request: Request, call_next):
    if request.method not in {'GET', 'HEAD'}:
        if not secrets.compare_digest(request.headers.get('X-Studio-Token', ''), TOKEN):
            return Response('Invalid local session', status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(ValueError)
async def invalid(request, error):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail': str(error)}, status_code=400)


@app.exception_handler(FileNotFoundError)
async def missing(request, error):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail': '저장된 작업이나 파일을 찾지 못했습니다.'}, status_code=404)


@app.get('/', response_class=HTMLResponse)
async def index():
    return (RESOURCES / 'studio/web/index.html').read_text(encoding='utf-8').replace('__TOKEN__', TOKEN)


@app.get('/font')
async def font():
    return FileResponse(RESOURCES / 'assets/fonts/Paperlogy-7Bold.ttf', media_type='font/ttf')


def spawn(coroutine):
    task = asyncio.create_task(coroutine)
    background.add(task)
    task.add_done_callback(background.discard)


@app.get('/api/connections')
async def connections():
    try:
        connected = await codex.login_status()
    except (OSError, RuntimeError):
        connected = False
    return {**connection, 'codex': connected,
            'fish_login_url': fish.authorization_url if connection.get('fish_busy') else None}


@app.post('/api/connect/codex')
async def connect_codex():
    if connection.get('codex_busy'):
        return {'ok': True}
    connection['codex_busy'] = True
    connection['codex_login'] = '로그인 진행 중'
    async def work():
        try:
            process = await codex.login()
            lines = []
            async for line in process.stdout:
                lines.append(line.decode('utf-8', 'replace'))
                connection['codex_login'] = ''.join(lines)[-3000:]
                pending_login.write_text(connection['codex_login'], encoding='utf-8')
            await process.wait()
        except Exception as error:
            connection['codex_login'] = str(error)
        finally:
            connection['codex_busy'] = False
    spawn(work())
    return {'ok': True}


async def restore_fish():
    """Reconnects with the saved Fish token at startup; never opens a browser."""
    if connection.get('fish') or connection.get('fish_busy'):
        return
    connection['fish_busy'] = True
    try:
        value = await fish.call('list_my_workspaces', {})
        connection.update(fish=True, workspaces=value.get('workspaces', []), message='저장된 Fish 계정으로 연결했습니다.')
    except Exception:
        pass  # No saved or expired token: the user connects once from settings as before.
    finally:
        connection['fish_busy'] = False


@app.post('/api/connect/fish')
async def connect_fish():
    if connection.get('fish_busy'):
        return {'ok': True}
    connection['fish_busy'] = True
    async def work():
        try:
            connection['message'] = '브라우저에서 Fish 계정 연결을 완료해 주세요.'
            value = await fish.connect()
            connection.update(fish=True, workspaces=value.get('workspaces', []), message='Fish MCP 연결 완료')
        except Exception as error:
            connection['message'] = 'Fish 연결에 실패했습니다. ' + error_description(error)
        finally:
            connection['fish_busy'] = False
    spawn(work())
    return {'ok': True}


@app.get('/oauth/callback', response_class=HTMLResponse)
async def oauth_callback(code: str, state: str | None = None, iss: str | None = None):
    fish.receive_callback(code, state, iss)
    return '<meta charset="utf-8"><p>계정 연결을 처리하고 있습니다. Stay Studio 창으로 돌아가 주세요.</p>'


@app.post('/api/connect/drama')
async def connect_drama():
    # Compatibility for older tabs: both features now share Fish OAuth.
    return await connect_fish()


@app.get('/api/jobs')
async def jobs():
    return [{'id': job.id, 'state': job.state, 'photos': len(job.photos), 'message': job.message,
             'name': ' / '.join(source['name'] for source in job.sources), 'domain': job.domain}
            for job in store.list_jobs()]


@app.get('/api/domains')
async def list_domains():
    return [domains.public(domain) for domain in domains.all()]


@app.get('/api/domains/problems')
async def domain_problems():
    return domains.problems()


@app.post('/api/jobs')
async def upload(files: list[UploadFile] = File(...), domain: str = Form('stay')):
    job = new_job(domain, workflow_version=2)
    if domains.load(domain).media == 'video':
        return (await add_videos(job, files)).model_dump()
    return add_photos(job, await read_uploads(files, domains.load(domain).max_media)).model_dump()


VIDEO_TYPES = {'.mp4', '.mov', '.m4v', '.webm'}
VIDEO_BYTES, VIDEOS_BYTES = 300 * 1024 * 1024, 1536 * 1024 * 1024


async def add_videos(job, files):
    """Streams uploaded source videos into the job (video domains), with a thumbnail and a default crop each."""
    from .render import video_frame, video_info
    photos_editable(job)
    domain = domains.load(job.domain)
    folder = job_path(job.id)
    folder.mkdir(parents=True, exist_ok=True)
    seen = {photo['sha256'] for photo in job.photos}
    total = sum(photo.get('bytes', 0) for photo in job.photos)
    added = []
    with tempfile.TemporaryDirectory(prefix='.videos-', dir=folder) as directory:
        staging = Path(directory)
        for file in files:
            suffix = Path(file.filename or '').suffix.lower()
            if suffix not in VIDEO_TYPES:
                raise ValueError('MP4, MOV, WebM 영상을 넣어 주세요.')
            index = len(job.photos) + len(added)
            name, thumb = f'source-{index:03}{suffix}', f'source-{index:03}.jpg'
            digest, size = hashlib.sha256(), 0
            with open(staging / name, 'wb') as target:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > VIDEO_BYTES or total + size > VIDEOS_BYTES:
                        raise ValueError('영상 용량이 너무 큽니다. 영상당 300MB, 전체 1.5GB 이내로 넣어 주세요.')
                    digest.update(chunk)
                    target.write(chunk)
            sha = digest.hexdigest()
            if sha in seen:
                continue
            if len(job.photos) + len(added) >= domain.max_media:
                raise ValueError(f'영상은 최대 {domain.max_media}개까지 넣을 수 있습니다.')
            try:
                width, height, duration = await asyncio.to_thread(video_info, staging / name)
                await asyncio.to_thread(video_frame, staging / name, min(1.0, duration / 2), staging / thumb, 720)
            except (ValueError, RuntimeError) as error:
                raise ValueError(f'읽을 수 없는 영상 파일이 있습니다: {Path(file.filename or name).name}') from error
            if duration < 1 or min(width, height) < 240:
                raise ValueError('영상은 1초 이상, 짧은 변 240px 이상이어야 합니다.')
            total += size
            seen.add(sha)
            added.append({'kind': 'video', 'name': Path(file.filename or name).name, 'file': name, 'thumb': thumb,
                          'sha256': sha, 'width': width, 'height': height, 'duration': round(duration, 3),
                          'bytes': size, 'crop': CropBox().model_dump()})
        if not job.photos and not added:
            raise ValueError('추가할 영상을 선택해 주세요.')
        for video in added:
            (staging / video['file']).replace(folder / video['file'])
            (staging / video['thumb']).replace(folder / video['thumb'])
    previous = len(job.photos)
    job.photos.extend(added)
    job.photo_order = (job.photo_order or list(range(previous))) + list(range(previous, len(job.photos)))
    job.state, job.error = 'uploaded', None
    job.message = f'영상 {len(job.photos)}개를 준비했습니다. 새 영상 {len(added)}개 추가.'
    store.save(job)
    return job


@app.post('/api/jobs/{job_id}/videos')
async def append_videos(job_id: str, files: list[UploadFile] = File(...)):
    job = store.read(job_id)
    if domains.load(job.domain).media != 'video':
        raise ValueError('이 작업에는 사진을 넣어 주세요.')
    return (await add_videos(job, files)).model_dump()


@app.post('/api/jobs/{job_id}/sources/{index}/crop')
async def source_crop(job_id: str, index: int, value: dict):
    job = store.read(job_id)
    editable(job)
    if not 0 <= index < len(job.photos) or job.photos[index].get('kind') != 'video':
        raise ValueError('영상 번호가 올바르지 않습니다.')
    crop = CropBox.model_validate(dict(value, user=True)).model_dump()
    if crop != job.photos[index].get('crop'):
        assets.capture(job)
        job.photos[index]['crop'] = crop
        if job.storyboard:
            assets.invalidate(job)
        store.save(job)
    return job.model_dump()


@app.post('/api/jobs/{job_id}/facts')
async def save_facts(job_id: str, value: dict):
    job = store.read(job_id)
    editable(job)
    allowed = {prop.name for _, prop in domains.load(job.domain).facts()}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('이 작업에 저장할 수 없는 정보가 있습니다.')
    if any(not isinstance(text, str) or len(text.strip()) > (SCRIPT_CHARS if key == 'script' else 300)
           for key, text in value.items()):
        raise ValueError(f'입력한 정보는 항목마다 300자, 내 대본은 {SCRIPT_CHARS}자 이내로 적어 주세요.')
    job.facts = {key: text.strip() for key, text in value.items() if text.strip()}
    store.save(job)
    return job.model_dump()


async def read_uploads(files, limit=60):
    if not 1 <= len(files) <= limit:
        raise ValueError(f'사진은 1~{limit}장까지 넣어 주세요.')
    inputs = []
    total = 0
    for file in files:
        data = await file.read(20 * 1024 * 1024 + 1)
        total += len(data)
        if len(data) > 20 * 1024 * 1024 or total > 300 * 1024 * 1024:
            raise ValueError('사진 용량이 너무 큽니다. 사진당 20MB, 전체 300MB 이내로 넣어 주세요.')
        inputs.append((data, {'name': Path(file.filename or '사진').name}))
    return inputs


def photos_editable(job):
    if pipeline.busy(job.id) or job.storyboard or job.generated or job.narration or job.result:
        raise ValueError('사진은 대본 분석 전에 추가할 수 있습니다. 새 작업을 만들어 주세요.')


def add_photos(job, inputs, source_metadata=None):
    photos_editable(job)
    folder = job_path(job.id)
    folder.mkdir(parents=True, exist_ok=True)
    seen = {photo['sha256'] for photo in job.photos}
    total = sum(photo.get('bytes', (folder / photo['file']).stat().st_size) for photo in job.photos)
    added = []
    # Validate the full batch in a temporary folder before changing the existing job.
    with tempfile.TemporaryDirectory(prefix='.photos-', dir=folder) as directory:
        staging = Path(directory)
        for data, metadata in inputs:
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                continue
            total += len(data)
            if len(data) > 20 * 1024 * 1024 or total > 300 * 1024 * 1024:
                raise ValueError('사진 용량이 너무 큽니다. 사진당 20MB, 전체 300MB 이내로 넣어 주세요.')
            limit = domains.load(job.domain).max_media
            if len(job.photos) + len(added) >= limit:
                raise ValueError(f'첨부 사진과 링크 사진을 합쳐 최대 {limit}장까지 넣을 수 있습니다.')
            name = f'photo-{len(job.photos) + len(added):03}.jpg'
            try:
                with Image.open(io.BytesIO(data)) as original:
                    if not source_metadata and original.format not in {'JPEG', 'PNG', 'WEBP'}:
                        raise ValueError('JPG, PNG, WebP 사진을 넣어 주세요.')
                    image = ImageOps.exif_transpose(original).convert('RGB')
                    if min(image.size) < 300:
                        raise ValueError('사진의 짧은 변이 300px 이상이어야 합니다.')
                    image.save(staging / name, quality=97)
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
                raise ValueError('읽을 수 없는 사진 파일이 있습니다.') from error
            added.append(dict(metadata, file=name, sha256=digest, width=image.width, height=image.height, bytes=len(data)))
            seen.add(digest)
        if not job.photos and not added:
            raise ValueError('추가할 사진을 선택해 주세요.')
        for photo in added:
            (staging / photo['file']).replace(folder / photo['file'])
    previous_count = len(job.photos)
    job.photos.extend(added)
    if job.workflow_version >= 2:
        job.photo_order = (job.photo_order or list(range(previous_count))) + list(range(previous_count, len(job.photos)))
        job.candidates = recommendation.candidates(job)
    if source_metadata and not any(s['url'] == source_metadata['url'] for s in job.sources):
        job.sources.append(source_metadata)
    job.state, job.error = 'uploaded', None
    job.message = f'사진 {len(job.photos)}장을 준비했습니다. 새 사진 {len(added)}장 추가.'
    store.save(job)
    return job


@app.post('/api/jobs/{job_id}/photos')
async def append_photos(job_id: str, files: list[UploadFile] = File(...)):
    inputs = await read_uploads(files)
    return add_photos(store.read(job_id), inputs).model_dump()


@app.post('/api/jobs/{job_id}/enhance', status_code=202)
async def enhance_photos(job_id: str, value: dict):
    job = store.read(job_id)
    photos_editable(job)
    selected = value.get('photos')
    if not job.photos:
        raise ValueError('변환할 사진을 먼저 넣어 주세요.')
    if selected is not None and (not isinstance(selected, list) or not selected or len(set(selected)) != len(selected)
                                 or any(type(i) is not int or not 0 <= i < len(job.photos) for i in selected)):
        raise ValueError('변환할 사진 번호가 올바르지 않습니다.')
    if not await codex.login_status():
        raise ValueError('설정에서 ChatGPT 구독 계정으로 Codex에 로그인해 주세요.')
    pipeline.launch(job_id, pipeline.enhance, selected)
    return {'ok': True}


@app.post('/api/jobs/{job_id}/photos/{index}/source')
async def photo_source(job_id: str, index: int, value: dict):
    job = store.read(job_id)
    photos_editable(job)
    if not 0 <= index < len(job.photos) or value.get('source') not in {'original', 'enhanced'}:
        raise ValueError('사진 번호나 선택 값이 올바르지 않습니다.')
    enhance.use(job, index, value['source'] == 'enhanced')
    store.save(job)
    return job.model_dump()


@app.post('/api/collections', status_code=202)
async def start_collection(value: dict):
    url = value.get('url')
    if not isinstance(url, str):
        raise ValueError('여기어때 국내 숙소 링크를 입력해 주세요.')
    collection_id, launch = collector.start(url)
    if launch:
        spawn(collector.collect(collection_id))
    return collector.gallery(collection_id)


@app.get('/api/collections/{collection_id}')
async def collection_status(collection_id: str):
    return collector.gallery(collection_id)


@app.get('/api/collections/{collection_id}/photos/{photo_id}')
async def collection_photo(collection_id: str, photo_id: str):
    return Response(await asyncio.to_thread(collector.thumbnail, collection_id, photo_id), media_type='image/jpeg')


@app.post('/api/jobs/import')
async def import_collection(value: dict):
    collection_id = value.get('collection_id', '')
    job = store.read(value['job_id']) if value.get('job_id') else new_job()
    link_collection(job)
    manifest = collector.read(collection_id)
    if manifest['status'] == 'in_progress':
        raise ValueError('수집이 끝난 뒤 사진을 선택해 주세요.')
    selected = value.get('photos')
    if not isinstance(selected, list) or not 1 <= len(selected) <= 60 or any(not isinstance(p, str) for p in selected):
        raise ValueError('가져올 사진을 1~60장 선택해 주세요.')
    photos_editable(job)
    inputs = []
    total = 0
    for photo_id in dict.fromkeys(selected):
        try:
            entry = collector.photo_entry(collection_id, photo_id)
        except FileNotFoundError as error:
            raise ValueError(str(error)) from error
        if entry['bytes'] > 20 * 1024 * 1024:
            raise ValueError('사진당 20MB 이하로 선택해 주세요.')
        total += entry['bytes']
        if total > 300 * 1024 * 1024:
            raise ValueError('선택 사진의 전체 용량은 300MB 이하여야 합니다.')
        labels = entry.get('labels', [])
        title = next((label.get('title') or label.get('room_name') for label in labels if label.get('title') or label.get('room_name')), '사진')
        inputs.append(((collector.folder(collection_id) / entry['file']).read_bytes(),
                       {'name': f"{manifest['name']} · {title}", 'labels': labels, 'source_url': entry['url']}))
    source = {'name': manifest['name'], 'url': manifest['source_url'], 'collection_id': collection_id}
    return add_photos(job, inputs, source).model_dump()


@app.get('/api/style')
async def saved_style():
    return saved_options().model_dump()


def caption_presets_file():
    from . import paths
    return paths.DATA / 'caption-presets.json'


def read_caption_presets():
    path = caption_presets_file()
    if not path.is_file():
        return []
    try:
        items = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(items, list):
            raise ValueError('invalid presets')
        return [{'id': item['id'], **CaptionPresetRequest.model_validate(item).model_dump()} for item in items]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError('저장한 자막 설정 파일을 읽을 수 없습니다.') from error


@app.get('/api/caption-presets')
async def caption_presets():
    with store.LOCK:
        return read_caption_presets()


@app.post('/api/caption-presets')
async def save_caption_preset(value: CaptionPresetRequest):
    with store.LOCK:
        items = read_caption_presets()
        item = next((p for p in items if p['name'] == value.name), None)
        if item is None:
            item = {'id': uuid.uuid4().hex}
            items.append(item)
        item.update(value.model_dump())
        path = caption_presets_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        return item


@app.get('/api/jobs/{job_id}')
async def status(job_id: str, per_photo: bool = False):
    job = store.read(job_id)
    domains.load(job.domain)  # A job whose kind was removed cannot be edited; say so instead of failing later.
    migration = bool(per_photo and job.domain == 'stay' and job.storyboard and not pipeline.busy(job_id) and not assets.pending(job)
                     and any(len(s.photos) > 1 for s in job.storyboard.scenes))
    if migration:
        assets.capture(job)
        job.storyboard = job.storyboard.per_photo()
        assets.restore(job)
        assets.invalidate(job)
        job.message = '사진마다 대본을 한 칸씩 나눴습니다. 변경 사항을 저장한 뒤 미리보기를 확인해 주세요.'
    return job.model_dump() | {'photo_plan_migration': migration,
        'preview_stale': bool(job.preview and job.preview.get('revision') != assets.render_revision(job)),
        'media_ready': assets.ready(job, job_path(job.id)), 'pending_media': assets.pending(job),
        'export_files': export_files(job)}


@app.post('/api/jobs/{job_id}/analyze')
async def analyze(job_id: str, options: CreateOptions):
    if pipeline.busy(job_id):
        raise ValueError('작업 진행 중에는 설정을 변경할 수 없습니다.')
    job = store.read(job_id)
    job.options = options
    store.save(job)
    pipeline.launch(job_id, pipeline.analyze)
    return {'ok': True}


@app.post('/api/jobs/{job_id}/prepare', status_code=202)
async def prepare(job_id: str, options: CreateOptions):
    job = store.read(job_id)
    editable(job)
    assets.prepare_allowed(job)
    domain = domains.load(job.domain)
    if domain.media != 'photo' or not domain.features.enhance:
        raise ValueError('이 영상 종류는 사진 동시 준비를 지원하지 않습니다.')
    if len(job.photos) < domain.min_media:
        raise ValueError(f'사진을 {domain.min_media}장 이상 준비해 주세요.')
    if job.domain == 'stay-silent' and len(job.photos) > 10:
        raise ValueError('음성X는 사용할 사진 8~10장을 먼저 골라 주세요.')
    if not await codex.login_status():
        raise ValueError('설정에서 ChatGPT 구독 계정으로 Codex에 로그인해 주세요.')
    # Login checks yield: re-read before writing so a concurrent request cannot overwrite work.
    job = store.read(job_id)
    editable(job)
    assets.prepare_allowed(job)
    if len(job.photos) < domain.min_media:
        raise ValueError(f'사진을 {domain.min_media}장 이상 준비해 주세요.')
    job.options = options
    store.save(job)
    pipeline.launch(job_id, pipeline.prepare)
    return {'ok': True}


@app.get('/api/jobs/{job_id}/performance')
async def performance_report(job_id: str):
    store.read(job_id)
    return await asyncio.to_thread(performance.report, job_path(job_id))


@app.post('/api/jobs/{job_id}/style')
async def style(job_id: str, options: CreateOptions):
    if pipeline.busy(job_id):
        raise ValueError('작업이 끝난 뒤 자막 설정을 변경해 주세요.')
    job = store.read(job_id)
    if job.workflow_version >= 2 and (job.options.caption != options.caption or job.options.speed != options.speed
                                      or job.options.badge != options.badge
                                      or job.options.thumbnail != options.thumbnail):
        job.result = None
    job.options.caption = options.caption
    job.options.badge = options.badge
    job.options.thumbnail = options.thumbnail
    job.options.speed = options.speed
    store.save(job)
    initialize()
    style_file().write_text(json.dumps({'caption': options.caption.model_dump(), 'speed': options.speed,
                                        'badge': options.badge.model_dump(),
                                        'thumbnail_style': options.thumbnail.style.model_dump()}), encoding='utf-8')
    return job.model_dump()


@app.post('/api/jobs/{job_id}/storyboard')
async def edit_storyboard(job_id: str, value: dict):
    job = store.read(job_id)
    if job.workflow_version >= 2:
        editable(job)
        if domains.load(job.domain).media == 'video':
            plan = Storyboard.validate_video_plan(value, job.photos)
        else:
            plan = Storyboard.validate_plan(value, len(job.photos), len(job.photos), job.photo_order or list(range(len(job.photos))))
            if job.domain == 'stay-silent':
                plan.validate_silent(len(job.photos))
        assets.capture(job)
        assets.checkpoint(job)
        job.storyboard = plan
        assets.restore(job)
        assets.invalidate(job)
        store.save(job)
        return job.model_dump()
    if pipeline.busy(job_id) or assets.voice_started(job) or any(v.get('generation_id') or v.get('idempotency_key') for v in job.generated.values()):
        raise ValueError('생성 전에만 대본·움직임을 바꿀 수 있습니다. 새 작업을 만들어 주세요.')
    job.storyboard = Storyboard.validate_plan(value, len(job.photos), len(job.photos))
    job.quote = job.approval = None
    job.generated = {}
    job.narration = {}
    job.state, job.error = 'uploaded', None
    job.message = '대본·움직임 설정을 저장했습니다. 크레딧 견적을 다시 확인해 주세요.'
    store.save(job)
    return job.model_dump()


@app.post('/api/jobs/{job_id}/quote')
async def quote(job_id: str, value: dict):
    workspace = value.get('workspace')
    if not workspace or not any(item['workspace_id'] == workspace for item in connection['workspaces']):
        raise ValueError('연결된 Fish 작업 공간을 선택해 주세요.')
    pipeline.launch(job_id, pipeline.quote, workspace)
    return {'ok': True}


@app.post('/api/jobs/{job_id}/video-models')
async def compare_video_models(job_id: str, value: dict):
    workspace = value.get('workspace')
    if not workspace or not any(item['workspace_id'] == workspace for item in connection['workspaces']):
        raise ValueError('모델별 크레딧을 비교하려면 연결된 Fish 작업 공간을 선택해 주세요.')
    job = store.read(job_id)
    if not job.photos:
        raise ValueError('사진을 먼저 추가해 주세요.')
    # Estimate with one real photo (prefer a motion photo) so credits match this job's input.
    order = job.photo_order or list(range(len(job.photos)))
    motion = job.storyboard.motion[0] if job.storyboard and job.storyboard.motion else None
    index = motion.photo if motion else order[0]
    focus = next(((f.x, f.y) for f in job.storyboard.crop_focus if f.photo == index), (0.5, 0.5)) if job.storyboard else (0.5, 0.5)
    image = job_path(job.id) / 'model-probe.jpg'
    await asyncio.to_thread(lambda: crop_photo(job_path(job.id) / job.photos[index]['file'], *focus).save(image, quality=96))
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    probe = job.model_probe or {}
    if probe.get('workspace') != workspace or probe.get('sha256') != digest:
        probe = {'workspace': workspace, 'sha256': digest, 'object_key': await fish.upload(image, workspace)}
        job = store.read(job_id)
        job.model_probe = probe
        store.save(job)
    prompt = motion.prompt if motion else 'Preserve the original scene; add only subtle natural motion.'
    rows = await video_models.estimates(fish, workspace, probe['object_key'], prompt)
    return {'models': rows, 'selected': job.video_choice(),
            'motion_count': len(job.storyboard.motion) if job.storyboard else 0}


@app.post('/api/jobs/{job_id}/video-model')
async def select_video_model(job_id: str, value: dict):
    job = store.read(job_id)
    editable(job)
    model, parameters = value.get('model'), value.get('parameters')
    if not video_models.find(await video_models.catalog(fish), model, parameters):
        raise ValueError('선택할 수 없는 영상 모델이거나 화질 설정입니다. 모델 목록을 다시 불러와 주세요.')
    if job.video_choice() == {'model': model, 'parameters': parameters}:
        return job.model_dump()
    assets.capture(job)
    job.video_model, job.video_parameters = model, parameters
    assets.restore(job)  # Reuses clips already made with this model; others need a new quote.
    assets.invalidate(job)
    job.message = f'영상 모델을 {model} · {parameters["resolution"]}로 바꿨습니다. 크레딧 견적을 다시 확인해 주세요.'
    store.save(job)
    return job.model_dump()


@app.post('/api/jobs/{job_id}/approve')
async def approve(job_id: str, value: ApproveRequest):
    if pipeline.busy(job_id):
        raise ValueError('작업이 실행 중입니다.')
    job = store.read(job_id)
    store.approve(job, value.quote_id, value.expected_credits)
    pipeline.launch(job_id, pipeline.generate)
    return {'ok': True}


@app.post('/api/jobs/{job_id}/resume')
async def resume(job_id: str):
    job = store.read(job_id)
    store.generation_allowed(job)
    pipeline.launch(job_id, pipeline.generate)
    return {'ok': True}


@app.post('/api/jobs/{job_id}/render')
async def rerender(job_id: str):
    pipeline.launch(job_id, pipeline.render)
    return {'ok': True}


def editable(job):
    if pipeline.busy(job.id) or assets.pending(job):
        raise ValueError('생성 작업을 완료하거나 제출 결과를 복구한 뒤 수정해 주세요.')


@app.post('/api/composition')
async def configure_composition(value: dict):
    job = store.read(value['job_id']) if value.get('job_id') else new_job(value.get('domain', 'stay'), workflow_version=2)
    editable(job)
    if value.get('collection_id'):
        link_collection(job)
    mode = value.get('mode', 'manual')
    if mode not in {'manual', 'ai'}:
        raise ValueError('구성 방식이 올바르지 않습니다.')
    target = value.get('target_count', 10)
    if type(target) is not int or not 1 <= target <= 60:
        raise ValueError('추천 장수는 1~60장입니다.')
    if job.domain == 'stay-silent' and mode == 'ai' and not 8 <= target <= 10:
        raise ValueError('음성X AI 추천은 사진 8~10장을 선택해 주세요.')
    picked = value.get('photos') if value.get('collection_id') else None
    if picked is not None and (not isinstance(picked, list) or any(not isinstance(i, str) for i in picked)):
        raise ValueError('가져올 사진 목록이 올바르지 않습니다.')
    job.candidates = recommendation.candidates(job, value.get('collection_id'), picked or ())
    if mode == 'ai' and not job.candidates:
        raise ValueError('AI 추천에 사용할 사진을 먼저 첨부하거나 수집 목록에서 선택해 주세요.')
    job.target_count = target
    job.composition_mode = mode
    job.workflow_version = 2
    if value.get('collection_id') and picked:
        gallery = collector.gallery(value['collection_id'])
        if not any(s.get('collection_id') == gallery['id'] for s in job.sources):
            job.sources.append({'name': gallery['name'], 'url': gallery['source_url'], 'collection_id': gallery['id']})
    if mode == 'manual':
        if 'order' in value:
            composition.reorder(job, value['order'])
        else:
            composition.select(job, value.get('photos'))
    store.save(job)
    if mode == 'ai':
        pipeline.launch(job.id, pipeline.recommend)
    return job.model_dump()


@app.post('/api/jobs/{job_id}/candidates')
async def append_candidates(job_id: str, files: list[UploadFile] = File(...)):
    inputs = await read_uploads(files)
    job = store.read(job_id)
    editable(job)
    folder = job_path(job.id)
    pool = {p['id']: p for p in recommendation.candidates(job)}
    staged = []
    with tempfile.TemporaryDirectory(prefix='.candidates-', dir=folder) as temporary:
        for data, metadata in inputs:
            digest = hashlib.sha256(data).hexdigest()
            if digest in pool:
                continue
            try:
                with Image.open(io.BytesIO(data)) as original:
                    if original.format not in {'JPEG', 'PNG', 'WEBP'}:
                        raise ValueError('JPG, PNG, WebP 사진을 넣어 주세요.')
                    image = ImageOps.exif_transpose(original).convert('RGB')
                    if min(image.size) < 300:
                        raise ValueError('사진의 짧은 변은 300px 이상이어야 합니다.')
                    name = f'candidate-{digest}.jpg'
                    image.save(Path(temporary) / name, quality=97)
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
                raise ValueError('읽을 수 없는 사진 파일이 있습니다.') from error
            candidate = dict(metadata, id=digest, sha256=digest, file=name, bytes=len(data), width=image.width, height=image.height)
            pool[digest] = candidate
            staged.append(name)
        for name in staged:
            (Path(temporary) / name).replace(folder / name)
    job.candidates = list(pool.values())
    if not job.storyboard and not job.generated and not job.narration:
        old_count = len(job.photos)
        active_ids = {p['sha256'] for p in job.photos}
        total = sum(p.get('bytes', 0) for p in job.photos)
        for candidate in job.candidates:
            if candidate['id'] not in active_ids and len(job.photos) < 60 and total + candidate.get('bytes', 0) <= 300 * 1024**2:
                job.photos.append(dict(candidate))
                active_ids.add(candidate['id'])
                total += candidate.get('bytes', 0)
        job.photo_order = (job.photo_order or list(range(old_count))) + list(range(old_count, len(job.photos)))
    job.message = f'후보 {len(job.candidates)}장 · 현재 구성 {len(job.photos)}장을 준비했습니다.'
    store.save(job)
    return job.model_dump()


@app.post('/api/jobs/{job_id}/candidates/remove')
async def remove_candidates(job_id: str, value: dict):
    job = store.read(job_id)
    editable(job)
    composition.remove(job, value.get('ids'))
    job.message = f'후보 {len(job.candidates)}장 · 현재 구성 {len(job.photos)}장입니다.'
    store.save(job)
    return job.model_dump()


@app.post('/api/jobs/{job_id}/preview-video')
async def preview_video(job_id: str):
    pipeline.launch(job_id, pipeline.preview)
    return {'ok': True}


@app.post('/api/jobs/{job_id}/media-attempt')
async def media_attempt(job_id: str, value: dict):
    job = store.read(job_id)
    editable(job)
    if not job.storyboard:
        raise ValueError('대본을 먼저 작성해 주세요.')
    kind, index = value.get('kind'), value.get('index')
    if kind == 'voice' and not domains.load(job.domain).features.narration:
        raise ValueError('숙소(음성X) 작업에서는 음성을 생성하지 않습니다.')
    if type(index) is not int:
        raise ValueError('장면 또는 사진 번호를 확인해 주세요.')
    if kind == 'voice' and 0 <= index < len(job.storyboard.scenes):
        key = assets.voice_key(job, job.storyboard.scenes[index])
    elif kind == 'motion' and any(m.photo == index for m in job.storyboard.motion):
        key = assets.motion_key(job, next(m for m in job.storyboard.motion if m.photo == index))
    else:
        raise ValueError('복원 또는 재생성할 미디어가 없습니다.')
    assets.capture(job)
    assets.checkpoint(job)
    if value.get('restore') is not None:
        attempt = value['restore']
        if type(attempt) is not int or f'{key}:{attempt}' not in job.assets or not job.assets[f'{key}:{attempt}'].get('file'):
            raise ValueError('이전 생성 결과를 찾지 못했습니다.')
        job.attempts[key] = attempt
    else:
        job.attempts[key] = max([int(k.rsplit(':', 1)[1]) for k in job.assets if k.startswith(key + ':')] + [job.attempts.get(key, 0)]) + 1
    assets.restore(job)
    assets.invalidate(job)
    store.save(job)
    return job.model_dump()


@app.get('/api/jobs/{job_id}/media/{asset_id}')
async def media_history(job_id: str, asset_id: str):
    from .models import stable_hash
    job = store.read(job_id)
    record = next((r for k, r in job.assets.items() if stable_hash(k) == asset_id), None)
    if not record or not record.get('file') or Path(record['file']).name != record['file']:
        raise HTTPException(404)
    path = job_path(job.id) / record['file']
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.post('/api/jobs/{job_id}/versions/{version}')
async def restore_version(job_id: str, version: int):
    job = store.read(job_id)
    editable(job)
    if not 0 <= version < len(job.versions):
        raise ValueError('저장된 이전 구성이 없습니다.')
    snapshot = dict(job.versions[version])
    assets.capture(job)
    assets.checkpoint(job)
    job.photos = snapshot['photos']
    job.photo_order = snapshot['photo_order']
    job.storyboard = Storyboard.model_validate(snapshot['storyboard'])
    job.attempts = snapshot['attempts']
    job.composition_mode = snapshot['mode']
    assets.restore(job)
    assets.invalidate(job)
    store.save(job)
    return job.model_dump()


@app.post('/api/jobs/{job_id}/recover-voice/{scene}')
async def recover_voice(job_id: str, scene: int, file: UploadFile = File(...)):
    if pipeline.busy(job_id):
        raise ValueError('작업이 실행 중입니다.')
    reservation = asyncio.current_task()
    pipeline.tasks[job_id] = reservation
    try:
        return await recover_voice_asset(job_id, scene, file)
    finally:
        if pipeline.tasks.get(job_id) is reservation:
            pipeline.tasks.pop(job_id, None)


async def recover_voice_asset(job_id, scene, file):
    job = store.read(job_id)
    if not domains.load(job.domain).features.narration:
        raise ValueError('숙소(음성X) 작업에서는 음성을 사용하지 않습니다.')
    if not job.storyboard or not 0 <= scene < len(job.storyboard.scenes):
        raise ValueError('복구할 수 없는 장면입니다.')
    data = await file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise ValueError('음성 파일은 20MB 이하로 넣어 주세요.')
    assets.capture(job)
    assets.checkpoint(job)
    key = assets.voice_key(job, job.storyboard.scenes[scene])
    if job.narration.get(str(scene), {}).get('file'):
        job.attempts[key] = max([int(k.rsplit(':', 1)[1]) for k in job.assets if k.startswith(key + ':')] + [job.attempts.get(key, 0)]) + 1
    name = f'voice-recovered-{uuid.uuid4().hex}.mp3' if job.workflow_version >= 2 else f'voice-{scene:03}.mp3'
    path = job_path(job_id) / name
    pending = path.with_name(f'voice-{scene:03}.pending.mp3')
    pending.write_bytes(data)
    from .render import run
    try:
        await asyncio.to_thread(run, '-i', pending, '-f', 'null', '-')
    except Exception:
        pending.unlink(missing_ok=True)
        raise ValueError('읽을 수 없는 음성 파일입니다.')
    pending.replace(path)
    job.narration[str(scene)] = {'file': name, 'state': 'completed', 'recovered_by_user': True,
                                'asset_key': assets.cache_key(job, assets.voice_key(job, job.storyboard.scenes[scene])),
                                'text': job.storyboard.scenes[scene].text}
    assets.capture(job)
    assets.invalidate(job)
    store.save(job)
    return {'ok': True}


@app.post('/api/jobs/{job_id}/caption-cues')
async def photo_caption_cues(job_id: str, value: dict):
    job = store.read(job_id)
    text = str(value.get('text', ''))[:320]
    style = CaptionStyle.model_validate(value.get('caption', {}))
    return [text] if not domains.load(job.domain).features.narration else caption_segments(text, style)


@app.post('/api/jobs/{job_id}/preview')
async def preview(job_id: str, value: dict):
    job = store.read(job_id)
    index = int(value.get('photo', 0))
    if not 0 <= index < len(job.photos):
        raise ValueError('사진 번호가 올바르지 않습니다.')
    caption = CaptionStyle.model_validate(value.get('caption', {}))
    text = value.get('text', domains.load(job.domain).text('caption_default', '자막'))[:320]
    if job.photos[index].get('kind') == 'video':
        image = await asyncio.to_thread(video_preview, job, index, value, text, caption)
        data = io.BytesIO()
        image.save(data, format='JPEG', quality=92)
        return Response(data.getvalue(), media_type='image/jpeg')
    focus = next(((item.x, item.y) for item in job.storyboard.crop_focus if item.photo == index),
                 (0.5, 0.5)) if job.storyboard else (0.5, 0.5)
    if 'focus' in value:
        crop = CropFocus.model_validate(dict(value['focus'], photo=index))
        focus = (crop.x, crop.y)
    image = await asyncio.to_thread(preview_image, job_path(job_id) / job.photos[index]['file'], text, caption, focus)
    data = io.BytesIO()
    image.save(data, format='JPEG', quality=95)
    return Response(data.getvalue(), media_type='image/jpeg')


@app.post('/api/jobs/{job_id}/thumbnail-preview')
async def thumbnail_preview(job_id: str, value: dict):
    """The reel's opening still with a draft title style, so it can be set by eye before rendering."""
    from .render import thumbnail_images
    job = store.read(job_id)
    choice = Thumbnail.model_validate(value.get('thumbnail', {}))
    badge = BadgeStyle.model_validate(value['badge']) if 'badge' in value else None
    _, image = await asyncio.to_thread(thumbnail_images, job, job_path(job_id), choice, badge)
    data = io.BytesIO()
    image.save(data, format='JPEG', quality=92)
    return Response(data.getvalue(), media_type='image/jpeg')


def video_preview(job, index, value, text, caption):
    """A source-video frame at `time` seconds, zoomed by the saved (or a draft) crop, with the caption on top."""
    from .render import video_frame
    source = job.photos[index]
    seconds = min(max(float(value.get('time', 0) or 0), 0), max(source['duration'] - 0.05, 0))
    crop = CropBox.model_validate(value['crop']).model_dump() if 'crop' in value else source.get('crop', {})
    frames = job_path(job.id) / 'frames'
    frames.mkdir(exist_ok=True)
    frame = frames / f'{index:03}-{seconds:.2f}.jpg'
    if not frame.is_file():
        video_frame(job_path(job.id) / source['file'], seconds, frame)
    image = crop_video_frame(frame, crop).convert('RGBA')
    if text.strip():
        image.alpha_composite(caption_image(text, caption))
    disclosure = domains.load(job.domain).disclosure
    if disclosure:  # Shown here so its position can be set by eye.
        badge = BadgeStyle.model_validate(value['badge']) if 'badge' in value else job.options.badge
        image.alpha_composite(badge_image(disclosure, badge))
    return image.convert('RGB')


@app.get('/api/jobs/{job_id}/files/{name}')
async def files(job_id: str, name: str, download: bool = False):
    job = store.read(job_id)
    allowed = enhance.files(job) | {'stay-reel.mp4', 'stay-video.mp4', 'stay-voice.mp3',
                                               'stay-script.txt', 'stay-reel.srt', 'preview.jpg', 'verification.json'}
    allowed |= {p['file'] for p in job.candidates if p.get('file') and not p.get('collection_id')}
    allowed |= {p['thumb'] for p in job.photos if p.get('thumb')}  # Source-video thumbnails.
    if job.preview:
        allowed |= {job.preview[k] for k in ('file', 'video', 'audio', 'script', 'srt', 'verification') if k in job.preview}
    allowed |= {r['file'] for r in job.assets.values() if r.get('file', '').endswith('.mp4')}
    if Path(name).name != name or name not in allowed:
        raise HTTPException(404)
    path = job_path(job_id) / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=name if download or name.endswith(('.mp4', '.mp3', '.txt', '.srt', '.json')) else None)
