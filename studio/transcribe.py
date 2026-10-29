"""원본 릴스의 말소리를 글로 받아 적기.

faster-whisper 로 이 컴퓨터에서 무료로 돌린다(그래픽카드용 추가 파일 없이 CPU, 30초 영상에 10초 안팎).
모델은 처음 한 번 인터넷에서 받아 두고(약 1.6GB) 그다음부터는 저장해 둔 것을 쓴다.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

MODEL = 'large-v3-turbo'
RATE = 16000
LOCK = threading.Lock()
_model = None


def audio(video: Path):
    """The clip's sound as 16 kHz mono floats, decoded by ffmpeg (faster-whisper's own decoder breaks on newer PyAV)."""
    import numpy as np
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-vn', '-f', 's16le', '-ac', '1', '-ar', str(RATE), '-'],
                         capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def transcribe(video: Path) -> str:
    """What the reel says, one spoken phrase per line; empty when it has no speech (music only)."""
    global _model
    sound = audio(video)
    if len(sound) < RATE:
        return ''
    with LOCK:  # One model in memory, used by one search at a time.
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(MODEL, device='cpu', compute_type='int8')
        segments, _ = _model.transcribe(sound, vad_filter=True, beam_size=5)
        lines = [segment.text.strip() for segment in segments if segment.text.strip()]
    return '\n'.join(lines)


def original_video(sheet: Path) -> Path | None:
    """The downloaded reel behind a search's frame sheet: shop_auto_search keeps `<name>_frames/sheet.jpg` next to
    `<name>.mp4`."""
    if not sheet.parent.name.endswith('_frames'):
        return None
    stem = sheet.parent.name[:-len('_frames')]
    return next((path for path in sheet.parent.parent.glob(stem + '.*') if path.suffix.lower() in {'.mp4', '.mov', '.webm', '.mkv'}), None)
