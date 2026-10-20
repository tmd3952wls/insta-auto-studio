from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

VIDEO_MODEL = 'seedance-2.0-mini'  # Cheapest 720p model that takes several reference photos per clip.
VOICE_ID = '4a81bbbb5cd44f22922bf934cb93b369'
DRAMA_MODEL = 'drama-3'  # Fish rejects the old name 'drama-3-preview' since 2026-10-14.
MOTION_PARAMETERS = {'aspect_ratio': '9:16', 'resolution': '720p', 'duration': '4s'}
MIN_MOTION_SIDE = 720  # Shorter side of an accepted Fish motion clip; smaller results are rejected.
MIN_PHOTOS = 10
TARGET_SECONDS = 25  # Average reel length; every photo shares it.
CLIP_PHOTOS = 4  # Reference images per clip; four kept each shot faithful in the 2026-10-09 test.
MIN_CLIP_SECONDS = 4  # Shortest clip Fish video models accept.
MAX_CLIP_SECONDS = 15  # Longest clip a Motion may ask for (Motion.seconds).
MIN_SEGMENT, MAX_SEGMENT = 0.5, 8.0  # Seconds of source video one shot may use.
MIN_CUT, MAX_CUT = 1.0, 2.0  # Seconds one cut of a video-domain reel stays on screen.
SYLLABLES_PER_SECOND = 7.5  # Drama3 reading speed measured on the 2026-10-13 shopping reel.
SCRIPT_CHARS = 600  # Longest user-written script.


def script_scenes(text: str) -> list[tuple[str, int]]:
    """A user's own script as (scene text, segment count): one voice scene per line, a line too short to hold
    a cut joined to its neighbour, and one segment per ~2 s of speech so every cut stays within MIN_CUT~MAX_CUT."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1:  # One pasted paragraph: a scene per sentence.
        lines = [part for part in re.split(r'(?<=[.?!])\s+', lines[0]) if part.strip()]
    seconds = lambda line: sum(char.isalnum() for char in line) / SYLLABLES_PER_SECOND
    join = lambda first, second: first + (' ' if re.search(r'[,.?!…~]$', first) else ', ') + second
    scenes = []
    for line in lines:
        if scenes and seconds(scenes[-1]) < MIN_CUT + 0.1:
            scenes[-1] = join(scenes[-1], line)
        else:
            scenes.append(line)
    if len(scenes) > 1 and seconds(scenes[-1]) < MIN_CUT + 0.1:
        scenes[-2:] = [join(scenes[-2], scenes[-1])]
    return [(scene, min(3, max(1, math.ceil(seconds(scene) / MAX_CUT)))) for scene in scenes]


def plan_clips(order: list[int], total=TARGET_SECONDS) -> list[dict]:
    """Group the photo order into 3-4 photo clips whose whole-second lengths add up to the target."""
    if not order:
        return []
    count = -(-len(order) // CLIP_PHOTOS)
    # Few-photo domains: add clips until the longest fits what a model accepts.
    while count < len(order) and -(-len(order) // count) * total / len(order) > MAX_CLIP_SECONDS:
        count += 1
    sizes = [len(order) // count + (index < len(order) % count) for index in range(count)]
    raw = [size * total / len(order) for size in sizes]
    seconds = [int(value) for value in raw]
    for index in sorted(range(count), key=lambda i: raw[i] - seconds[i], reverse=True)[:round(total) - sum(seconds)]:
        seconds[index] += 1
    clips, start = [], 0
    for size, length in zip(sizes, seconds):
        clips.append({'photos': order[start:start + size], 'seconds': max(MIN_CLIP_SECONDS, length)})
        start += size
    return clips


class CaptionStyle(BaseModel):
    font_size: int = Field(64, ge=20, le=120)
    x: float = Field(0, ge=-35, le=35)
    y: float = Field(13, ge=-35, le=35)
    color: str = Field('#CCCACC', pattern=r'^#[0-9a-fA-F]{6}$')
    outline: int = Field(8, ge=0, le=16)
    background_opacity: float = Field(0.2, ge=0, le=1)
    shadow: int = Field(3, ge=0, le=12)
    align: Literal['left', 'center', 'right'] = 'center'

    def center(self, width=1080, height=1920):
        # VLLO screenshot calibration: positive Y moves upward from center.
        return round(width * (0.5 + self.x / 100)), round(height * (0.5 - self.y / 100))


class BadgeStyle(BaseModel):
    """Where a domain's fixed notice (shopping: [광고]) sits on the final reel."""
    side: Literal['left', 'right'] = 'right'
    x: int = Field(39, ge=0, le=500)  # Pixels in from that side of the 1080-wide frame.
    y: int = Field(44, ge=0, le=1800)  # Pixels down from the top.
    size: int = Field(40, ge=16, le=90)


