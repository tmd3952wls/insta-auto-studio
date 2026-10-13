"""Content-addressed media reuse, independent from scene and photo ordering."""
from . import domains
from .models import DRAMA_MODEL, VOICE_ID, stable_hash


def voice_key(job, scene):
    return 'voice:' + stable_hash({'text': scene.text, 'voice': VOICE_ID, 'model': DRAMA_MODEL})


def motion_key(job, motion):
    focus = {f.photo: (f.x, f.y) for f in job.storyboard.crop_focus}
    photos = [job.photos[index].get('sha256', job.photos[index].get('file')) for index in motion.photos]
    value = {'photo': photos[0], 'crop': focus.get(motion.photo, (.5, .5)),
             'prompt': motion.prompt, 'model': job.video_model, 'parameters': job.video_parameters}
    if len(motion.photos) > 1 or motion.seconds != 4:
        # Multi-photo clips; single 4s clips keep their original key so cached results stay reusable.
        value.update(photos=photos, crops=[focus.get(i, (.5, .5)) for i in motion.photos], seconds=motion.seconds)
    return 'motion:' + stable_hash(value)


def cache_key(job, key):
    return f'{key}:{job.attempts.get(key, 0)}'


def capture(job):
    if not job.storyboard:
        return
    for index, scene in enumerate(job.storyboard.scenes if domains.load(job.domain).features.narration else []):
        record = job.narration.get(str(index))
        if record:
            key = record.get('asset_key') or cache_key(job, voice_key(job, scene))
            job.assets[key] = dict(record, asset_key=key)
    for motion in job.storyboard.motion:
        record = job.generated.get(str(motion.photo))
        if record:
            key = record.get('asset_key') or cache_key(job, motion_key(job, motion))
            job.assets[key] = dict(record, asset_key=key)


def restore(job):
    job.narration, job.generated = {}, {}
    if not job.storyboard:
        return
    for index, scene in enumerate(job.storyboard.scenes if domains.load(job.domain).features.narration else []):
        key = cache_key(job, voice_key(job, scene))
        if key in job.assets:
            job.narration[str(index)] = dict(job.assets[key])
    for motion in job.storyboard.motion:
        key = cache_key(job, motion_key(job, motion))
        if key in job.assets:
            job.generated[str(motion.photo)] = dict(job.assets[key])


def pending(job):
    records = list(job.generated.values()) + list(job.narration.values()) + list(job.assets.values())
    return any(r.get('state') in {'submitting', 'submitted'} and not r.get('file') for r in records)


def voice_started(job):
    return any(r.get('state') in {'submitting', 'submitted', 'completed'} or r.get('url') or r.get('file')
               for r in job.narration.values())


def prepare_allowed(job):
    if pending(job) or voice_started(job) or job.result or any(
            r.get('file') or r.get('generation_id') or r.get('idempotency_key') for r in job.generated.values()):
        raise ValueError('영상·음성 생성이 시작된 작업은 새 작업에서 고화질과 대본을 함께 만들어 주세요.')


def render_revision(job):
    value = {'photos': [[p['sha256'], p.get('crop')] if p.get('kind') == 'video' else p.get('sha256', p.get('file'))
                        for p in job.photos],  # Source videos also carry their crop; photo entries are unchanged.
        'storyboard': job.storyboard.model_dump() if job.storyboard else None,
        'caption': job.options.caption.model_dump(), 'speed': job.options.speed,
        'voices': job.narration, 'motions': job.generated}
    if job.domain == 'stay':
        value['motion_timing_version'] = 2
    disclosure = domains.load(job.domain).disclosure
    if not domains.load(job.domain).features.narration:
        value['narration'] = False
    if disclosure:  # Only domains that show one; stay revisions keep their old value.
        value['disclosure'] = disclosure
        value['badge'] = job.options.badge.model_dump()
    return stable_hash(value)


def ready(job, folder):
    if not job.storyboard:
        return False
    voices = ([job.narration.get(str(i), {}) for i in range(len(job.storyboard.scenes))]
              if domains.load(job.domain).features.narration else [])
    motions = [job.generated.get(str(m.photo), {}) for m in job.storyboard.motion]
    return all(r.get('file') and (folder / r['file']).is_file() for r in voices + motions)


def checkpoint(job):
    if job.storyboard:
        value = {'photos': job.photos, 'photo_order': job.photo_order,
                 'storyboard': job.storyboard.model_dump(), 'attempts': dict(job.attempts),
                 'mode': job.composition_mode}
        if not job.versions or stable_hash(job.versions[-1]) != stable_hash(value):
            job.versions.append(value)
            job.versions = job.versions[-30:]


def invalidate(job):
    job.quote = job.approval = job.result = None
    job.state, job.error = 'uploaded', None
    job.message = '구성이 변경되었습니다. 미리보기를 업데이트하거나 변경분 견적을 확인해 주세요.'
