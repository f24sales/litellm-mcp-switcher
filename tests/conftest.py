import copy
import json
import os
import uuid

import httpx
import pytest
from pydantic import SecretStr

from mcp_switcher.config import Identity, Settings
from mcp_switcher.db import Database
from mcp_switcher.litellm import LiteLLM
from mcp_switcher.service import Service


class FakeAPI:
    """A deterministic HTTP boundary, including LiteLLM's empty-list inheritance."""
    def __init__(self):
        self.registry = [{"server_id": "tasks", "alias": "tasks", "server_name": "Tasks"},
                         {"server_id": "docs", "alias": "docs", "server_name": "Documents"}]
        self.keys, self.teams = {}, {}
        for name in ("alice", "bob"):
            key = name + "-litellm-" + "x" * 32
            op = {"mcp_servers": ["tasks", "docs"], "mcp_tool_permissions": {}, "mcp_access_groups": [],
                  "mcp_toolsets": [], "vector_stores": ["keep-me"]}
            self.keys[key] = {"team_id": name, "models": ["no-default-models"], "budget_id": "unchanged",
                              "object_permission": copy.deepcopy(op)}
            self.teams[name] = {"team_id": name, "object_permission": copy.deepcopy(op)}
        self.fail_write, self.uncertain_write, self.fail_tools = False, False, False
        self.writes = []

    def response(self, request):
        path, query = request.url.path, request.url.params
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/mcp/server":
            return httpx.Response(200, json=self.registry)
        if path == "/key/info":
            return httpx.Response(200, json={"info": self.keys[query["key"]]})
        if path == "/team/info":
            name = query["team_id"]
            return httpx.Response(200, json={"team_info": self.teams[name], "keys": [{"token": "redacted"}]})
        if path in {"/key/update", "/team/update"}:
            if self.fail_write:
                return httpx.Response(500, json={"error": "do not expose server secrets"})
            self.writes.append((path, copy.deepcopy(body)))
            target = self.keys[body["key"]] if path == "/key/update" else self.teams[body["team_id"]]
            target["object_permission"] = body["object_permission"]
            if self.uncertain_write:
                raise httpx.ReadTimeout("secret-token", request=request)
            return httpx.Response(200, json={"ok": True})
        if path == "/mcp-rest/tools/list":
            if self.fail_tools:
                return httpx.Response(502, json={"secret": "must not escape"})
            return httpx.Response(200, json={"tools": [{"name": "read", "description": "Read"},
                                                       {"name": "write", "description": "Write"}]})
        return httpx.Response(404, json={})


@pytest.fixture(params=["sqlite", "postgres", "mariadb"])
def db(request, tmp_path):
    url = {"sqlite": f"sqlite:///{tmp_path}/state.db", "postgres": os.getenv("TEST_POSTGRES_URL"),
           "mariadb": os.getenv("TEST_MARIADB_URL")}[request.param]
    if not url:
        pytest.skip("Database service is not configured")
    database = Database(url, "t" + uuid.uuid4().hex[:15])
    database.migrate()
    database.initialize_owners(["alice", "bob"])
    yield database
    database.metadata.drop_all(database.engine)
    database.close()


@pytest.fixture
def users():
    return [Identity("admin", "admin-" + "s" * 32, admin=True)] + [
        Identity(name, name + "-switcher-" + "s" * 32, name + "-litellm-" + "x" * 32)
        for name in ("alice", "bob")]


@pytest.fixture
def setup(db, users):
    fake = FakeAPI()
    settings = Settings(litellm_admin_virtual_key=SecretStr(users[0].bearer))
    upstream = LiteLLM(settings, transport=httpx.MockTransport(fake.response))
    return Service(db, upstream, users, settings), fake, users
