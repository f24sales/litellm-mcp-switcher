# Source of truth: SCRIPTS/githubactions. Generated copies are overwritten.
"""Runner-only HTTP fixture for candidate lifecycle checks."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

permission = {"mcp_servers": ["fixture", "obsolete"], "mcp_tool_permissions": {}, "mcp_access_groups": [], "mcp_toolsets": []}
key_permission = json.loads(json.dumps(permission))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        data = {
            "/v1/mcp/server": [{"server_id": "fixture", "alias": "fixture", "server_name": "CI fixture"},
                                {"server_id": "obsolete", "alias": "obsolete", "server_name": "Removed reference"}],
            "/key/info": {"info": {"team_id": "ci-user", "object_permission": key_permission}},
            "/team/info": {"team_info": {"team_id": "ci-user", "object_permission": permission}, "keys": [{}]},
            "/mcp-rest/tools/list": {"tools": [{"name": "read", "description": "Read a fixture"}]},
        }.get(path)
        self.reply(data)

    def do_POST(self):
        global key_permission, permission
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path == "/key/update":
            assert data["key"] == os.environ["LITELLM_MCP_SWITCHER_KEY_BEARER"]
            key_permission = data["object_permission"]
        elif self.path == "/team/update":
            permission = data["object_permission"]
        else:
            return self.reply(None)
        self.reply({"ok": True})

    def reply(self, data):
        body = json.dumps(data if data is not None else {}).encode()
        self.send_response(200 if data is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 4000), Handler).serve_forever()
