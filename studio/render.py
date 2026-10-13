from __future__ import annotations

import json
import re
import subprocess
import wave
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from array import array
from pathlib import Path

from . import domains
from .media import allocate_frames, badge_image, caption_image, caption_segments, crop_photo, cut_zoom, motion_cuts, srt_time
from .assets import render_revision
from .models import MAX_CUT, MIN_CUT, MIN_MOTION_SIDE
from . import performance
from .paths import RESOURCES, job_path

# Pause lengths measured on the reference reel (2026-10-12): ~0.18s after each sentence, ~0.08s between phrases.
SENTENCE_PAUSE = 0.15  # Breath after a sentence, scene ends included, so the next line does not run on.
PHRASE_PAUSE = 0.08  # Longest pause kept inside a sentence; leading silence is dropped.
SILENCE = 328  # About -40dBFS on 16-bit samples.
CAPTION_LEAD = 0.1  # Seconds a mid-sentence caption appears before its estimated spoken start.
RENDER_WORKERS = 2


def encode_clips(commands, progress, workers=RENDER_WORKERS):
    """Keep FFmpeg work bounded; join all workers before packaging or returning errors."""
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(copy_context().run, run, *command) for command in commands]
        for index, future in enumerate(futures):
            future.result()
            progress(f'1080p 클립 합성 완료 · {index + 1}/{len(commands)}')


def ffmpeg():
    path = RESOURCES / 'tools/ffmpeg.exe'
    if path.exists():
        return str(path)
    installed = list((RESOURCES / '.tools/python/imageio_ffmpeg/binaries').glob('ffmpeg*.exe'))
    if installed:
        return str(installed[0])
    import sys
    if str(RESOURCES / '.tools/python') not in sys.path:
        sys.path.insert(0, str(RESOURCES / '.tools/python'))
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(*args):
    output = Path(str(args[-1])).name
    if '-filter_complex' in args:
        stage = 'render.clip_encode'
    elif 'null' in args:
        stage = 'render.decode_verify'
    elif output.startswith('audio-'):
        stage = 'render.audio_process'
    elif output.startswith('padded-'):
        stage = 'render.audio_pad'
    elif output == 'preview.jpg':
        stage = 'render.poster'
    else:
        stage = 'render.package'
    with performance.span(stage, output=output):
        process = subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y', *map(str, args)],
            capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode:
            raise RuntimeError(process.stderr.decode('utf-8', 'replace')[-2000:])


@performance.measured('render.probe')
def video_size(path):
    process = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(path)], capture_output=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    text = process.stderr.decode('utf-8', 'replace')
    match = re.search(r'Video:.*?\b(\d{2,5})x(\d{2,5})\b', text)
    if not match:
        raise RuntimeError('영상 해상도를 읽을 수 없습니다.')
    return tuple(map(int, match.groups()))


def video_info(path):
    """(width, height, seconds) of a video file, or a ValueError when ffmpeg cannot read it."""
    process = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(path)], capture_output=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    text = process.stderr.decode('utf-8', 'replace')
    size = re.search(r'Video:.*?\b(\d{2,5})x(\d{2,5})\b', text)
    duration = re.search(r'Duration: (\d+):(\d+):([\d.]+)', text)
    if not size or not duration:
        raise ValueError('읽을 수 없는 영상 파일입니다.')
    rotate = re.search(r'rotate\s*:\s*(-?\d+)|displaymatrix: rotation of (-?[\d.]+)', text)
    width, height = map(int, size.groups())
    if rotate and abs(round(float(rotate.group(1) or rotate.group(2)))) % 180 == 90:
        width, height = height, width  # Phone clips store portrait video as rotated landscape.
    return width, height, int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])


def video_frame(path, seconds, target, width=None):
    """Saves the frame at `seconds` as a JPEG, optionally scaled to `width` pixels wide."""
    run('-ss', f'{max(seconds, 0):.3f}', '-i', path, '-frames:v', '1',
        *(['-vf', f'scale={width}:-2'] if width else []), '-q:v', '3', target)


