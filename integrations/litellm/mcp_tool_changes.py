"""Key-scoped MCP notifications, loaded through LITELLM_WORKER_STARTUP_HOOKS.

Adapter for LiteLLM 1.106.2 / MCP SDK 2. No upstream files are replaced.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import zlib

LOG = logging.getLogger("f24sales.mcp_tool_changes")
METHOD = "notifications/tools/list_changed"
BODY_LIMIT = 1024 * 1024


async def buffer_opening(original, transport, scope, receive, send):
    """Commit SDK session admission before a client can close the initialize POST."""
    messages = []
    size = 0

    async def capture(message):
        nonlocal size
        size += len(message.get("body", b""))
        if size > BODY_LIMIT:
            raise RuntimeError("MCP initialization response exceeds the adapter limit")
        messages.append(dict(message))

    await original(transport, scope, receive, capture)
    for message in messages:
        await send(message)


@dataclass
class Binding:
    key: str
    team: str | None
    generation: int = 0


class ToolChanges:
    def __init__(self, gateway, normalize_key, notification_type, initialize_type, versions, push=None):
        self.gateway = gateway
        self.normalize_key = normalize_key
        self.notification_type = notification_type
        self.initialize_type = initialize_type
        self.versions = versions
        self.clients = {}
        self.tasks = set()
        self.transports = gateway.session_manager_stateful._server_instances
        self.push = push

    def prune(self):
        for session_id in list(self.clients):
            if session_id not in self.transports:
                del self.clients[session_id]

    def bind(self, ctx):
        connection = ctx.session._connection
        self.prune()
        session_id = connection.session_id
        if session_id not in self.transports:
            return None
        auth = getattr(self.gateway, "_stateful_session_auth_contexts", {}).get(session_id)
        if auth is None:
            auth = self.gateway.auth_context_var.get()
        caller = getattr(auth, "user_api_key_auth", None)
        key = getattr(caller, "api_key", None)
        if ctx.protocol_version not in self.versions or not isinstance(key, str) or not key:
            return None
        if not connection.has_standalone_channel:
            return None
        normalized = self.normalize_key(key)
        team = getattr(caller, "team_id", None)
        binding = self.clients.get(session_id)
        if binding is None:
            binding = Binding(normalized, team)
            self.clients[session_id] = binding
        elif binding.key != normalized:
            raise RuntimeError("MCP connection identity changed")
        else:
            binding.team = team
        return binding

    async def __call__(self, ctx, call_next):
        binding = self.bind(ctx)
        generation = binding.generation if binding else 0
        result = await call_next(ctx)
        if ctx.method == "initialize":
            initialized = self.initialize_type.model_validate(result)
            if initialized.protocol_version in self.versions and binding is not None:
                capabilities = initialized.capabilities.model_copy(deep=True)
                if capabilities.tools is not None:
                    capabilities.tools.list_changed = True
                result = initialized.model_copy(update={"capabilities": capabilities})
        elif ctx.method == "tools/call" and binding and binding.generation != generation:
            # A tool caller may receive notifications on its POST without holding a GET open.
            await ctx.session.send_notification(self.notification_type(), related_request_id=ctx.request_id)
        return result

    async def publish(self, *, key=None, team=None):
        if key is None and team is None:
            return 0
        self.prune()
        targets = []
        for session_id, binding in list(self.clients.items()):
            if (key is not None and binding.key == key) or (team is not None and binding.team == team):
                binding.generation += 1
                targets.append(self.transports[session_id])

        async def notify(connection):
            try:
                async with asyncio.timeout(2):
                    if self.push is None:
                        await connection.notify(METHOD, None)
                    else:
                        await self.push(connection)
                return True
            except Exception:
                # Never expose credentials or turn a committed key update into a failure.
                LOG.warning("MCP tool-list notification could not be delivered")
                return False

        delivered = sum(await asyncio.gather(*(notify(connection) for connection in targets)))
        LOG.warning("MCP tool-list change: notified %d matching connections (%d registered)", delivered, len(self.clients))
        return delivered

    def schedule(self, **target):
        task = asyncio.create_task(self.publish(**target))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


class PermissionUpdates:
    """Observe completed management requests without changing their bodies or responses."""

    def __init__(self, app, changes):
        self.app = app
        self.changes = changes

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").rstrip("/")
        if scope.get("type") != "http" or scope.get("method") != "POST" or path not in {"/key/update", "/team/update"}:
            return await self.app(scope, receive, send)
        request_body = bytearray()
        response_body = bytearray()
        too_large = False
        status = 0
        encoding = b""

        async def receive_observed():
            nonlocal too_large
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                if len(request_body) + len(chunk) <= BODY_LIMIT:
                    request_body.extend(chunk)
                else:
                    too_large = True
            return message

        async def send_observed(message):
            nonlocal status, too_large, encoding
            if message["type"] == "http.response.start":
                status = message["status"]
                encoding = dict(message.get("headers", [])).get(b"content-encoding", b"")
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if len(response_body) + len(chunk) <= BODY_LIMIT:
                    response_body.extend(chunk)
                else:
                    too_large = True
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed()

        def completed():
            if too_large or not 200 <= status < 300:
                return
            try:
                requested = json.loads(request_body)
                decoded = response_body
                if encoding == b"gzip":
                    decoded = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(response_body, BODY_LIMIT + 1)
                    if len(decoded) > BODY_LIMIT:
                        return
                response = json.loads(decoded)
                if not isinstance(requested, dict) or not isinstance(response, dict):
                    return
                if not {"object_permission", "team_id", "access_group_ids"}.intersection(requested):
                    return
                if path == "/key/update":
                    key = response.get("key")
                    if isinstance(key, str) and key:
                        self.changes.schedule(key=self.changes.normalize_key(key))
                elif isinstance(response.get("team_id"), str) and "object_permission" in requested:
                    self.changes.schedule(team=response["team_id"])
            except (ValueError, TypeError, zlib.error):
                LOG.warning("MCP notification skipped: unrecognized management response")

        await self.app(scope, receive_observed, send_observed)


async def install():
    from litellm.proxy import proxy_server
    from litellm.proxy._experimental.mcp_server import server as gateway
    from litellm.proxy.utils import hash_token_if_needed
    from mcp.types import InitializeResult, ToolListChangedNotification
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

    if getattr(proxy_server.app.state, "f24_mcp_tool_changes", None) is not None:
        return
    enabled = os.environ.get("LITELLM_MCP_AUTO_TOOL_NOTIFICATIONS", "false").strip().lower()
    if enabled not in {"false", "true"}:
        raise RuntimeError("LITELLM_MCP_AUTO_TOOL_NOTIFICATIONS must be true or false")
    if int(os.environ.get("NUM_WORKERS", "1")) != 1:
        raise RuntimeError("MCP tool notifications require one worker; shared fan-out is not configured")
    if not hasattr(gateway.server, "middleware") or not callable(hash_token_if_needed):
        raise RuntimeError("Unsupported LiteLLM MCP extension interface")
    from fastapi import Depends, HTTPException
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCNotification

    async def push(transport):
        await transport._write_stream.send(SessionMessage(
            JSONRPCNotification(jsonrpc="2.0", method=METHOD)))

    changes = ToolChanges(gateway, hash_token_if_needed, ToolListChangedNotification,
                          InitializeResult, HANDSHAKE_PROTOCOL_VERSIONS, push)
    original_opening = gateway.session_manager_stateful._serve_opening_request

    async def admitted_opening(transport, scope, receive, send):
        await buffer_opening(original_opening, transport, scope, receive, send)

    gateway.session_manager_stateful._serve_opening_request = admitted_opening
    gateway.server.middleware.append(changes)
    if enabled == "true":
        proxy_server.app.middleware_stack = PermissionUpdates(
            proxy_server.app.middleware_stack or proxy_server.app.build_middleware_stack(), changes)

    async def notify_tools_changed(caller=Depends(user_api_key_auth)):
        if not isinstance(caller.api_key, str) or not caller.api_key:
            raise HTTPException(403, "An authenticated LiteLLM key is required")
        notified = await changes.publish(key=hash_token_if_needed(caller.api_key))
        return {"notification": METHOD, "notified_connections": notified,
                "permissions_changed": False, "client_refresh_required": True}

    proxy_server.app.add_api_route("/mcp/notify-tools-changed", notify_tools_changed,
                                  methods=["POST"], tags=["MCP notifications"])
    proxy_server.app.router.routes.insert(0, proxy_server.app.router.routes.pop())
    proxy_server.app.state.f24_mcp_tool_changes = changes
    # Lazy initialization in an HTTP request binds AnyIO's manager scopes to
    # that request's cancellation. A process task must own the long-lived scopes.
    ready = asyncio.Event()

    async def session_lifespan():
        async with gateway.lifespan(proxy_server.app):
            ready.set()
            await asyncio.Event().wait()

    supervisor = asyncio.create_task(session_lifespan(), name="f24-mcp-session-lifespan")
    proxy_server.app.state.f24_mcp_session_supervisor = supervisor
    try:
        await asyncio.wait_for(ready.wait(), 10)
    except TimeoutError:
        if supervisor.done():
            supervisor.result()
        supervisor.cancel()
        raise RuntimeError("MCP session lifespan did not start") from None
    LOG.warning("F24 MCP tool-change extension enabled (automatic=%s, single worker)", enabled)
