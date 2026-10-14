"""Development reload that also works in a hidden Windows console."""
from __future__ import annotations

import asyncio
import multiprocessing

import uvicorn
from uvicorn._subprocess import get_subprocess
from uvicorn.supervisors.statreload import StatReload


class ReloadServer(uvicorn.Server):
    async def serve(self, sockets=None):
        async def watch_shutdown():
            while not self.config.shutdown_event.is_set():
                await asyncio.sleep(0.25)
            from .app import background, pipeline
            # Finish ongoing generation and collection before loading edited code.
            while any(not task.done() for task in [*pipeline.tasks.values(), *background]):
                await asyncio.sleep(0.25)
            self.should_exit = True

        monitor = asyncio.create_task(watch_shutdown())
        try:
            await super().serve(sockets=sockets)
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)


class ReloadSupervisor(StatReload):
    def restart(self):
        # A shared event avoids Windows CTRL_C_EVENT, which can hang hidden workers.
        self.config.shutdown_event.set()
        self.process.join()
        self.config.shutdown_event.clear()
        self.mtimes = {}
        self.process = get_subprocess(config=self.config, target=self.target, sockets=self.sockets)
        self.process.start()

    def shutdown(self):
        self.config.shutdown_event.set()
        self.process.join()
        for sock in self.sockets:
            sock.close()


def run(app, **settings):
    config = uvicorn.Config(app, factory=True, reload=True, **settings)
    config.shutdown_event = multiprocessing.get_context('spawn').Event()
    server = ReloadServer(config)
    sock = config.bind_socket()
    ReloadSupervisor(config, target=server.run, sockets=[sock]).run()
