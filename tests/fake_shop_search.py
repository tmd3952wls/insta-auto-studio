"""가짜 소재 찾기: shop_auto_search 의 `python -m sas.auto <링크>` 와 같은 줄을 출력한다 (화면·자동 추가 시험용).

  python tests/fake_shop_search.py <링크> [--delay 초] [--fail 메시지] [영상 파일 ...]

스튜디오에서 쓰려면 데이터 폴더의 shop-search.json 에
{"command": ["python", "-u", "<이 파일>", "{url}", "--delay", "2", "영상1.mp4", ...]} 를 적는다.
"""
import argparse
import json
import sys
import time
from pathlib import Path


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('--delay', type=float, default=1.0)
    parser.add_argument('--fail', default='')
    parser.add_argument('files', nargs='*')
    args = parser.parse_intermixed_args()

    def say(line):
        print(line, flush=True)
        time.sleep(args.delay)

    say('STEP: 1/6 원본 영상 받는 중')
    say('STEP: 2/6 Claude가 제품을 확인하고 중국어 검색어를 정하는 중')
    say('PRODUCT: 지문 인식 USB 메모리')
    say('KEYWORDS: 指纹U盘 / 指纹加密U盘')
    say('STEP: 3/6 도우인·틱톡에서 영상 찾는 중 (퍼즐이 뜨면 맞춰 주세요)')
    say('NOTICE: 도우인 창에 퍼즐이 떴어요. 브라우저에서 직접 맞춰 주세요.')
    say('  도우인 검색 결과 12개')
    if args.fail:
        print(f'ERROR: {args.fail}', flush=True)
        sys.exit(1)
    folder = Path(args.files[0]).parent if args.files else Path('.')
    say(f'JOB_DIR: {folder}')
    say(f'STEP: 4/6 Claude가 후보 {len(args.files)}개 중 쓸 만한 영상을 고르는 중')
    say(f'STEP: 5/6 vmake로 자막 지우는 중 ({len(args.files)}개)')
    say('STEP: 6/6 최종 폴더에 모으는 중')
    files = [{'file': str(Path(f).resolve()), 'how': '확대로 자막 가림' if 'crop916' in f else 'vmake 자막 삭제',
              'quality': 'A', 'duration': 10, 'reason': '원본과 같은 제품'} for f in args.files]
    print('RESULT_JSON: ' + json.dumps({'product': '지문 인식 USB 메모리', 'keywords': ['指纹U盘'], 'job_dir': str(folder),
                                       'final_dir': str(folder / '최종'), 'files': files}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
