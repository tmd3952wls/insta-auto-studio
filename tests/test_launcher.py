import os
import sys
import unittest
from unittest.mock import patch

import launch
from studio import app as studio_app


class LauncherTests(unittest.TestCase):
    def test_source_launcher_reloads_application_code_by_default(self):
        with patch.object(sys, 'argv', ['launch.py', '--no-browser', '--port', '8779']), \
             patch.dict(os.environ), patch('studio.dev_reload.run') as run:
            launch.main()
            args, options = run.call_args
            self.assertEqual(args, ('launch:create_app',))
            self.assertEqual(options['reload_dirs'], [str(launch.ROOT / 'studio')])
            self.assertEqual(options['port'], 8779)
            self.assertEqual(os.environ['STAY_STUDIO_PORT'], '8779')

    def test_reload_worker_keeps_the_selected_callback_port(self):
        original = studio_app.fish.base_url
        try:
            with patch.dict(os.environ, {'STAY_STUDIO_PORT': '8779'}):
                self.assertIs(launch.create_app(), studio_app.app)
                self.assertEqual(studio_app.fish.base_url, 'http://127.0.0.1:8779')
        finally:
            studio_app.fish.base_url = original

    def test_reload_can_be_disabled_for_a_long_generation(self):
        with patch.object(sys, 'argv', ['launch.py', '--no-browser', '--no-reload']), \
             patch.dict(os.environ), patch('uvicorn.run') as run:
            launch.main()
            self.assertIs(run.call_args.args[0], studio_app.app)
            self.assertFalse(run.call_args.kwargs.get('reload', False))

    def test_packaged_app_runs_without_a_reload_supervisor(self):
        with patch.object(sys, 'argv', ['launch.py', '--no-browser']), \
             patch.object(sys, 'frozen', True, create=True), \
             patch.dict(os.environ), patch('uvicorn.run') as run:
            launch.main()
            self.assertIs(run.call_args.args[0], studio_app.app)
            self.assertFalse(run.call_args.kwargs.get('reload', False))
