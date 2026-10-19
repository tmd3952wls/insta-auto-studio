"""인스타 릴스 링크로 쇼핑 소재 영상 찾기.

별도 프로그램 shop_auto_search 의 `python -u -m sas.auto <링크>` 를 한 번에 하나씩 차례로 실행한다
(그 프로그램의 브라우저 로그인 정보 폴더는 동시에 하나만 쓸 수 있음). 출력은 로그 파일로 받아 읽으므로
스튜디오가 다시 켜져도 그 프로그램은 계속 돌고, 다시 켜진 스튜디오가 이어서 읽는다.
끝나면 RESULT_JSON 의 최종 영상들을 `deliver` 로 넘겨 그 쇼핑 작업의 '올린 영상'으로 넣는다.

설정 파일 DATA/shop-search.json (없어도 됨): {"root": 프로그램 폴더, "command": ["실행", "인자", "{url}"]}
— 실제 검색 없이 화면을 시험할 때 가짜 출력 스크립트로 바꿔 끼우는 용도.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(r'C:\Users\tmd39\Desktop\shop_auto_search')
STEPS = ['원본 영상 받기', '제품 확인·중국어 검색어 정하기', '도우인·틱톡에서 영상 찾기',
         '쓸 만한 영상 고르기', 'vmake로 중국 자막 지우기', '최종 폴더에 모으기', '올린 영상에 넣기']
LINK = re.compile(r'^https?://(www\.)?instagram\.com/(reel|reels|p|tv)/[\w-]+', re.I)
LOCK = threading.RLock()
wake = None  # asyncio.Event: a new search was queued.


def folder():
    from . import paths  # Read at call time so tests can redirect the data folder.
    return paths.DATA / 'shop-searches'


def settings():
    from . import paths
    try:
        value = json.loads((paths.DATA / 'shop-search.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def command(url):
    custom = settings().get('command')
    if isinstance(custom, list) and custom and all(isinstance(part, str) for part in custom):
        return [part.replace('{url}', url) for part in custom]
    return [shutil.which('python') or sys.executable, '-u', '-m', 'sas.auto', url]


def save(search):
    with LOCK:
        folder().mkdir(parents=True, exist_ok=True)
        temporary = folder() / f'{search["id"]}.tmp'
        temporary.write_text(json.dumps(search, ensure_ascii=False, indent=1), encoding='utf-8')
        temporary.replace(folder() / f'{search["id"]}.json')


def all_searches():
    with LOCK:
        items = []
        for path in folder().glob('*.json') if folder().is_dir() else []:
            try:
                items.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                continue
        return sorted(items, key=lambda s: s['created'])


def read(search_id):
    with LOCK:
        path = folder() / f'{search_id}.json'
        if not re.fullmatch(r'[0-9a-f]{32}', search_id) or not path.is_file():
            raise ValueError('소재 찾기 기록을 찾지 못했어요.')
        return json.loads(path.read_text(encoding='utf-8'))


def public(search):
    """What the shopping tab shows: no log path or process id, plus the place in the queue."""
    value = {k: v for k, v in search.items() if k not in {'pid', 'offset'}}
    if search['state'] == 'waiting':
        ahead = [s for s in all_searches() if s['state'] in {'waiting', 'running'} and s['created'] < search['created']]
        value['ahead'] = len(ahead)
    value['steps'] = STEPS
    return value


def for_job(job_id):
    return [public(s) for s in reversed(all_searches()) if s['job_id'] == job_id]


def queue(job_id, url):
    url = url.strip()
    if not LINK.match(url):
        raise ValueError('인스타 릴스 링크를 넣어 주세요. 예: https://www.instagram.com/reel/…')
    if any(s['job_id'] == job_id and s['state'] in {'waiting', 'running'} for s in all_searches()):
        raise ValueError('이 작업은 이미 소재를 찾고 있어요. 끝난 뒤 다시 시도해 주세요.')
    search = {'id': uuid.uuid4().hex, 'job_id': job_id, 'url': url, 'state': 'waiting', 'created': time.time(),
              'step': 0, 'step_text': '차례를 기다리는 중', 'notice': '', 'product': '', 'keywords': '',
              'error': '', 'result': None, 'added': 0, 'skipped': [], 'log': [], 'pid': None, 'offset': 0}
    save(search)
    if wake:
        wake.set()
    return search


def cancel(search_id):
    search = read(search_id)
    if search['state'] not in {'waiting', 'running'}:
        raise ValueError('이미 끝난 소재 찾기예요.')
    if search['state'] == 'running' and search.get('pid'):
        # Stops the search and the browser windows it opened.
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(search['pid'])], capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    search = read(search_id)
    search.update(state='cancelled', notice='', error='', step_text='취소했어요.')
    save(search)
    return search


def alive(pid):
    """Whether a process started before a studio restart is still running (Windows)."""
    if not pid:
        return False
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def read_lines(search):
    """New complete lines of the search's output since the last read, applied to its state."""
    path = folder() / f'{search["id"]}.log'
    try:
        with open(path, 'rb') as file:
            file.seek(search['offset'])
            data = file.read()
    except OSError:
        return
    end = data.rfind(b'\n') + 1
    search['offset'] += end
    for raw in data[:end].decode('utf-8', 'replace').splitlines():
        line = raw.rstrip()
        if not line or 'Warning' in line:
            continue
        if line.startswith('STEP: '):
            number, _, text = line[6:].partition(' ')
            try:
                search['step'] = int(number.split('/')[0])
            except ValueError:
                pass
            search['step_text'], search['notice'] = text, ''
        elif line.startswith('NOTICE: '):
            search['notice'] = line[8:]
        elif line.startswith('PRODUCT: '):
            search['product'] = line[9:]
        elif line.startswith('KEYWORDS: '):
            search['keywords'] = line[10:]
        elif line.startswith('RESULT_JSON: '):
            try:
                search['result'] = json.loads(line[13:])
            except ValueError:
                search['error'] = '결과를 읽지 못했어요.'
        elif line.startswith('ERROR: '):
            search['error'] = line[7:]
        search['log'] = (search['log'] + [line[:400]])[-60:]


