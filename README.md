# LiteLLM MCP Switcher

A small workspace for choosing MCP servers and tools available through an existing **LiteLLM MCP key**. Save selections as projects, then activate a complete selection with one action.

Web UI, REST API and MCP tools share one authorization and application layer. LiteLLM executes upstream tools and retains their credentials. The Switcher manages access and projects; it does not proxy upstream tool calls or manage LLM models.

## Architecture

```text
User Switcher bearer ──┬── Web UI / REST :8080 ──┐
                       └── FastMCP :8000/mcp ───┤
                                              ├── project database
Admin virtual key ─────── administrative tools ─┤
                                              └── LiteLLM management API
                                                    │
User LiteLLM MCP bearer ─────────────────────── LiteLLM MCP
                                                    │
                                           upstream MCP servers
```

The Python application runs directly under systemd or in an Alpine 3.24.1 image. FastMCP supplies Streamable HTTP; Supergateway and Node.js are not required at runtime.

## Three credentials

| Credential | Purpose |
|---|---|
| `LITELLM_ADMIN_VIRTUAL_KEY` | Administrative Switcher login and backend LiteLLM management. Use a dedicated management key, never the master key. |
| `USER_SWITCHER_BEARER` | This user's Switcher Web UI, REST and MCP login. |
| `USER_LITELLM_BEARER` | The user's assigned LiteLLM MCP key, used directly at LiteLLM. |

A server-side configuration group binds one Switcher identity to one LiteLLM key. Requests cannot choose a different key. Every user needs a unique dedicated LiteLLM MCP team containing exactly one key; the team's explicit MCP permissions are the administrative ceiling. The key's MCP permissions are the active selection. Neither credential is an existing LLM key.

The Switcher rejects additive MCP access groups/toolsets in managed scopes rather than silently weakening the ceiling. Register each distinct upstream URL/credential pair as a separate LiteLLM MCP alias. The registry is discovered dynamically; no server or tool names are hardcoded.

## Configure

Copy `env.example` to `.env` and `config.conf_example` to `config.conf`. Both real files are git-ignored. Generate opaque Switcher tokens using a cryptographically secure generator, for example `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`, and put them directly into your private configuration.

Alternatively run `./config.sh` to use the existing New/Skip configuration flow and generate Compose/Quadlet files. `config.sh` and `optional_persistence.sh` are shared convention snapshots from `safrano9999/SCRIPTS/safrano9999/config`; use those sources for future synchronization. Configuration rendering never authorizes a local build or pull.

User repeat group:

```dotenv
#repeat-group: USER suffix02 USER_NAME USER_SWITCHER_BEARER USER_LITELLM_BEARER
USER_NAME=rafael
#secret
USER_SWITCHER_BEARER=
#secret
USER_LITELLM_BEARER=
```

New creates `_02`, `_03`, etc.; Skip stops adding groups. Duplicate names, duplicate assigned keys, duplicate login tokens and incomplete groups are rejected. There is no admin-name variable.

### LiteLLM management permissions

The dedicated administrative key needs these routes:

- `/key/info`, `/key/list`, `/key/update`
- `/team/info`, `/team/list`, `/team/update`
- `/v1/mcp/server`
- `/mcp-rest/tools/list`

Grant MCP discovery for the managed registry to this administrative identity, using the LiteLLM permission model. Do not grant LLM inference routes. The Switcher never returns management credentials, key values or upstream registry credentials to users.

The assigned user key needs MCP inference routes and `/key/info`. The latter is called with its Authorization header, never a key in the URL. The supported reference deployment is LiteLLM `v1.106.2`. Its `no-mcp-servers` marker is used for a complete opt-out, because an empty list can inherit team access. Empty tool selections disable that server instead of passing an inheriting empty tool list.

### Database

`MCP_SWITCHER_DB_BACKEND` selects `sqlite`, `postgres` or `mariadb` through SQLAlchemy. Alembic runs migrations before listeners start. Prefixes are validated and isolate tables in network databases.

With SQLite, network database fields are omitted/blanked by the existing config.sh metadata. The fixed application directory is:

```text
/opt/f24sales/litellm-mcp-switcher/sqlite
```

Its mount is generated through the existing `*_DB_BACKEND` named-volume convention. No separate JSON state file or manually configured SQLite path is required. The explicit deployment Quadlet includes an equivalent dedicated volume. Do not install both generated and explicit Quadlets with the same container name.

Projects contain owner, ID, name, selection and version. An operation table stores pending/verified/uncertain outcomes. Neither table stores bearer tokens. Back up the chosen database using your normal database procedures.

### Listeners

- `WEBUI_ENABLED=1`: Web UI and REST listen on `0.0.0.0:8080`.
- `MCP_ENABLED=1`: MCP listens on `0.0.0.0:8000/mcp`.
- Each can be disabled independently. Disabling both is an error.
- Blank `WEBUI_PUBLISH_PORT`/`MCP_PUBLISH_PORT` disables host publication, independently of internal listeners.

The default refresh interval is 30 seconds. `LITELLM_API_BASE_URL` must be reachable from the application's network namespace. Internal Podman DNS is preferred when both containers share a network.

## Use

Open the Web UI and enter your **Switcher** bearer. The browser retains it only in page memory, not in localStorage, cookies or URLs. Reloading or signing out clears it. REST calls send `Authorization: Bearer …`; no cookie authentication is used, so browser cross-site requests do not acquire ambient login credentials. Browser cookies from other services cannot authenticate this application.

Choose MCP servers, expand individual tool lists, and save a project. Saving does not activate it. Apply changes the assigned LiteLLM key for **every client using that key**. Independent simultaneous workspaces require separate configured LiteLLM keys.

