"""Finds text burned into a source video (Chinese subtitles, watermarks) and the least zoom that pushes it out of a 9:16 frame.

Overlay text is told apart from text printed on things in the scene (labels, packaging):
 - subtitles: Chinese text centred at one height whose wording keeps changing, or that stays there across most frames;
 - watermarks: the same text in the same spot through most of the clip.
Everything else is left visible.
"""
from __future__ import annotations

import json
import math

from .render import video_frame, video_info

SAMPLES = 10  # Frames read per source.
MARGIN = 0.012  # Clearance kept around found text, as a share of the frame.
ICON = 0.06  # Extra width around a watermark for the logo or dot that text reading misses.
MAX_ZOOM = 1.6  # Beyond this the picture gets too soft; such clips need the subtitles erased instead.
ASPECT = 9 / 16


def chinese(text):
    return sum('一' <= char <= '鿿' for char in text)


def distinct(texts):
    """How many different lines there are; misreadings of one line count once."""
    kept = []
    for text in sorted(texts, key=len, reverse=True):
        if all(len(set(text) & set(other)) < 0.5 * min(len(set(text)), len(set(other))) for other in kept):
            kept.append(text)
    return len(kept)


_engine = None


def read_text(image_path):
    """Text boxes in one frame as {'text', 'box': (x0, y0, x1, y1) in 0~1}, or [] when no reader is installed."""
    global _engine
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return []
    from PIL import Image
    _engine = _engine or RapidOCR()
    with Image.open(image_path) as image:
        width, height = image.size
    found, _ = _engine(str(image_path))
    return [{'text': text, 'box': (min(p[0] for p in box) / width, min(p[1] for p in box) / height,
                                   max(p[0] for p in box) / width, max(p[1] for p in box) / height)}
            for box, text, score in (found or []) if float(score) > 0.5]


def overlays(frames):
    """Boxes of overlay text (subtitles and watermarks) from per-frame text boxes."""
    groups = []  # Text at about the same height.
    for index, frame in enumerate(frames):
        for item in frame:
            middle = (item['box'][1] + item['box'][3]) / 2
            group = next((g for g in groups if abs(g['middle'] - middle) < 0.025), None)
            if group is None:
                group = {'middle': middle, 'items': []}
                groups.append(group)
            group['items'].append((index, item))
    hidden = []
    for group in groups:
        seen = {index for index, _ in group['items']}
        texts = {item['text'] for _, item in group['items']}
        boxes = [item['box'] for _, item in group['items']]
        centred = sum(abs((b[0] + b[2]) / 2 - 0.5) < 0.08 for b in boxes) / len(boxes) > 0.7
        han = any(chinese(text) for text in texts)
        subtitle = han and centred and len(seen) >= 3 and (distinct(texts) >= 3 or len(seen) >= 0.6 * len(frames))
        centres = [(b[0] + b[2]) / 2 for b in boxes]
        steady = max(centres) - min(centres) < 0.05 and len(texts) <= 3  # Pinned to the screen, not to an object.
        watermark = len(frames) >= 3 and len(seen) >= 0.7 * len(frames) and steady
        if subtitle or watermark:
            extra = 0 if subtitle else ICON
            hidden.append({'kind': 'subtitle' if subtitle else 'watermark', 'sample': sorted(texts)[0],
                           'box': (min(b[0] for b in boxes) - extra, min(b[1] for b in boxes) - extra / 3,
                                   max(b[2] for b in boxes) + extra, max(b[3] for b in boxes) + extra / 3)})
    return hidden


def window(hidden, aspect):
    """The least-zoomed 9:16 window missing every box, as a CropBox dict, or None when MAX_ZOOM is not enough."""
    wide, tall = min(1, ASPECT / aspect), min(1, aspect / ASPECT)  # The unzoomed 9:16 window, as shares of the frame.
    steps = [n / 50 for n in range(51)]
    for tenth in range(0, round((MAX_ZOOM - 1) * 50) + 1):
        zoom = 1 + tenth / 50
        w, h = wide / zoom, tall / zoom
        best = None
        for x in steps:
            for y in steps:
                left, top = x * (1 - w), y * (1 - h)
                if all(b['box'][2] + MARGIN <= left or b['box'][0] - MARGIN >= left + w
                       or b['box'][3] + MARGIN <= top or b['box'][1] - MARGIN >= top + h for b in hidden):
                    miss = abs(left + w / 2 - 0.5) + abs(top + h / 2 - 0.5)  # Prefer the window nearest the centre.
                    if best is None or miss < best[0]:
                        best = (miss, x, y)
        if best:
            return {'zoom': round(zoom, 2), 'x': best[1], 'y': best[2]}
    return None


def crop_for(folder, source, index):
    """{'crop', 'hidden', 'reason'} for one uploaded source video, cached; crop is None when nothing needs hiding
    or zooming cannot hide it (then 'reason' says so)."""
    cache = folder / f"{source['file']}.text.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding='utf-8'))
    width, height, duration = video_info(folder / source['file'])
    frames_dir = folder / 'analysis-frames'
    frames_dir.mkdir(exist_ok=True)
    count = max(1, min(SAMPLES, math.floor(duration)))
    frames = []
    for n in range(count):
        seconds = round((n + 0.5) * duration / count, 2)
        image = frames_dir / f'text-{index:03}-{seconds:06.2f}s.jpg'
        if not image.is_file():
            video_frame(folder / source['file'], seconds, image, 576)
        frames.append(read_text(image))
    hidden = overlays(frames)
    crop = window(hidden, width / height) if hidden else None
    words = ', '.join(f"{'자막' if item['kind'] == 'subtitle' else '워터마크'} \"{item['sample'][:12]}\"" for item in hidden)
    if not hidden:
        reason = ''
    elif crop:
        reason = f'글자 자동 감지: {words} → {crop["zoom"]:.2f}배 확대로 화면 밖으로 밀어냄'
    else:
        reason = f'글자 자동 감지: {words} → 확대로는 다 가릴 수 없어요. Vmake로 지운 영상을 써 주세요.'
    result = {'crop': crop, 'hidden': hidden, 'reason': reason}
    cache.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result
