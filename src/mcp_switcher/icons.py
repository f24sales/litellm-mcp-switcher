"""Fetch small raster icons from registered MCP origins without credentials."""
import asyncio
import hashlib
from io import BytesIO
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image


def candidates(server):
    base = server.get("url")
    if not isinstance(base, str):
        return []
    origin = urlsplit(base)
    if origin.scheme not in ("http", "https") or not origin.hostname or origin.username or origin.password:
        return []
    metadata = server.get("mcp_info") or server.get("server_info") or {}
    icons = server.get("icons") or (metadata.get("icons") if isinstance(metadata, dict) else []) or []
    paths = [i.get("src") for i in icons if isinstance(i, dict)]
    if isinstance(metadata, dict):
        paths.extend([metadata.get("icon_url"), metadata.get("logo_url")])
    paths.extend([server.get("icon_url"), server.get("logo_url"), "/favicon.ico"])
    urls = []
    for path in paths:
        if isinstance(path, str) and path:
            url = urljoin(base, path)
            parts = urlsplit(url)
            if (parts.scheme, parts.netloc) == (origin.scheme, origin.netloc) and url not in urls:
                urls.append(url)
    return urls[:4]


class IconCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.tasks = {}
        self.limit = asyncio.Semaphore(3)

    def lookup(self, server):
        try:
            urls = candidates(server)
            if not urls:
                return None
            key = hashlib.sha256(repr((server["server_id"], urls)).encode()).hexdigest()
            path = self.directory / (key + ".png")
            stamp = path.with_suffix(".checked")
            if key not in self.tasks and (not stamp.exists() or time.time() - stamp.stat().st_mtime > 86400):
                task = asyncio.create_task(self.fetch(path, stamp, urls))
                self.tasks[key] = task
                task.add_done_callback(lambda _: self.tasks.pop(key, None))
            return "/icons/" + path.name if path.is_file() else None
        except (OSError, ValueError):
            return None

    async def fetch(self, path, stamp, urls):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            async with self.limit, httpx.AsyncClient(timeout=3, trust_env=False) as client:
                for url in urls:
                    try:
                        async with asyncio.timeout(5), client.stream("GET", url) as response:
                            if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                                continue
                            data = bytearray()
                            async for chunk in response.aiter_raw(8192):
                                data.extend(chunk)
                                if len(data) > 256 * 1024:
                                    raise ValueError("oversized icon")
                        with Image.open(BytesIO(data), formats=("PNG", "ICO", "JPEG", "GIF")) as image:
                            if max(image.size) > 1024:
                                continue
                            image.thumbnail((32, 32))
                            tmp = path.with_suffix(".tmp")
                            image.convert("RGBA").save(tmp, format="PNG")
                            tmp.replace(path)
                            break
                    except (httpx.HTTPError, OSError, ValueError, TimeoutError, Image.DecompressionBombError):
                        continue
            stamp.touch()
        except OSError:
            return