async def run(search, deliver):
    """Runs (or, after a restart, keeps watching) one search, then hands its videos to `deliver`."""
    process = None
    if search['state'] == 'waiting':
        root = Path(settings().get('root') or ROOT)
        if not root.is_dir():
            search.update(state='failed', error=f'소재 찾기 프로그램 폴더가 없어요: {root}')
            save(search)
            return
        log = open(folder() / f'{search["id"]}.log', 'wb')
        try:
            process = subprocess.Popen(command(search['url']), cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as error:
            search.update(state='failed', error=f'소재 찾기 프로그램을 실행하지 못했어요: {error}')
            save(search)
            return
        finally:
            log.close()
        search.update(state='running', pid=process.pid, step_text='시작하는 중')
        save(search)
    while process.poll() is None if process else alive(search['pid']):
        await asyncio.sleep(1)
        before = search['offset']
        read_lines(search)
        if search['offset'] != before:
            with LOCK:
                if read(search['id'])['state'] == 'cancelled':
                    return
                save(search)
    read_lines(search)
    if read(search['id'])['state'] == 'cancelled':
        return
    search['notice'] = ''
    if not search['result']:
        search.update(state='failed', error=search['error'] or '소재 찾기가 중간에 멈췄어요. 아래 진행 기록을 확인해 주세요.')
        save(search)
        return
    if not search['result'].get('files'):
        search.update(state='failed', error='찾은 영상이 0개예요. 도우인·틱톡 검색에서 영상을 받지 못했어요.')
        save(search)
        return
    search.update(step=len(STEPS), step_text='올린 영상에 넣는 중')
    save(search)
    try:
        await deliver(search)
    except Exception as error:  # The videos stay in the result folder; say where.
        search.update(state='failed', error=f'{error} (찾은 영상은 이 폴더에 있어요: {search["result"].get("final_dir", "")})')
    else:
        search.update(state='done', step_text='완료!')
    save(search)


async def worker(deliver):
    """Runs queued searches one at a time, oldest first; resumes watching one left running before a restart."""
    global wake
    wake = asyncio.Event()
    while True:
        pending = [s for s in all_searches() if s['state'] in {'running', 'waiting'}]
        pending.sort(key=lambda s: (s['state'] != 'running', s['created']))
        if not pending:
            wake.clear()
            await wake.wait()
            continue
        try:
            await run(pending[0], deliver)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            search = pending[0]
            search.update(state='failed', error=f'소재 찾기 중 문제가 생겼어요: {error}')
            save(search)
