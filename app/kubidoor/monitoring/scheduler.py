"""Asyncio scheduler: runs monitoring cycles in a worker thread at a fixed interval."""

import asyncio
import contextlib

from kubidoor.logging.setup import get_logger
from kubidoor.monitoring.engine import MonitoringEngine

logger = get_logger("scheduler")


class MonitoringScheduler:
    def __init__(self, engine: MonitoringEngine, interval_seconds: float) -> None:
        self._engine = engine
        self._interval = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="kubidoor-monitoring")
        logger.info("monitoring scheduler started (interval=%ss)", self._interval)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # Blocking Kubernetes/DB I/O runs in a thread so the event loop stays responsive.
                await asyncio.to_thread(self._engine.run_cycle)
            except Exception:  # one bad cycle must not kill the scheduler
                logger.exception("unexpected error in monitoring cycle")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def stop(self) -> None:
        """Graceful shutdown: let a running cycle finish, then stop."""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("monitoring scheduler stopped")
