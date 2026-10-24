"""Explicit config.sh provisioning of a dedicated LiteLLM MCP key and team."""
import argparse
import os
import re
import sys
import uuid
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

LABELS = "Enter existing LiteLLM bearer|Create new virtual key in LiteLLM"


class ProvisionError(Exception):
    """Safe diagnostics: never include credentials or upstream response bodies."""


class Provisioner:
    def __init__(self, values, key, transport=None):
        match = re.fullmatch(r"USER_LITELLM_BEARER(_\d{2,})?", key)
        if not match:
            raise ProvisionError("Unsupported configuration field")
        suffix = match[1] or ""
        self.values, self.key = values, key
        self.name = (values.get("USER_NAME" + suffix) or "").strip()
        bearer = (values.get("USER_SWITCHER_BEARER" + suffix) or "").strip()
        csv = values.get("USER_ALLOWED_MCPS" + suffix) or ""
        self.aliases = list(dict.fromkeys(a.strip() for a in csv.split(",") if a.strip()))
        self.always = [a.strip() for a in (values.get("USER_ALWAYS_ON_MCPS" + suffix) or "").split(",") if a.strip()]
        base = (values.get("LITELLM_API_BASE_URL") or "").strip().rstrip("/")
        admin = values.get("LITELLM_MASTER_KEY") or values.get("LITELLM_ADMIN_VIRTUAL_KEY") or ""
        try:
            url = urlsplit(base)
            port = url.port or {"http": 80, "https": 443}.get(url.scheme)
        except ValueError:
            raise ProvisionError("Invalid LiteLLM URL or port") from None
        if (url.scheme not in {"http", "https"} or not url.hostname or not port
                or url.username or url.password or url.query or url.fragment):
            raise ProvisionError("Configure a LiteLLM HTTP(S) URL and valid port first")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.name):
            raise ProvisionError("Configure a valid user name first")
        if min(len(admin), len(bearer)) < 24 or "\n" in admin or "\r" in admin:
            raise ProvisionError("Configure the administrative LiteLLM key and Switcher bearer first")
        if not self.aliases or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", a) for a in self.aliases):
            raise ProvisionError("Configure USER_ALLOWED_MCPS with existing MCP aliases first")
        if not set(self.always) <= set(self.aliases):
            raise ProvisionError("Always-on MCPs must be included in USER_ALLOWED_MCPS")
        self.team_id = str(uuid.uuid5(uuid.NAMESPACE_URL, base + "/mcp-switcher/" + self.name))
        self.marker = {"mcp_switcher_owner": self.name, "mcp_switcher_setup": "v1"}
        self.client = httpx.Client(base_url=base, headers={"Authorization": "Bearer " + admin},
                                   timeout=10, follow_redirects=False, transport=transport)

    def request(self, method, path, *, missing=False, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ProvisionError("LiteLLM request failed; a write may have succeeded. Check LiteLLM before retrying") from None
        if missing and response.status_code == 404:
            return None
        expected = (200,) if method == "GET" else (200, 201)
        if response.status_code not in expected:
            raise ProvisionError(f"LiteLLM rejected {method} {path} (HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError:
            raise ProvisionError("LiteLLM returned invalid JSON") from None

    def check(self):
        # A successful authenticated catalog request is required; no health-page fallback.
        catalog = self.request("GET", "/v1/mcp/server")
        if not isinstance(catalog, list):
            raise ProvisionError("LiteLLM returned an invalid MCP registry")
        ids = []
        for alias in self.aliases:
            matches = {s.get("server_id") for s in catalog if isinstance(s, dict)
                       and alias in (s.get("alias"), s.get("server_name"), s.get("server_id"))}
            if len(matches) != 1 or not all(isinstance(s, str) and s for s in matches):
                raise ProvisionError(f"MCP alias is missing or ambiguous: {alias}")
            ids.extend(matches)
        return {"mcp_servers": list(dict.fromkeys(ids)), "mcp_access_groups": [],
                "mcp_toolsets": [], "mcp_tool_permissions": {}}

    def create(self):
        permission = self.check()
        team = self.request("GET", "/team/info", params={"team_id": self.team_id}, missing=True)
        if team is None:
            self.request("POST", "/team/new", json={"team_id": self.team_id,
                "team_alias": "mcp-switcher-" + self.name, "metadata": self.marker,
                "models": ["no-default-models"], "object_permission": permission})
            team = self.request("GET", "/team/info", params={"team_id": self.team_id})
        info = team.get("team_info", {}) if isinstance(team, dict) else {}
        metadata = info.get("metadata") or {} if isinstance(info, dict) else {}
        if not isinstance(metadata, dict) or any(metadata.get(k) != v for k, v in self.marker.items()):
            raise ProvisionError("The dedicated team belongs to another setup; no changes made")
        if not isinstance(team.get("keys"), list) or team["keys"]:
            raise ProvisionError("This team already has a key or cannot be inspected; enter its existing bearer")
        self.request("POST", "/team/update", json={"team_id": self.team_id, "object_permission": permission})
        result = self.request("POST", "/key/generate", json={"team_id": self.team_id,
            "key_alias": "mcp-switcher-" + self.name, "models": ["no-default-models"],
            "allowed_routes": ["mcp_inference_routes", "/key/info"], "object_permission": permission})
        token = result.get("key") if isinstance(result, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{24,512}", token):
            raise ProvisionError("LiteLLM did not return a usable bearer; inspect the created key before retrying")
        return token


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "create"))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config-file", default="config.conf")
    parser.add_argument("--key", default="USER_LITELLM_BEARER")
    args = parser.parse_args(argv)
    values = {}
    for path in (args.config_file, args.env_file):
        values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
    values.update(os.environ)
    existing = values.get(args.key) or ""
    if existing and args.mode == "create":
        if not re.fullmatch(r"[A-Za-z0-9_.-]{24,512}", existing):
            print("Existing bearer is invalid; no new key created", file=sys.stderr)
            return 1
        print(existing)
        return 0
    provisioner = None
    try:
        provisioner = Provisioner(values, args.key)
        if args.mode == "check":
            provisioner.check()
            print(LABELS)
        else:
            print(provisioner.create())
        return 0
    except ProvisionError as error:
        print(f"LiteLLM key creation unavailable: {error}", file=sys.stderr)
        return 1
    finally:
        if provisioner:
            provisioner.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
