from __future__ import annotations

import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .models import BadgeStyle, CaptionStyle
from .paths import font_path

SIZE = (1080, 1920)
PHRASE_CHARS = 8  # One short phrase per cue ("안녕하세요" / "남승진입니다") keeps eyes on the screen.
SPLIT_SECONDS = 1.8  # Reference reels cut every ~1.2s, so longer shots become two cuts.
CUT_ZOOMS = (1.0, 1.15)  # Wide, then a punch-in on the same shot.


def crop_photo(path: Path, focus_x=0.5, focus_y=0.5) -> Image.Image:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
    width, height = image.size
    target_ratio = SIZE[0] / SIZE[1]
    if width / height > target_ratio:
        crop_width = height * target_ratio
        left = min(max(0, width * focus_x - crop_width / 2), width - crop_width)
        box = (left, 0, left + crop_width, height)
    else:
        crop_height = width / target_ratio
        top = min(max(0, height * focus_y - crop_height / 2), height - crop_height)
        box = (0, top, width, top + crop_height)
    return image.crop(box).resize(SIZE, Image.Resampling.LANCZOS)


def crop_video_frame(path: Path, crop: dict) -> Image.Image:
    """A source-video frame zoomed into 1080x1920 the way render.crop_filter does it."""
    with Image.open(path) as source:
        image = source.convert('RGB')
    zoom, x, y = crop.get('zoom', 1.0), crop.get('x', 0.5), crop.get('y', 0.5)
    scale = max(SIZE[0] * zoom / image.width, SIZE[1] * zoom / image.height)
    image = image.resize((max(SIZE[0], round(image.width * scale)), max(SIZE[1], round(image.height * scale))),
                         Image.Resampling.LANCZOS)
    left, top = round((image.width - SIZE[0]) * x), round((image.height - SIZE[1]) * y)
    return image.crop((left, top, left + SIZE[0], top + SIZE[1]))


def wrap_caption(text: str, font, width=940, outline=8) -> str:
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    lines, line = [], ''
    for char in text.strip():
        if char == '\n':
            lines.append(line)
            line = ''
        elif draw.textlength(line + char, font=font) + 2 * outline > width and line:
            split = line.rfind(' ')
            if split > len(line) / 2:
                lines.append(line[:split])
                line = line[split + 1:] + char
            else:
                lines.append(line)
                line = char.lstrip()
        else:
            line += char
    if line:
        lines.append(line)
    return '\n'.join(lines)


def caption_image(text: str, style: CaptionStyle | None = None) -> Image.Image:
    style = style or CaptionStyle()
    font = ImageFont.truetype(str(font_path()), style.font_size)
    if len(text.strip().splitlines()) == 2:
        # Keep explicit two-line copy intact even with a large saved font size.
        measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        size = style.font_size
        while size > 20 and max(measure.textlength(line, font=font) for line in text.splitlines()) + 2 * style.outline > 940:
            size -= 1
            font = ImageFont.truetype(str(font_path()), size)
    text = wrap_caption(text, font, outline=style.outline)
    layer = Image.new('RGBA', SIZE)
    draw = ImageDraw.Draw(layer)
    center = style.center(*SIZE)
    kwargs = dict(font=font, anchor='mm', align=style.align, spacing=round(font.size * 0.12),
                  stroke_width=style.outline)
    bounds = draw.multiline_textbbox(center, text, **kwargs)
    # Keep a moved or left-aligned block inside the portrait's safe margins.
    dx = max(70 - bounds[0], 0) - max(bounds[2] - (SIZE[0] - 70), 0)
    dy = max(70 - bounds[1], 0) - max(bounds[3] - (SIZE[1] - 70), 0)
    center = (center[0] + dx, center[1] + dy)
    bounds = draw.multiline_textbbox(center, text, **kwargs)
    if style.background_opacity:
        draw.rectangle(bounds, fill=(0, 0, 0, round(255 * style.background_opacity)))
    if style.shadow:
        shadow = Image.new('RGBA', SIZE)
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.multiline_text((center[0] + style.shadow, center[1] + style.shadow), text,
                                  fill='black', stroke_fill='black', **kwargs)
        layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(1)))
    ImageDraw.Draw(layer).multiline_text(center, text, fill=style.color, stroke_fill='black', **kwargs)
    return layer


