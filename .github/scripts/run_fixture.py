# Source of truth: SCRIPTS/githubactions. Generated copies are overwritten.
"""Runner-only upstream fixture starts before the candidate's normal startup."""
import threading
from http.server import ThreadingHTTPServer

from fake_upstream import Handler
from mcp_switcher.__main__ import main

server = ThreadingHTTPServer(("127.0.0.1", 4000), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    main()
finally:
    server.shutdown()
