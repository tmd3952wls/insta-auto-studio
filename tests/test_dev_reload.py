import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import uvicorn
from studio import app
from studio.dev_reload import ReloadServer


class ReloadDeferralTests(unittest.IsolatedAsyncioTestCase):
    async def check_deferral(self, background=False):
        release = asyncio.Event()
        work = asyncio.create_task(release.wait())
        config = uvicorn.Config('launch:create_app', factory=True)
        config.shutdown_event = threading.Event()
        server = ReloadServer(config)

        async def serving(instance, sockets=None):
            while not instance.should_exit:
                await asyncio.sleep(.01)

        with patch('uvicorn.Server.serve', serving), \
             patch.object(app, 'background', {work} if background else set()), \
             patch.object(app, 'pipeline', SimpleNamespace(tasks={} if background else {'job': work})):
            serving_task = asyncio.create_task(server.serve())
            try:
                config.shutdown_event.set()
                await asyncio.sleep(.35)
                self.assertFalse(server.should_exit, 'Reload interrupted unfinished work')
                release.set()
                await asyncio.wait_for(serving_task, timeout=2)
                self.assertTrue(server.should_exit)
            finally:
                release.set()
                serving_task.cancel()
                await asyncio.gather(work, serving_task, return_exceptions=True)

    async def test_reload_waits_for_video_generation_to_finish(self):
        await self.check_deferral()

    async def test_reload_waits_for_background_collection_to_finish(self):
        await self.check_deferral(background=True)


class ReloadLoopTests(unittest.TestCase):
    def test_windows_reload_worker_can_start_subprocesses(self):
        # The image collector (Playwright) and Codex start subprocesses, which a SelectorEventLoop cannot do on Windows.
        from studio import dev_reload
        seen = {}
        def capture(app, **settings):
            seen.update(settings)
            raise SystemExit
        with patch('studio.dev_reload.sys.platform', 'win32'), patch('studio.dev_reload.uvicorn.Config', side_effect=capture):
            with self.assertRaises(SystemExit):
                dev_reload.run('launch:create_app', host='127.0.0.1', port=8765)
        self.assertEqual(seen['loop'], 'asyncio:ProactorEventLoop')