def badge_image(text: str, style: BadgeStyle | None = None) -> Image.Image:
    """A small fixed notice (e.g. [광고]) pinned `x` pixels in from the chosen side and `y` down from the top."""
    style = style or BadgeStyle()
    font = ImageFont.truetype(str(font_path()), style.size)
    layer = Image.new('RGBA', SIZE)
    right = style.side == 'right'
    ImageDraw.Draw(layer).text((SIZE[0] - style.x if right else style.x, style.y), text, font=font,
                               anchor='rt' if right else 'lt', fill='white',
                               stroke_width=max(2, style.size // 10), stroke_fill='black')
    return layer


def split_words(text: str, limit: int) -> list[str]:
    lines, line = [], ''
    for word in text.split():
        if line and len(line) + 1 + len(word) > limit:
            lines.append(line)
            line = word
        else:
            line = f'{line} {word}'.strip()
    return lines + [line] if line else lines


def caption_phrases(text: str) -> list[str]:
    """Split narration into short on-screen phrases at punctuation, then evenly by words when a phrase runs long."""
    phrases = []
    for part in re.split(r'(?<=[,.?!])\s+|\n', text.strip()):
        part = part.strip().rstrip(',.').strip()
        count = len(split_words(part, PHRASE_CHARS))
        # Even line lengths avoid a lone trailing word ("음식 해 먹기" / "좋고" -> "음식 해" / "먹기 좋고").
        limit = math.ceil(len(part) / max(count, 1))
        while len(split_words(part, limit)) > count:
            limit += 1
        phrases += split_words(part, limit)
    return phrases


def caption_segments(text: str, style: CaptionStyle) -> list[str]:
    font = ImageFont.truetype(str(font_path()), style.font_size)
    # One line per cue; a phrase too wide for the frame still wraps into separate cues.
    return [line for phrase in caption_phrases(text)
            for line in wrap_caption(phrase, font, outline=style.outline).splitlines()] or ['']


def preview_image(photo: Path, text: str, style: CaptionStyle, focus=(0.5, 0.5)):
    image = crop_photo(photo, *focus).convert('RGBA')
    image.alpha_composite(caption_image(text, style))
    return image.convert('RGB')


def allocate_frames(audio_seconds: float, motion_flags: list[bool], fps=30) -> list[int]:
    if not motion_flags:
        raise ValueError('사진이 없습니다.')
    # No tail padding: narration already ends with its sentence pause, so scenes run back to back.
    total = max(math.ceil(audio_seconds * fps), len(motion_flags) * 9)
    # Photos without a generated shot (older jobs) get less time than moving ones.
    weights = [2 if moving else 1 for moving in motion_flags]
    remaining = total - len(weights) * 9
    weight_sum = sum(weights)
    raw = [remaining * weight / weight_sum for weight in weights]
    counts = [9 + math.floor(value) for value in raw]
    for index in sorted(range(len(raw)), key=lambda i: raw[i] % 1, reverse=True)[:total - sum(counts)]:
        counts[index] += 1
    return counts


def motion_cuts(frames: int, fps=30, longest=None) -> list[int]:
    """Start frames of the cuts one photo's shot is split into: two once it lasts SPLIT_SECONDS, else one.
    With `longest` (video domains) a shot is split only as often as needed to keep every cut within that many seconds."""
    if longest:
        count = max(1, math.ceil(frames / (longest * fps)))
    else:
        count = len(CUT_ZOOMS) if frames >= SPLIT_SECONDS * fps else 1
    return [frames * index // count for index in range(count)]


def cut_zoom(frames: int, fps=30, longest=None) -> str:
    """ffmpeg zoom expression that punches in at each cut, so one clip reads as several shots."""
    starts = motion_cuts(frames, fps, longest)
    zooms = [CUT_ZOOMS[index % len(CUT_ZOOMS)] for index in range(len(starts))]
    expression = str(zooms[-1])
    for index in range(len(starts) - 1, 0, -1):
        expression = f'if(lt(on,{starts[index]}),{zooms[index - 1]},{expression})'
    return expression


def srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}'
