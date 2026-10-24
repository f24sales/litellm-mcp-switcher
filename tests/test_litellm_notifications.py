import asyncio
import contextvars
import gzip
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location("tool_changes", str(Path(__file__).resolve().parents[1] / "integrations/litellm/mcp_tool_changes.py"))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Connection:
    has_standalone_channel = True

    def __init__(self):
        self.messages = []
        self.session_id = str(id(self))

    async def notify(self, method, params):
        self.messages.append(method)


class Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = contextvars.ContextVar("auth", default=None)
        self.transports = {}
        self.changes = module.ToolChanges(SimpleNamespace(auth_context_var=self.auth,
                                         session_manager_stateful=SimpleNamespace(_server_instances=self.transports)),
                                         lambda x: x.removeprefix("raw:"), lambda: "notification", None,
                                         {"2025-11-25"})

    def bind(self, key, team="team-a"):
        connection = Connection()
        self.transports[connection.session_id] = connection
        self.auth.set(SimpleNamespace(user_api_key_auth=SimpleNamespace(api_key=key, team_id=team)))
        ctx = SimpleNamespace(session=SimpleNamespace(_connection=connection), protocol_version="2025-11-25")
        self.changes.bind(ctx)
        return connection

    async def test_same_key_multiple_clients_other_key_isolation(self):
        a, b, c = self.bind("raw:a"), self.bind("raw:a"), self.bind("raw:b")
        await self.changes.publish(key="a")
        self.assertEqual(a.messages, [module.METHOD])
        self.assertEqual(b.messages, [module.METHOD])
        self.assertEqual(c.messages, [])

    async def test_team_change_matches_only_affected_team(self):
        a, b = self.bind("raw:a"), self.bind("raw:b", "team-b")
        await self.changes.publish(team="team-a")
        self.assertEqual(a.messages, [module.METHOD])
        self.assertEqual(b.messages, [])

    async def test_closed_sessions_are_pruned(self):
        a = self.bind("raw:a")
        self.transports.pop(a.session_id)
        self.assertEqual(await self.changes.publish(key="a"), 0)
        self.assertEqual(self.changes.clients, {})

    async def test_authoritative_session_identity_wins_over_request_context(self):
        a = self.bind("raw:a")
        self.changes.gateway._stateful_session_auth_contexts = {
            a.session_id: SimpleNamespace(user_api_key_auth=SimpleNamespace(api_key="raw:a", team_id="team-a"))}
        self.auth.set(SimpleNamespace(user_api_key_auth=SimpleNamespace(api_key="raw:b", team_id="team-b")))
        ctx = SimpleNamespace(session=SimpleNamespace(_connection=a), protocol_version="2025-11-25")
        self.changes.bind(ctx)
        await self.changes.publish(key="b")
        self.assertEqual(a.messages, [])
        await self.changes.publish(key="a")
        self.assertEqual(a.messages, [module.METHOD])

    async def test_anonymous_connection_not_registered(self):
        ctx = SimpleNamespace(session=SimpleNamespace(_connection=Connection()), protocol_version="2025-11-25")
        self.assertIsNone(self.changes.bind(ctx))

    async def request(self, payload, response, status=200):
        messages = []
        async def app(scope, receive, send):
            self.assertEqual(json.loads((await receive())["body"]), payload)
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": json.dumps(response).encode()})
        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode()}
        async def send(message):
            messages.append(message)
        await module.PermissionUpdates(app, self.changes)(
            {"type": "http", "method": "POST", "path": "/key/update"}, receive, send)
        if self.changes.tasks:
            await asyncio.gather(*self.changes.tasks)
        self.assertEqual(json.loads(messages[-1]["body"]), response)

    async def test_committed_update_and_response_unchanged(self):
        a, b = self.bind("raw:a"), self.bind("raw:b")
        await self.request({"object_permission": {"mcp_servers": ["s"]}}, {"key": "raw:a"})
        self.assertEqual(a.messages, [module.METHOD])
        self.assertEqual(b.messages, [])

    async def test_failed_or_unrelated_update_emits_nothing(self):
        a = self.bind("raw:a")
        await self.request({"object_permission": {}}, {"key": "raw:a"}, 403)
        await self.request({"key_alias": "alias"}, {"key": "raw:a"})
        self.assertEqual(a.messages, [])

    async def test_gzip_response_is_observed_without_rewriting_it(self):
        a = self.bind("raw:a")
        compressed = gzip.compress(json.dumps({"key": "raw:a"}).encode())
        messages = []
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-encoding", b"gzip")]})
            await send({"type": "http.response.body", "body": compressed})
        async def receive():
            return {"type": "http.request", "body": b'{"object_permission":{}}'}
        async def send(message):
            messages.append(message)
        await module.PermissionUpdates(app, self.changes)(
            {"type": "http", "method": "POST", "path": "/key/update"}, receive, send)
        await asyncio.gather(*self.changes.tasks)
        self.assertEqual(a.messages, [module.METHOD])
        self.assertEqual(messages[-1]["body"], compressed)

    async def test_tool_call_can_carry_change_without_get(self):
        a = self.bind("raw:a")
        notifications = []
        async def send(notification, related_request_id=None):
            notifications.append((notification, related_request_id))
        ctx = SimpleNamespace(session=SimpleNamespace(_connection=a, send_notification=send),
                              protocol_version="2025-11-25", method="tools/call", request_id=77)
        async def next_call(ctx):
            await self.changes.publish(key="a")
            return {"result": "unchanged"}
        result = await self.changes(ctx, next_call)
        self.assertEqual(result, {"result": "unchanged"})
        self.assertEqual(notifications, [("notification", 77)])

    async def test_notification_fires_at_response_end_before_request_cleanup(self):
        a = self.bind("raw:a")
        cleanup = asyncio.Event()
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"key":"raw:a"}'})
            await cleanup.wait()
        async def receive():
            return {"type": "http.request", "body": b'{"object_permission":{}}'}
        async def send(message):
            pass
        task = asyncio.create_task(module.PermissionUpdates(app, self.changes)(
            {"type": "http", "method": "POST", "path": "/key/update"}, receive, send))
        try:
            for _ in range(4):
                await asyncio.sleep(0)
            self.assertEqual(a.messages, [module.METHOD])
        finally:
            cleanup.set()
            await task

    async def test_initialize_response_is_sent_only_after_sdk_commits_session(self):
        committed = False
        received = []
        async def original(transport, scope, receive, send):
            nonlocal committed
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"initialize-result"})
            committed = True
        async def send(message):
            self.assertTrue(committed)
            received.append(message)
        await module.buffer_opening(original, None, {}, None, send)
        self.assertEqual(received[-1]["body"], b"initialize-result")

    async def test_failed_initialization_does_not_send_buffered_success(self):
        received = []
        async def original(transport, scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            raise RuntimeError("Admission rolled back")
        async def send(message):
            received.append(message)
        with self.assertRaises(RuntimeError):
            await module.buffer_opening(original, None, {}, None, send)
        self.assertEqual(received, [])


if __name__ == "__main__":
    unittest.main()
