# MCP tool-list change extension

Loaded by the LiteLLM worker startup hook `mcp_tool_changes:install`.
The Quadlet drop-in `40-mcp-tool-changes.conf` bind mounts this directory
read-only at `/etc/litellm/extensions`. Upstream source files and image are unchanged.

Set `LITELLM_MCP_AUTO_TOOL_NOTIFICATIONS=true` in the LiteLLM container environment
to enable the extension, or `false` to disable automatic notifications on its next startup.
The explicit notification API remains available.
See [env.example](env.example) and [container-dropin.example](container-dropin.example).
Preserve any existing worker startup hooks and Python search paths when merging
this example into a deployment. This is an adapter setting, not an upstream built-in.

Tested runtime: LiteLLM 1.106.2, MCP SDK 2.2.0, one worker.
The adapter uses the SDK middleware interface and LiteLLM admission context.
Compatibility must be checked before upgrading those components.

After successful POST `/key/update` or `/team/update` requests changing MCP
permissions, clients of the affected key or team receive
`notifications/tools/list_changed`. A no-op re-save can also invalidate the list.
Other keys are not notified. No permissions or tool results are rewritten.
Bindings use the live HTTP MCP session registry and are pruned after sessions close.
The adapter owns session-manager lifetime in a process task and buffers only the
opening response (maximum 1 MiB), so SDK admission completes before the client
can close that response. This avoids an observed SDK 2.2.0 admission rollback.

Handshake MCP revisions advertise `tools.listChanged: true`. A client with an
open Streamable HTTP GET/SSE channel can receive changes without making another
request. A tool-triggered update can also notify on its own POST response stream.
The client must handle the notification and request `tools/list` again.
POST `/mcp/notify-tools-changed` authenticates through LiteLLM and notifies only
connections belonging to the calling key. It accepts no target key or permission
changes. The Switcher exposes this through its user-only `refresh_tools` tool.

A reconnect is required to renegotiate capabilities for pre-existing connections.
The 2026-07-28 `subscriptions/listen` flow and cross-worker message fan-out are
not implemented; the extension does not advertise change support for that revision.

