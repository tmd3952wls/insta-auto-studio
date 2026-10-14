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
