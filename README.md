# LiteLLM MCP Switcher

A lightweight MCP switcher and web UI for choosing which MCP servers and tools are available through an existing LiteLLM virtual key.

**Status: concept only.** This repository currently contains the design README. There is no application, container image, or deployment yet. All behavior below describes the planned implementation.

## Purpose

Use one existing LiteLLM virtual bearer to authenticate, choose a project, select MCP servers and individual tools, and apply that selection through the LiteLLM API.

LiteLLM remains responsible for upstream MCP connections and credentials. The switcher provides project selection, a compact management interface, and enforcement of the configured selection.

## Architecture

```text
Browser ── bearer login ── FastAPI web UI / backend
                                      │
MCP client ── bearer ── Supergateway ── switcher
                                      │
                                LiteLLM API
                                      │
                           Registered MCP aliases
                                      │
                              Upstream servers
```

- Build on an existing Supergateway container image, with FastAPI serving the web UI and its API.
- Target a small Alpine-based container; verify the selected Supergateway image's base and compatibility before implementation.
- Communicate with LiteLLM through its API, without accessing its database.
- Discover MCP servers and tools from LiteLLM rather than maintaining hardcoded tool catalogs.
- Preserve the originating server alias and server ID for every tool, including tools with identical names.

The exact Supergateway tag, process layout, and authenticated request-context propagation still need implementation validation.

## Bearers and upstream credentials

Only bearers present in the injected configuration are accepted. The incoming bearer is the existing LiteLLM virtual key, not a newly issued switcher credential. An unknown bearer is rejected before forwarding.

Each distinct **upstream URL + credential pair** is registered as a separate MCP alias in LiteLLM. For example, two aliases can point to the same task service while using different upstream users or permissions. The switcher selects aliases; it does not mix their credentials.

Each configured bearer has an allowed set of projects and MCP aliases. A selection can narrow this set, never expand it. Keep the allowed inventory separate from the currently enabled selection so that disabled servers can be enabled again.

Any LiteLLM management credential required to change key permissions stays on the backend. The exact permissions required for these updates must be verified against the supported LiteLLM version.

## Projects

Projects define a named selection of MCP aliases and, optionally, individual tools. A bearer can access one or more configured projects.

Applying a project to a virtual key changes that key's permissions for all clients using it. Independent simultaneous selections therefore require separate virtual keys, or a separately designed project-scoped routing mode. These modes must not be presented as interchangeable.

## Web UI

A small, responsive interface served by FastAPI:

1. Authenticate using an injected LiteLLM virtual bearer.
2. Select an available project from a dropdown.
3. View MCP servers with checkboxes, availability information, and their aliases.
4. Expand a server to enable or disable individual tools.
5. Use select-all or clear-all within the permitted inventory.
6. Submit the complete selection using one **Apply** action.

The UI should show currently functioning servers based on authenticated discovery results. A successful HTTP response alone is insufficient: upstream errors and partial discovery failures must be shown accurately. Discovery must not invoke tools that change user data.

Unavailable servers should retain their saved selection and show their status; a transient outage must not silently rewrite permissions.

### Live refresh

The default refresh interval is **30 seconds**, configurable in the UI.

- While there are no unsaved changes, refresh the displayed checkbox state from the backend.
- As soon as the user edits a selection, preserve that draft.
- Background responses, including requests started before the edit, must not overwrite unsaved changes.
- If the saved state changes elsewhere, indicate the conflict before applying the draft.
- After a successful apply or explicit discard, resume normal synchronization.
- Show the last successful refresh and any refresh error.

### Applying a selection

Submit the complete desired selection together, validate it on the backend, and apply it through the LiteLLM API. Verify the resulting state before showing success. Preserve unrelated key settings.

One UI submission does not by itself guarantee an atomic upstream update. The implementation must verify the relevant LiteLLM API behavior, handle concurrent changes, and avoid reporting a partially applied update as successful.

**Clear-all must mean no MCP access.** Empty-list inheritance or default permissions in LiteLLM must not accidentally grant broader access.

## MCP interface

The switcher should also expose project and tool selection to MCP clients. The precise tool schema remains to be designed.

A compact, stable interface for discovering, selecting, and invoking tools is the preferred starting point. Client-specific support for changing tool catalogs must be evaluated before relying on automatic tool-list refresh. Every invocation must enforce current permissions, even when a client has cached an older catalog.

## Configuration and packaging

Follow the existing configuration conventions:

- `env.example` for secret and environment inputs, including injected bearer entries.
- `config.conf_example` for non-secret settings.
- `container.example` and the shared `config.sh` conventions for container configuration, repeat groups, and additional container lines.
- No real credentials or private deployment details in examples.
- Container builds and smoke tests on GitHub-hosted Actions runners, publishing to GHCR.
- No build or deployment workflow is enabled at this concept stage.

## References

- [LiteLLM MCP REST API](https://docs.litellm.ai/docs/mcp_rest_api)
- [LiteLLM MCP access control](https://docs.litellm.ai/docs/mcp_control)
- [LiteLLM MCP toolsets](https://docs.litellm.ai/docs/mcp_toolsets)
- [Supergateway](https://github.com/supercorp-ai/supergateway)
