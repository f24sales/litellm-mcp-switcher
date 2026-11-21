"""One tool catalogue per Switcher process, shared across managed keys."""
import asyncio
import logging

from .models import Problem


class ToolCatalog:
    def __init__(self, upstream):
        self.upstream = upstream
        self.entries = {}
        self.locks = {}
        self.limit = asyncio.Semaphore(3)
        self.revision = 0
        self.job = None
        self.error = None

    @property
    def refreshing(self):
        return self.job is not None and not self.job.done()

    def refresh_all(self):
        if not self.refreshing:
            self.error = None
            self.job = asyncio.create_task(self._refresh_all())
        return self.job

    async def _refresh_all(self):
        try:
            inventory = await self.upstream.inventory()
            ids = {s["server_id"] for s in inventory}
            await asyncio.gather(*(self.get(sid, refresh=True) for sid in ids))
            for sid in self.entries.keys() - ids:
                self.entries.pop(sid, None)
        except Problem:
            self.error = "Could not refresh the LiteLLM server inventory"
            logging.warning(self.error)

    async def get(self, sid, refresh=False):
        previous = self.entries.get(sid)
        if previous and not refresh:
            return previous
        lock = self.locks.setdefault(sid, asyncio.Lock())
        async with lock:
            entry = self.entries.get(sid)
            if entry is not None and entry is not previous:
                return entry
            async with self.limit:
                try:
                    tools = await self.upstream.tools(sid)
                    entry = {"status": "reachable", "tools": tools}
                except Problem as exc:
                    entry = {"status": "error", "tools": [], "message": exc.message}
            self.revision += 1
            entry["revision"] = self.revision
            self.entries[sid] = entry
            return entry

    async def close(self):
        if self.job:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