With no edits, background refresh follows the active backend selection. Once edited, the draft is protected, including against requests already in flight. An external revision change is shown as a conflict. Discard reloads current state. Apply rechecks the revision and the administrative ceiling.

Discovery invokes only tool listing. A failed listing preserves the saved selection and displays an error. A successful listing is not proof that every tool invocation or multi-step browser session will work. Tools are loaded on demand with bounded concurrency and a short cache.

Administrative login shows the configured user selector and edits the selected user's ceiling. Normal identities cannot list other users, access their projects, call administrative tools or choose a target key.

## REST API

All `/api` routes require an injected bearer.

| Method and path | Purpose |
|---|---|
| `GET /api/me` | Identity and default refresh interval |
| `GET /api/state` | Own ceiling, active selection, projects and revision |
| `GET /api/servers/{id}/tools` | Discover a permitted server's tools |
| `POST /api/projects` | Create a saved project |
| `PUT /api/projects/{id}` | Update using its current version |
| `DELETE /api/projects/{id}?version=N` | Delete an owned project |
| `POST /api/apply` | Apply a selection using the state revision |
| `GET /api/admin/users` | Admin: configured users |
| `GET /api/admin/users/{name}` | Admin: inspect user and registry |
| `GET /api/admin/users/{name}/servers/{id}/tools` | Admin: tool discovery |
| `PUT /api/admin/users/{name}/access` | Admin: replace ceiling and cap active access |

Selections use stable LiteLLM server IDs:

```json
{
  "selection": {
    "servers": {
      "server-id-for-all-permitted-tools": null,
      "another-server-id": ["read_document", "search_documents"]
    }
  },
  "revision": "64-character revision returned by the state endpoint"
}
```

An empty `servers` object disables all MCP access. Project writes additionally take `name` and, for updates, `version`. Unknown request fields are rejected. The public `/openapi.json` describes schemas, not credentials. `/healthz` indicates local WebUI listener readiness; authenticated state/discovery reports upstream availability.

## MCP tools

Configure a client with the Switcher `/mcp` URL and the appropriate bearer.

User tools:
- `current_state`
- `discover_tools`
- `save_project`
- `delete_project`
- `apply_selection`

Admin tools:
- `admin_list_users`
- `admin_inspect_user`
- `admin_discover_tools`
- `admin_set_access`

Role checks filter tool listings and also enforce calls against cached tool names. Upstream MCP tools remain on LiteLLM's endpoint.

## Concurrent changes and uncertain writes

Project versions prevent lost project updates. State revisions include the ceiling and effective active selection. Database operation leases serialize Switcher writes for the same user across access methods. Unrelated key/object-permission fields are preserved.

The local database and LiteLLM are separate systems, so a single distributed transaction is not claimed. A write is successful only after read-back verification. Timeouts become uncertain operations and are reconciled against LiteLLM; no write is blindly retried. A crashed pending operation is reconciled after its bounded lease expires (12 upstream request timeouts plus 60 seconds). If the observed state differs, review it before issuing a new change.

Administrative revocation updates the team ceiling first, limiting direct LiteLLM use even if a later key cleanup fails. Stored projects remain owned records but cannot reactivate revoked access. Changes performed outside the Switcher between an upstream read and write cannot be made transactional by a client-only API; the preflight and post-write checks detect observable conflicts, while organizational use of one management path avoids this remaining race.

## Bare-metal installation

Install Python 3.11+ and the PostgreSQL client library if using PostgreSQL. Install the package normally with your distribution's supported pip policy, for example `python3 -m pip install .`. No virtual environment is required by the application.

Create a dedicated `mcp-switcher` service account, `/etc/litellm-mcp-switcher` for `.env` and `config.conf`, and a writable SQLite directory if selected. Restrict secret-file permissions. Install `deploy/litellm-mcp-switcher.service`, adjusting the installed executable path if necessary, then enable/start it using systemd.

The same entry point, `litellm-mcp-switcher`, supervises both listeners on the host and in the image. Stopping either required listener fails the whole process so systemd/Podman can restart it.

## Container deployment

```text
ghcr.io/f24sales/litellm-mcp-switcher-safrano9999:latest
```

Versioned tags use `YYYY.M.N` and are immutable; increment `project.version` after a published release. Source/revision/version labels identify the tested commit. The image runs as UID/GID 10001, with a read-only filesystem supported, a writable `/tmp`, and the selected persistent database.

`deploy/` contains a core-network Quadlet, SQLite volume and Caddy example. The host publication port remains installation-specific. Prefer Caddy-to-container routing over publishing database or backend ports.

Validate Caddy before reloading. Route `/mcp` to port 8000 and other paths to port 8080. Keep authentication enabled in the application. Preserve existing Caddy imports and register the frontend through the installation's normal Citadel scan.

Image builds, dependency tests, database tests and restart tests run only on GitHub-hosted Actions runners. Local infrastructure pulls exclusively through the installation's Smart1 `images.json` inventory and `sudo ./podman-smart1.sh --update`; no implicit pulls on starts. The deployment Quadlet uses `Pull=never`.

Workflow source of truth:
`SCRIPTS/githubactions/litellm-mcp-switcher/workflows/container-image.yml`.
The repository `.github/workflows` file is a synchronized copy.

## Verification

CI checks SQLite, PostgreSQL and MariaDB; user isolation; stale project versions; MCP tool visibility and cached-call denial; empty scopes; revocation; preservation of independent key settings; uncertain writes; process-crash reconciliation; failed discovery; and browser draft-refresh conflicts. The Alpine candidate is tested with both listeners, each listener separately, graceful shutdown and persistent state across restart before publication.

## References

- [FastMCP](https://gofastmcp.com/)
- [LiteLLM MCP access control](https://docs.litellm.ai/docs/mcp_control)
- [SQLAlchemy](https://docs.sqlalchemy.org/)
