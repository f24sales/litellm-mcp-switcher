"""One supervised process, with independently enabled fixed-port listeners."""
import asyncio
import logging
import signal
from contextlib import contextmanager

import uvicorn

from .config import load_configuration
from .db import Database
from .litellm import LiteLLM
from .mcp import create_mcp
from .service import Service
from .startup import prune_configured_access
from .models import Problem
from .web import create_web


class Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield  # The supervisor owns signal handling for both listeners.


async def serve():
    settings, users = load_configuration()
    prefix = settings.mcp_switcher_db_prefix if settings.mcp_switcher_db_backend != "sqlite" else "litellm_mcp_switcher"
    db = Database(settings.database_url(), prefix)
    db.migrate()
    db.initialize_owners([u.name for u in users if u.litellm_key and not u.admin])
    upstream = LiteLLM(settings)
    service = Service(db, upstream, users, settings)
    try:
        await prune_configured_access(service)
    except BaseException:
        await upstream.close()
        db.close()
        raise
    apps = []
    if settings.webui_enabled:
        apps.append((create_web(service, users), 8080))
    if settings.mcp_enabled:
        apps.append((create_mcp(service, users).http_app(path="/mcp", stateless_http=True), 8000))
    servers = [Server(uvicorn.Config(app, host="0.0.0.0", port=port, access_log=False,
                                    log_level="warning", timeout_graceful_shutdown=10)) for app, port in apps]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([*tasks, stopping], return_when=asyncio.FIRST_COMPLETED)
        if stopping not in done:
            for task in done:
                task.result()
            raise RuntimeError("A required listener stopped unexpectedly")
    finally:
        for server in servers:
            server.should_exit = True
        stopping.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
        except TimeoutError:
            for task in tasks:
                task.cancel()
        await upstream.close()
        db.close()


def main():
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(serve())
    except (ValueError, RuntimeError, Problem):
        # Configuration may contain secrets. Do not interpolate raw validation exceptions.
        logging.error("Switcher startup failed; verify configuration and enabled listeners")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