class TitleStyle(CaptionStyle):
    """The thumbnail title's own look: larger and brighter than the running captions."""
    font_size: int = Field(110, ge=20, le=220)
    y: float = Field(18, ge=-35, le=35)
    color: str = Field('#FFE500', pattern=r'^#[0-9a-fA-F]{6}$')
    outline: int = Field(10, ge=0, le=24)
    background_opacity: float = Field(0, ge=0, le=1)
    shadow: int = Field(0, ge=0, le=12)


class Thumbnail(BaseModel):
    """Video domains: a still from a source video that opens the reel for 0.1 s so it becomes the cover."""
    enabled: bool = True
    text: str = Field('', max_length=60)
    source: int | None = Field(None, ge=0)  # None: the first segment's own first frame.
    time: float = Field(0, ge=0)
    style: TitleStyle = Field(default_factory=TitleStyle)


class Scene(BaseModel):
    id: str = ''
    photos: list[int] = Field(min_length=1)
    text: str = Field(max_length=160)  # Empty text is allowed for caption-free silent scenes.

    @model_validator(mode='after')
    def identity(self):
        if not self.id:
            self.id = stable_hash({'photos': self.photos, 'text': self.text})[:24]
        return self


class MotionRecommendation(BaseModel):
    photo: int = Field(ge=0)
    recommended: bool
    reason: str = Field(min_length=1, max_length=300)
    prompt: str = Field(default='', max_length=2000)
    explanation: str = Field(default='', max_length=500)