def video_frame_rate(path):
    process = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(path)], capture_output=True,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    match = re.search(r'Video:.*?\b(\d+(?:\.\d+)?) fps', process.stderr.decode('utf-8', 'replace'))
    if not match:
        raise ValueError('원본 영상의 프레임 속도를 읽을 수 없습니다.')
    return float(match[1])


def motion_filter(start, end, frames, source_fps, fill=None, smooth=True):
    """Fit the shot's first/last frames to its slot; interpolation fills slow-motion frames."""
    fill = fill or 'scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920'
    source_frames = max(round((end - start) * source_fps), 2)
    factor = max(frames - 1, 1) / 30 / ((source_frames - 1) / source_fps)
    cadence = ('minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1'
               if smooth and factor > 1.01 else 'fps=30')
    # Buffer frames serve the interpolator's lookahead; output ends at the mapped final source frame.
    return (f'trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,'
            f'tpad=stop_mode=clone:stop=3,settb=AVTB,setpts=PTS*{factor:.8f},{cadence},'
            f'tpad=stop_mode=clone:stop=2,{fill},'
            f'trim=end_frame={frames},settb=1/30,setpts=N,setsar=1')


def crop_filter(crop):
    """ffmpeg filter that zooms a source into 1080x1920; same geometry as media.crop_video_frame."""
    zoom, x, y = crop.get('zoom', 1.0), crop.get('x', 0.5), crop.get('y', 0.5)
    return (f'scale=w={round(1080 * zoom)}:h={round(1920 * zoom)}:force_original_aspect_ratio=increase:flags=lanczos,'
            f'crop=1080:1920:(iw-1080)*{x:.4f}:(ih-1920)*{y:.4f}')


@performance.measured('render.shot_detection')
def shot_bounds(path, shots):
    """(start, end) seconds of each hard-cut shot in a clip, placing cuts at the strongest scene changes."""
    process = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(path), '-an', '-vf',
        "select='gte(scene,0)',metadata=print:key=lavfi.scene_score", '-f', 'null', '-'],
        capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    text = process.stderr.decode('utf-8', 'replace')
    match = re.search(r'Duration: (\d+):(\d+):([\d.]+)', text)
    duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]) if match else 4.0
    times = [float(t) for t in re.findall(r'pts_time:([\d.]+)', text)]
    scores = [float(s) for s in re.findall(r'lavfi\.scene_score=([\d.]+)', text)]
    cuts = []
    for time, score in sorted(zip(times, scores), key=lambda pair: pair[1], reverse=True):
        if len(cuts) == shots - 1 or score < 0.2:
            break
        if 0.3 < time < duration - 0.3 and all(abs(time - cut) > 0.35 for cut in cuts):
            cuts.append(time)
    if len(cuts) != shots - 1:  # The model merged or skipped a shot: fall back to equal parts.
        cuts = [duration * index / shots for index in range(1, shots)]
    edges = [0.0, *sorted(cuts), duration]
    return list(zip(edges, edges[1:]))


