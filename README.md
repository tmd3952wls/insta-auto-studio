# Stay Studio · 숙소 세로 영상 자동 제작

숙소 사진만 넣으면 **대본 · 내레이션 · AI 움직임 · 자막**이 들어간 인스타그램용 세로 영상(1080×1920)을
만들어 주는 Windows 로컬 프로그램입니다. 사진을 직접 첨부하거나 여기어때 숙소 링크에서 가져올 수 있고,
유료 생성 전에 무료 견적을 보여 주고 승인을 받은 뒤 만듭니다.

## 주요 기능

- **사진 입력**: 파일 첨부, 또는 여기어때 국내 숙소 상세 링크에서 사진을 수집해 골라 추가
- **숙소(음성O)**: 짧은 후킹 뒤 자연스럽게 이어지는 본문을 가진 대본 후보 3개와 내레이션 생성
- **숙소(음성X)**: 직접 또는 AI 추천으로 고른 사진 8~10장을 장당 2초씩 표시하는 16~20초 무음 영상. 사진마다 좌측 아래 두 줄 자막, 마지막 둘째 줄은 `댓글 남겨주세요`로 고정
- **이름별 자막 설정**: 크기(20~120)·색상·위치·외곽선·배경·그림자·왼쪽/가운데/오른쪽 정렬을 이름으로 저장하고 다른 작업에서 선택. 같은 이름으로 저장하면 갱신하며 음성 속도는 포함하지 않음
- **고화질 변환 · 인물 제거**: Codex 이미지 생성으로 사진을 9:16 고화질로 다시 만들고 사람·모자이크 제거, 화사하게 보정 (사진별 HD/원본 전환)
- **고화질 + 대본 함께 만들기**: 버튼 하나로 원본 사진 분석과 고화질 변환·인물 제거를 동시에 진행. 영상·음성 생성 전에는 기존 대본이 있어도 다시 실행 가능하며 이미 변환한 사진은 재사용
- **AI 구성 추천**: 후보 사진 중에서 장수·순서·선정 이유를 추천받고 고화질 변환 후 대본을 만들거나, 직접 순서를 정해 구성
- **대본 · 자막 편집**: 대본이 그대로 내레이션과 화면 자막이 됨. 글꼴·크기·위치·색상·음성 속도 조절
- **사진별 대본과 자막 확인**: 숙소 사진마다 대본 칸 하나를 표시하고, 미리보기 아래 자막 버튼으로 실제 표시되는 짧은 자막을 순서대로 확인. 기존에 사진을 묶어 쓴 대본은 사진별로 나눠 저장 가능
- **AI 움직임**: 선택한 사진을 Fish MCP(Seedance 2.0 등)로 짧은 영상 클립으로 생성, 사진별 프롬프트 편집
- **내레이션**: Fish Story Studio의 Drama3(`drama-3`) + 일반여성2 목소리로 고정
- **견적 → 승인 → 생성**: 움직임·음성의 크레딧 견적을 먼저 확인하고, 모델·목소리·가격이 바뀌면 생성 중단
- **재합성**: 자막·속도·사진 순서만 바꿀 때는 기존 음성·클립을 재사용해 크레딧 없이 다시 합성
- **음성O 움직임 연결**: 내레이션 길이에 맞춰 원본 움직임을 끝까지 재생하고, 늘어난 구간은 프레임 보간으로 연결. 컷 중간의 급격한 확대와 긴 끝 프레임 정지를 제거
- **이어서 하기**: 작업과 생성 ID를 저장해 프로그램을 다시 열어도 이어서 진행, 유료 요청을 반복하지 않음

## 동작 흐름

```
사진 첨부 / 링크 수집
   → 고화질 변환 + 사진 분석·대본 작성 동시 진행 (Codex)
   → 대본·자막·움직임 편집
   → 크레딧 견적 확인 & 승인
   → 움직임 클립 + 내레이션 생성 (Fish MCP)
   → 로컬 합성 (FFmpeg) → 미리보기 → 내보내기
```

## 결과물

| 파일 | 내용 |
| --- | --- |
| 완성본 MP4 | 자막 · 내레이션 포함, 1080×1920 / 30fps / H.264 |
| 영상 MP4 | 자막 · 음성 없는 영상 |
| 자막대본 | 대본 TXT + 시간 자막 SRT |
| 음성파일 | 영상 순서·속도에 맞춘 내레이션 MP3 |

