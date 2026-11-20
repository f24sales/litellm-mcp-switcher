import asyncio
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from PIL import Image

from mcp_switcher.icons import IconCache


async def test_cache_valid_icon_negative_result_and_reopen(tmp_path):
    png = BytesIO()
    Image.new("RGB", (48, 48), "blue").save(png, format="PNG")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(png.getvalue() if self.path == "/logo.png" else b"<html>login</html>")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        present = {"server_id": "one", "url": base + "/mcp", "icons": [{"src": "/logo.png"}]}
        missing = {"server_id": "two", "url": base + "/mcp"}
        cache = IconCache(tmp_path)
        for entry in (present, missing):
            assert cache.lookup(entry) is None
        await asyncio.gather(*cache.tasks.values())
        reopened = IconCache(tmp_path)
        resource = reopened.lookup(present)
        assert resource and resource.startswith("/icons/")
        with Image.open(tmp_path / resource.split("/")[-1]) as image:
            assert image.format == "PNG" and image.size == (32, 32)
        assert reopened.lookup(missing) is None and not reopened.tasks
        assert sorted(requests) == [("/favicon.ico", None), ("/logo.png", None)]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