class Motion(BaseModel):
    """One generated clip. `photo` is its first photo; `photos` are the reference images shown as hard-cut shots."""
    photo: int = Field(ge=0)
    prompt: str = Field(min_length=1, max_length=2000)
    photos: list[int] = Field(default_factory=list, max_length=9)
    seconds: int = Field(4, ge=MIN_CLIP_SECONDS, le=MAX_CLIP_SECONDS)

    @field_validator('prompt', mode='before')
    @classmethod
    def trim_prompt(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def first_photo(self):
        if not self.photos:
            self.photos = [self.photo]
        if self.photos[0] != self.photo or len(set(self.photos)) != len(self.photos):
            raise ValueError('클립의 첫 사진과 사진 목록이 맞지 않습니다.')
        return self


class CropFocus(BaseModel):
    photo: int = Field(ge=0)
    x: float = Field(0.5, ge=0, le=1)
    y: float = Field(0.5, ge=0, le=1)


class Segment(BaseModel):
    """A stretch of an uploaded source video used as one shot (video domains: Scene.photos index these)."""
    source: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    words: str = Field('', max_length=80)  # The part of the scene's narration heard while this segment shows.

    @model_validator(mode='after')
    def span(self):
        if not MIN_SEGMENT <= self.end - self.start <= MAX_SEGMENT:
            raise ValueError(f'영상 구간은 {MIN_SEGMENT}~{MAX_SEGMENT:g}초 길이여야 합니다.')
        return self


class CropBox(BaseModel):
    """How a source video is zoomed into 9:16 to push burned-in subtitles and watermarks out of frame."""
    zoom: float = Field(1.0, ge=1.0, le=2.5)
    x: float = Field(0.5, ge=0, le=1)
    y: float = Field(0.5, ge=0, le=1)
    user: bool = False  # Set once the user adjusts it, so a new analysis keeps their choice.


class ScriptOption(BaseModel):
    """One full narration candidate: a text per scene, sharing the storyboard's scene grouping."""
    title: str = Field(min_length=1, max_length=40)
    texts: list[str] = Field(min_length=1)
    thumbnail_text: str = Field('', max_length=60)  # Video domains: the two-line cover title for this candidate.
    thumbnail_source: int | None = Field(None, ge=0)  # The source video and second its cover still comes from.
    thumbnail_time: float = Field(0, ge=0)


class Storyboard(BaseModel):
    scenes: list[Scene] = Field(min_length=1)
    script_options: list[ScriptOption] = Field(default_factory=list, max_length=5)
    segments: list[Segment] = Field(default_factory=list)  # Video domains only; empty for photo storyboards.
    provenance: dict = Field(default_factory=dict)  # {domain, version, skill_hash, overridden} of the analysis that wrote it.
    script_choice: int = Field(0, ge=0)
    motion: list[Motion] = Field(default_factory=list)
    crop_focus: list[CropFocus] = Field(default_factory=list)
    motion_recommendations: list[MotionRecommendation] = Field(default_factory=list)
    selection_reasons: dict[str, str] = Field(default_factory=dict)

    def per_photo(self):
        """Expand grouped photo narration without changing the words or candidate order."""
        if self.segments or all(len(scene.photos) == 1 for scene in self.scenes):
            return self.model_copy(deep=True)
        def parts(text, count):
            words = text.split()
            if len(words) < count:
                return [text.strip()] * count
            result = []
            for remaining in range(count, 1, -1):
                target = len(' '.join(words)) / remaining
                boundary = min(range(1, len(words) - remaining + 2),
                               key=lambda n: abs(len(' '.join(words[:n])) - target))
                result.append(' '.join(words[:boundary]))
                words = words[boundary:]
            return result + [' '.join(words)]
        plan = self.model_copy(deep=True)
        plan.scenes = [Scene(photos=[photo], text=text) for scene in self.scenes
                       for photo, text in zip(scene.photos, parts(scene.text, len(scene.photos)))]
        for original, option in zip(self.script_options, plan.script_options):
            option.texts = [text for scene, value in zip(self.scenes, original.texts)
                            for text in parts(value, len(scene.photos))]
        return plan

    def validate_silent(self, photo_count):
        if not 8 <= photo_count <= 10 or len(self.scenes) != photo_count or any(len(s.photos) != 1 for s in self.scenes):
            raise ValueError('음성X는 8~10장의 사진에 장면을 하나씩 배치합니다. 대본을 다시 분석해 주세요.')
        def copy(text, last):
            if not text.strip():
                return ''
            lines = [line.strip() for line in text.strip().splitlines()]
            if len(lines) != 2 or any(not line or len(line) > 18 for line in lines):
                raise ValueError('음성X 자막은 줄마다 18자 이하인 두 줄로 작성해 주세요.')
            if last:
                lines[1] = '댓글 남겨주세요'
            return '\n'.join(lines)
        for index, scene in enumerate(self.scenes):
            scene.text = copy(scene.text, index == photo_count - 1)
        for option in self.script_options:
            option.texts = [copy(text, i == photo_count - 1) for i, text in enumerate(option.texts)]
        return self

    @classmethod
    def validate_plan(cls, value, photo_count: int, max_motion: int, expected_order=None, allow_empty_text=False):
        plan = cls.model_validate(value)
        if not allow_empty_text and any(not scene.text.strip() for scene in plan.scenes):
            raise ValueError('대본을 입력해 주세요.')
        used = [index for scene in plan.scenes for index in scene.photos]
        if sorted(used) != list(range(photo_count)):
            raise ValueError('모든 사진은 장면에 정확히 한 번씩 포함되어야 합니다.')
        if expected_order is not None and used != expected_order:
            raise ValueError('직접 구성의 사진 순서를 유지해야 합니다.')
        if len({scene.id for scene in plan.scenes}) != len(plan.scenes):
            raise ValueError('장면 ID가 중복되었습니다.')
        if any(len(option.texts) != len(plan.scenes) or not all(0 < len(text.strip()) <= 160 for text in option.texts)
               for option in plan.script_options):
            raise ValueError('대본 후보의 장면 수가 맞지 않거나 빈 문장이 있습니다.')
        if plan.script_choice >= max(1, len(plan.script_options)):
            raise ValueError('선택한 대본 후보 번호가 올바르지 않습니다.')
        indices = [index for item in plan.motion for index in item.photos]
        if len(plan.motion) > max_motion:
            raise ValueError('움직임 장면 수가 설정 범위를 벗어났습니다.')
        if len(set(indices)) != len(indices):
            raise ValueError('같은 사진이 여러 움직임 클립에 들어 있습니다.')
        if any(index >= photo_count for index in indices):
            raise ValueError('움직임 장면의 사진 번호가 올바르지 않습니다.')
        if any(focus.photo >= photo_count for focus in plan.crop_focus):
            raise ValueError('크롭 기준의 사진 번호가 올바르지 않습니다.')
        if len({f.photo for f in plan.crop_focus}) != len(plan.crop_focus):
            raise ValueError('크롭 기준이 중복되었습니다.')
        if any(r.photo >= photo_count for r in plan.motion_recommendations) or len({r.photo for r in plan.motion_recommendations}) != len(plan.motion_recommendations):
            raise ValueError('움직임 추천의 사진 번호가 올바르지 않습니다.')
        return plan

    @classmethod
    def validate_video_plan(cls, value, sources: list[dict]):
        """A video-domain plan: scenes use every segment once, in order, and segments stay inside their source."""
        plan = cls.model_validate(value)
        if not plan.segments:
            raise ValueError('영상 구간이 없습니다.')
        plan = cls.validate_plan(value, len(plan.segments), 0, list(range(len(plan.segments))))
        for segment in plan.segments:
            if segment.source >= len(sources) or sources[segment.source].get('kind') != 'video':
                raise ValueError('영상 구간의 소스 번호가 올바르지 않습니다.')
            if segment.end > sources[segment.source]['duration'] + 0.05:
                raise ValueError(f'{segment.source + 1}번 영상은 {sources[segment.source]["duration"]:.1f}초까지만 있습니다.')
        return plan


class CreateOptions(BaseModel):
    max_motion: int = Field(7, ge=0)  # Clips are paid per clip; each 4s clip is split into several cuts.
    caption: CaptionStyle = Field(default_factory=CaptionStyle)
    badge: BadgeStyle = Field(default_factory=BadgeStyle)
    thumbnail: Thumbnail = Field(default_factory=Thumbnail)
    speed: float = Field(1.0, ge=0.75, le=1.25)


class CaptionPresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    caption: CaptionStyle

    @field_validator('name', mode='before')
    @classmethod
    def clean_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class ApproveRequest(BaseModel):
    quote_id: str
    expected_credits: int = Field(ge=0)


class Job(BaseModel):
    id: str
    state: Literal['uploaded', 'preparing', 'analyzing', 'quoting', 'awaiting_approval', 'generating',
                   'rendering', 'complete', 'error', 'interrupted', 'recommending',
                   'previewing', 'preview_ready', 'enhancing'] = 'uploaded'
    message: str = '사진을 준비했습니다.'
    photos: list[dict] = Field(default_factory=list)  # Media items: photos, or source videos (kind='video').
    sources: list[dict] = Field(default_factory=list)
    options: CreateOptions = Field(default_factory=CreateOptions)
    storyboard: Storyboard | None = None
    quote: dict | None = None
    approval: dict | None = None
    generated: dict = Field(default_factory=dict)
    narration: dict = Field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    composition_mode: Literal['manual', 'ai'] = 'manual'
    photo_order: list[int] = Field(default_factory=list)
    target_count: int = Field(10, ge=1, le=60)
    candidates: list[dict] = Field(default_factory=list)
    selection_reasons: dict[str, str] = Field(default_factory=dict)  # AI pick reason by photo sha256.
    assets: dict[str, dict] = Field(default_factory=dict)
    attempts: dict[str, int] = Field(default_factory=dict)
    versions: list[dict] = Field(default_factory=list)
    preview: dict | None = None
    workflow_version: int = 1
    video_model: str = VIDEO_MODEL
    video_parameters: dict = Field(default_factory=lambda: dict(MOTION_PARAMETERS))
    model_probe: dict | None = None  # Uploaded sample image reused for free per-model estimates.
    domain: str = 'stay'  # Fixed at creation (validated by app.new_job); picks the schema, prompts and UI wording.
    batch: dict = Field(default_factory=dict)  # Shopping: one final reel per script candidate (quote, voices, results).
    facts: dict[str, str] = Field(default_factory=dict)  # User-entered schema properties, e.g. product name.

    def video_choice(self):
        return {'model': self.video_model, 'parameters': self.video_parameters}

    def default_video(self):
        return self.video_model == VIDEO_MODEL and self.video_parameters == MOTION_PARAMETERS


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def motion_fingerprint(photo_hash: str, prompt: str, model: str = VIDEO_MODEL, parameters: dict = MOTION_PARAMETERS) -> str:
    return stable_hash({'image': photo_hash, 'prompt': prompt, 'model': model, 'parameters': parameters})