def speech_end(path, threshold=328):
    """Seconds until the last sample louder than about -40dBFS in a 16-bit wav."""
    with wave.open(str(path)) as source:
        rate, channels = source.getframerate(), source.getnchannels()
        samples = array('h', source.readframes(source.getnframes()))
    for index in range(len(samples) - 1, -1, -1):
        if abs(samples[index]) >= threshold:
            return (index // channels + 1) / rate
    return 0.0


def sentences(text):
    return [part for part in re.split(r'(?<=[.?!])\s+', text.strip()) if part.strip()] or [text]


def pace_pauses(source, target, breaks, scale=1.0, tail=SENTENCE_PAUSE):
    """Rewrites a 16-bit wav with the `breaks` longest inner pauses set to SENTENCE_PAUSE, the tail to `tail` and
    other pauses capped at PHRASE_PAUSE (lengths multiplied by `scale`). Returns each sentence's (start, end) seconds."""
    with wave.open(str(source)) as audio:
        params, rate, channels = audio.getparams(), audio.getframerate(), audio.getnchannels()
        samples = array('h', audio.readframes(audio.getnframes()))
    step = rate // 100 * channels  # 10ms blocks.
    loud = [max(map(abs, samples[i:i + step]), default=0) >= SILENCE for i in range(0, len(samples), step)]
    runs, start = [], None  # Quiet runs as (first block, end block).
    for index, value in enumerate(loud + [True]):
        if not value and start is None:
            start = index
        elif value and start is not None:
            runs.append((start, index))
            start = None
    if not any(loud):
        audio_out, spans = array('h'), []
    else:
        first, last = loud.index(True), len(loud) - loud[::-1].index(True)
        inner = [r for r in runs if first < r[0] and r[1] <= last]
        chosen = set(sorted(inner, key=lambda r: r[1] - r[0], reverse=True)[:breaks])
        audio_out, spans, cursor, sentence_start = array('h'), [], first, 0.0
        silence = lambda seconds: array('h', bytes(round(seconds * scale * rate) * channels * 2))
        for begin, end in sorted(inner):
            audio_out += samples[cursor * step:begin * step]
            if (begin, end) in chosen:
                spans.append((sentence_start, len(audio_out) / channels / rate))
                audio_out += silence(SENTENCE_PAUSE)
                sentence_start = len(audio_out) / channels / rate
            else:
                audio_out += samples[begin * step:min(end, begin + round(PHRASE_PAUSE * scale * 100)) * step]
            cursor = end
        audio_out += samples[cursor * step:last * step]
        spans.append((sentence_start, len(audio_out) / channels / rate))
        audio_out += silence(tail)
    with wave.open(str(target), 'wb') as output:
        output.setparams(params)
        output.writeframes(audio_out.tobytes())
    return [(begin / scale, end / scale) for begin, end in spans]


def verify(path, decode=True):
    if video_size(path) != (1080, 1920):
        raise RuntimeError('출력 영상이 1080×1920이 아닙니다.')
    if decode:
        run('-i', path, '-f', 'null', '-')


@performance.measured('render.total')
def render(job, progress=lambda message: None, preview=False, folder=None, workers=RENDER_WORKERS):
    if not job.storyboard:
        raise ValueError('대본이 없습니다.')
    if job.domain == 'stay-silent':
        job.storyboard.validate_silent(len(job.photos))
    folder = folder if folder is not None else job_path(job.id)
    for motion in job.storyboard.motion:
        asset = job.generated.get(str(motion.photo), {}).get('file')
        if not asset or not (folder / asset).is_file():
            raise RuntimeError('선택한 움직임 영상이 아직 없습니다. 생성 작업을 먼저 완료해 주세요.')
    export = folder / 'render'
    export.mkdir(exist_ok=True)
    # A domain's notice (shopping: the affiliate disclosure) stays on the final reel, never on the clean video.
    disclosure = domains.load(job.domain).disclosure
    badge = export / 'disclosure.png' if disclosure else None
    if badge:
        badge_image(disclosure, job.options.badge).save(badge)
    clips, clean_clips, audio_clips, subtitles, timeline = [], [], [], [], []
    commands = []
    offset = 0
    focus = {item.photo: (item.x, item.y) for item in job.storyboard.crop_focus}
    shots = {}  # photo -> (clip file, start, end): each photo plays its own shot of a multi-photo clip.
    source_rates = {}
    for motion in job.storyboard.motion:
        source = folder / job.generated[str(motion.photo)]['file']
        if job.domain == 'stay':
            source_rates[source] = video_frame_rate(source)
        if min(video_size(source)) < MIN_MOTION_SIDE:
            raise RuntimeError(f'생성 영상이 {MIN_MOTION_SIDE}p보다 작아 1080×1920 영상에 사용할 수 없습니다.')
        for photo, (start, end) in zip(motion.photos, shot_bounds(source, len(motion.photos))):
            shots[photo] = (source, start, end)
    fill = {}  # shot -> filter zooming it into 1080x1920; generated clips are just center-cropped.
    for index, segment in enumerate(job.storyboard.segments):  # Video domains: scenes index these segments.
        item = job.photos[segment.source]
        shots[index] = (folder / item['file'], segment.start, segment.end)
        fill[index] = crop_filter(item.get('crop', {}))
    narrated = domains.load(job.domain).features.narration
    longest = MAX_CUT if job.storyboard.segments else None  # Video domains keep every cut within MAX_CUT.
    for scene_index, scene in enumerate(job.storyboard.scenes):
        if narrated:
            record = job.narration.get(str(scene_index), {})
            audio = folder / record.get('file', '__missing__')
            if not audio.is_file():
                raise RuntimeError(f'{scene_index + 1}번 장면의 음성이 필요합니다.')
            decoded, paced = export / f'decoded-{scene_index:03}.wav', export / f'paced-{scene_index:03}.wav'
            adjusted = export / f'audio-{scene_index:03}.wav'
            run('-i', audio, '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', decoded)
            lines = sentences(scene.text)
            # Set pauses before atempo; a scene ending mid-sentence uses a phrase pause.
            tail = SENTENCE_PAUSE if re.search(r'[.?!…~]["\')\]]*$', scene.text.strip()) else PHRASE_PAUSE
            spans = pace_pauses(decoded, paced, len(lines) - 1, job.options.speed, tail)
            run('-i', paced, '-af', f'atempo={job.options.speed},loudnorm=I=-16:TP=-1.5:LRA=11',
                '-ar', '48000', '-ac', '2', adjusted)
            with wave.open(str(adjusted)) as source:
                seconds = source.getnframes() / source.getframerate()
        else:
            seconds = 2 * len(scene.photos)
            lines, spans = [scene.text], [(0.0, seconds)]
        moving = [index in shots for index in scene.photos]
        counts = allocate_frames(seconds, moving) if narrated else [60] * len(scene.photos)
        scene_duration = sum(counts) / 30
        scene_start = offset
        if len(spans) != len(lines):  # Fewer pauses than sentences: time the whole scene as one sentence.
            lines, spans = [scene.text], [(0.0, spans[-1][1] if spans else 0.0)]
        spans[-1] = (spans[-1][0], (min(speech_end(adjusted), scene_duration) or scene_duration) if narrated else scene_duration)
        # Each sentence's cues follow its own spoken span and split it by syllables, which track speech time;
        # a new sentence's first cue waits for its voice, so captions change after the breath, not before it.
        cues = []
        for (begin, finish), line in zip(spans, lines):
            segments = caption_segments(line, job.options.caption) if narrated else [line]
            weights = [max(len(re.sub(r'\W', '', text)), 1) for text in segments]
            boundary = 0
            for part, text in enumerate(segments):
                # Cues inside a sentence switch slightly early: a late caption reads as lag, an early one does not.
                start = begin + (finish - begin) * boundary / sum(weights) - (CAPTION_LEAD if part else 0)
                cues.append((text, max(start, 0) if cues else 0))
                boundary += weights[part]
        caption_paths = []
        for part, (text, start) in enumerate(cues):
            caption = export / f'caption-{scene_index:03}-{part:03}.png'
            caption_image(text, job.options.caption).save(caption)
            end = cues[part + 1][1] if part < len(cues) - 1 else scene_duration
            caption_paths.append((caption, start, end))
            subtitles.append(f'{len(subtitles)+1}\n{srt_time(offset+start)} --> {srt_time(offset+end)}\n{text}\n')
        if narrated:
            padded = export / f'padded-{scene_index:03}.wav'
            run('-i', adjusted, '-af', 'apad', '-t', scene_duration, padded)
            audio_clips.append(padded)
        for photo_index, count, is_moving in zip(scene.photos, counts, moving):
            progress(f'{photo_index + 1}/{len(job.storyboard.segments)} 구간을 1080p로 합성 중입니다.' if job.storyboard.segments
                     else f'{photo_index + 1}/{len(job.photos)} 사진을 1080p로 합성 중입니다.')
            clip = export / f'clip-{len(clips):03}.mp4'
            clean_clip = export / f'clean-{len(clips):03}.mp4'
            if is_moving:
                source, start, end = shots[photo_index]
                inputs = ['-i', source]
                # Stretch or trim the photo's shot to its slot (at most 2x slow motion, then hold the last frame),
                # upscale 720p-class clips to 1080×1920 and split long shots into punch-in cuts.
                speed = min(max(count / 30 / max(end - start, 0.1), 0.5), 2.0)
                base = (f'trim=start={start:.3f}:end={end:.3f},setpts=(PTS-STARTPTS)*{speed:.4f},'
                        f"{fill.get(photo_index, 'scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920')},fps=30,"
                        f'tpad=stop_mode=clone:stop_duration={count/30+1},'
                        f"zoompan=z='{cut_zoom(count, longest=longest) if narrated else '1.0'}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=1080x1920:fps=30,setsar=1")
                if job.domain == 'stay':
                    base = motion_filter(start, end, count, source_rates[source], fill.get(photo_index))
            else:
                source = export / f'photo-{photo_index:03}.jpg'
                crop_photo(folder / job.photos[photo_index]['file'], *focus.get(photo_index, (0.5, 0.5))).save(source, quality=96)
                inputs = ['-loop', '1', '-framerate', '30', '-i', source]
                # Render at 2x before zoom to avoid fractional-pixel jitter.
                base = f"scale=2160:3840,zoompan=z='1+0.035*on/{max(count-1,1)}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=1080x1920:fps=30,setsar=1"
            encoding = ['-an', '-frames:v', count, '-r', '30', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-crf', '18', '-preset', 'fast',
                        '-threads', '2', '-movflags', '+faststart']
            filters = [f'[0:v]{base},split=2[clean][base]']
            current = 'base'
            for part, (caption, start, end) in enumerate(caption_paths):
                inputs += ['-loop', '1', '-i', caption]
                local_start, local_end = start - (offset - scene_start), end - (offset - scene_start)
                output = f'cap{part}'
                filters.append(f"[{current}][{part+1}:v]overlay=0:0:format=auto:enable='gte(t,{local_start})*lt(t,{local_end})'[{output}]")
                current = output
            if badge:
                inputs += ['-loop', '1', '-i', badge]
                filters.append(f'[{current}][{len(caption_paths)+1}:v]overlay=0:0:format=auto[badge]')
                current = 'badge'
            filters.append(f'[{current}]format=yuv420p[v]')
            commands.append((*inputs, '-filter_complex_threads', '2', '-filter_complex', ';'.join(filters),
                             '-map', '[clean]', *encoding, clean_clip, '-map', '[v]', *encoding, clip))
            clips.append(clip)
            clean_clips.append(clean_clip)
            timeline.append({'photo': photo_index, 'start': offset, 'frames': count, 'motion': is_moving})
            offset += count / 30
    encode_clips(commands, progress, workers)
    video_list = export / 'video-list.txt'
    audio_list = export / 'audio-list.txt'
    clean_list = export / 'clean-list.txt'
    video_list.write_text(''.join(f"file '{p.name}'\n" for p in clips), encoding='utf-8')
    audio_list.write_text(''.join(f"file '{p.name}'\n" for p in audio_clips), encoding='utf-8')
    clean_list.write_text(''.join(f"file '{p.name}'\n" for p in clean_clips), encoding='utf-8')
    narration = export / 'narration.wav'
    if narrated:
        run('-f', 'concat', '-safe', '0', '-i', audio_list, '-c:a', 'pcm_s16le', narration)
    publish = export / 'publish'
    publish.mkdir(exist_ok=True)
    result = folder / 'stay-reel.mp4'
    pending = publish / 'stay-reel.mp4'
    clean_pending = publish / 'stay-video.mp4'
    voice_pending = publish / 'stay-voice.mp3'
    run('-f', 'concat', '-safe', '0', '-i', clean_list, '-an', '-c:v', 'copy',
        '-movflags', '+faststart', '-t', offset, clean_pending)
    if narrated:
        run('-i', narration, '-c:a', 'libmp3lame', '-q:a', '2', '-t', offset, voice_pending)
        run('-f', 'concat', '-safe', '0', '-i', video_list, '-i', narration, '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-t', offset, pending)
    else:
        run('-f', 'concat', '-safe', '0', '-i', video_list, '-an', '-c:v', 'copy',
            '-movflags', '+faststart', '-t', offset, pending)
    progress('최종 영상 전체를 디코딩하여 검사하고 있습니다.')
    verify(pending)
    verify(clean_pending)
    if narrated:
        run('-i', voice_pending, '-f', 'null', '-')
    (publish / 'stay-reel.srt').write_text('\n'.join(subtitles), encoding='utf-8')
    (publish / 'stay-script.txt').write_text('\n\n'.join(scene.text for scene in job.storyboard.scenes) + '\n', encoding='utf-8-sig')
    run('-i', pending, '-frames:v', '1', publish / 'preview.jpg')
    report = {'width': 1080, 'height': 1920, 'fps': 30, 'duration': offset,
              'frames': sum(item['frames'] for item in timeline), 'photos': len(timeline),
              'timeline': timeline, 'caption': job.options.caption.model_dump(), 'fully_decoded': True,
              'exports': {'final': 'stay-reel.mp4', 'video': 'stay-video.mp4',
                          **({'audio': 'stay-voice.mp3'} if narrated else {}),
                          'script': 'stay-script.txt', 'srt': 'stay-reel.srt'}}
    (publish / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    # All deliverables are prepared before making the new set available.
    prefix = f'preview-{render_revision(job)}-' if preview else ''
    for name in ('stay-reel.mp4', 'stay-video.mp4', 'stay-voice.mp3', 'stay-reel.srt',
                 'stay-script.txt', 'preview.jpg', 'verification.json'):
        if name == 'stay-voice.mp3' and not narrated:
            continue
        (publish / name).replace(folder / (prefix + name))
    warnings = []
    minimum, maximum = (20, 28) if longest else (25, 35) if narrated else (16, 20)
    if not minimum <= offset <= maximum:
        warnings.append(f'실제 길이가 {offset:.1f}초입니다. 권장 길이는 {minimum}~{maximum}초입니다.')
    if longest:
        # Longer shots are already split by a punch-in; a short one can only be fixed by using fewer segments.
        short = sum(item['frames'] / len(motion_cuts(item['frames'], longest=longest)) < MIN_CUT * 30 - 1
                    for item in timeline)
        if short:
            warnings.append(f'{short}개 컷이 {MIN_CUT:g}초보다 짧습니다. 그 장면의 구간 수를 줄여 보세요.')
    elif offset / len(timeline) < 1.5:
        warnings.append('사진당 평균 노출이 1.5초보다 짧습니다. 사진 수를 줄여 보세요.')
    if timeline[-1]['frames'] < 30:
        warnings.append('마지막 사진의 노출이 1초보다 짧습니다.')
    return {'file': prefix + result.name, 'video': prefix + 'stay-video.mp4',
            **({'audio': prefix + 'stay-voice.mp3'} if narrated else {}),
            'script': prefix + 'stay-script.txt', 'srt': prefix + 'stay-reel.srt', 'verification': prefix + 'verification.json',
            'duration': offset, 'frames': report['frames'], 'warnings': warnings}


def publish_preview(job):
    folder = job_path(job.id)
    names = {'file': 'stay-reel.mp4', 'video': 'stay-video.mp4', 'audio': 'stay-voice.mp3',
             'script': 'stay-script.txt', 'srt': 'stay-reel.srt', 'verification': 'verification.json'}
    if not domains.load(job.domain).features.narration:
        names.pop('audio')
    for key in names:
        if not (folder / job.preview[key]).is_file():
            raise ValueError('미리보기 파일이 없습니다. 미리보기를 다시 만들어 주세요.')
    for key, name in names.items():
        pending = folder / (name + '.pending')
        shutil.copyfile(folder / job.preview[key], pending)
        pending.replace(folder / name)
    return {**job.preview, **names}