## 필요한 것

- Windows 10/11 64비트, 인터넷 연결
- **ChatGPT 구독 계정** (Codex 로그인 — 사진 분석·대본·고화질 변환)
- **Fish Audio 패키지 크레딧** (Fish MCP OAuth 로그인 — 움직임·음성 생성)

개발자 API 키는 쓰지 않습니다. 로그인은 각 PC에서 직접 하며, Fish 토큰은 Windows DPAPI로 암호화해 저장합니다.

## 사용하기 (배포본)

1. `dist/StayStudio-Windows.zip`을 압축 해제하고 `StayStudio.exe` 실행
2. 브라우저에 `http://127.0.0.1:8765`가 열리면 **계정 연결**에서 Codex와 Fish MCP 연결
3. 사진을 넣고 화면 상단의 제작 순서를 따라 진행

밖에서 휴대폰으로 쓰려면 PC를 켜 둔 채 Tailscale로 접속합니다(`tailscale serve --bg 8765`).

자세한 사용법은 [친구 PC 사용법](docs/친구-PC-사용법.md)을 보세요.

## 개발 환경

Python 3.11 기준입니다.

```powershell
python -m pip install --target .tools/studio-python -r requirements-studio.txt
python scripts/install_browser.py     # Playwright 브라우저 설치
python launch.py                      # 서버 실행 + 브라우저 열기 (기본 포트 8765)
python scripts/test_studio.py         # 테스트
python scripts/build_windows.py       # dist/StayStudio, dist/StayStudio-Windows.zip 생성
```

`launch.py --no-browser --port 8766`처럼 브라우저 자동 열기를 끄거나 포트를 바꿀 수 있습니다.

## 프로젝트 구조

```
launch.py              실행 진입점 (127.0.0.1 전용, EXE 진입점 겸용)
studio/
  app.py               FastAPI 서버와 API
  pipeline.py          분석 → 견적 → 생성 → 합성 작업 흐름
  codex.py             Codex 로그인·사진 분석·대본 작성
  enhance.py           Codex 이미지 생성으로 고화질 변환
  composition.py       사진 구성·AI 추천 구성
  recommendation.py    움직임 추천
  fish.py, drama.py    Fish MCP 연결, 움직임·Drama3 음성 생성
  collector.py, yeogi.py  여기어때 링크 사진 수집
  render.py, media.py  크롭·자막·FFmpeg 합성
  store.py, paths.py   작업 저장과 경로
  skills/              대본·고화질 변환 프롬프트
  web/                 화면 (index.html, app.js, style.css)
scripts/               빌드·설치·검사(check_*)·테스트 스크립트
assets/fonts/          페이퍼로지 Bold (OFL)
docs/                  사용법, 설계·진행 기록
Data/                  테스트용 사진·참고 영상
```

## 데이터 저장 위치

| 구분 | 개발 실행 | 배포본 |
| --- | --- | --- |
| 작업 | `.local/jobs` | `%LOCALAPPDATA%\StayStudio\jobs` |
| 링크 수집 | `.local/collections` | `%LOCALAPPDATA%\StayStudio\collections` |

같은 링크를 다시 수집하면 정상 저장된 사진을 재사용합니다. 새 작업은 기존 결과물을 덮어쓰지 않습니다.

## 제한 사항

- 링크 수집은 여기어때 **국내 숙소 상세 페이지**만 지원합니다.
- 사진은 최대 60장, 장당 20MB, 짧은 변 300px 이상이어야 합니다.
- 고화질 변환은 가로로 넓은 사진의 위아래를 AI가 새로 그리므로 실제 숙소와 다르지 않은지 확인이 필요합니다.
- 사진과 대본은 분석을 위해 Codex로, 움직임용 사진은 Fish로 전송됩니다. 화면과 합성은 내 PC에서 실행됩니다.


## 참고: 첫 시범 영상 (17장)

프로그램 이전에 `Data/Test_Data` 사진 17장으로 만든 시범 영상이 `output/stay_reel.mp4`에 있습니다
(대본·타임라인 `output/storyboard.json`, 자막 `output/stay_reel.srt`, 검증 결과 `output/verification.json`).
저장된 대본과 음성으로 다시 합성하려면 다음을 실행합니다. Fish 생성 호출은 하지 않습니다.

```powershell
python scripts/render_video.py
python scripts/verify_video.py
```
