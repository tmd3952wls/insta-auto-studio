"""Loopback-only desktop launcher, also the Windows frozen entry point."""
from pathlib import Path
import argparse
import os
import sys

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / '.tools/studio-python'))
if (ROOT / 'tools/ms-playwright').exists():
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / 'tools/ms-playwright')
elif (ROOT / '.tools/ms-playwright').exists():
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.tools/ms-playwright')


def create_app():
    """Configure each worker, including the worker spawned by the development reloader."""
    from studio.app import app, fish
    fish.base_url = f"http://127.0.0.1:{int(os.environ.get('STAY_STUDIO_PORT', '8765'))}"
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-reload', action='store_true', help='Disable automatic code reload during development')
    args = parser.parse_args()
    import uvicorn
    os.environ['STAY_STUDIO_PORT'] = str(args.port)
    url = f'http://127.0.0.1:{args.port}'
    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    settings = dict(host='127.0.0.1', port=args.port, log_level='warning', access_log=False)
    if not getattr(sys, 'frozen', False) and not args.no_reload:
        from studio.dev_reload import run
        run('launch:create_app', reload_dirs=[str(ROOT / 'studio')], **settings)
    else:
        uvicorn.run(create_app(), **settings)


if __name__ == '__main__':
    main()
