"""Injected configuration: managed keys and separately authenticated orchestrators."""
import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL
from .permissions import read as read_permissions

PREFIX = "LITELLM_MCP_SWITCHER_"
SQL_PREFIX = "litellm_mcp_switcher"


@dataclass(frozen=True)
class Identity:
    name: str
    bearer: str = ""
    litellm_key: str = ""
    admin: bool = False
    always_on_mcps: tuple[str, ...] = ()
    key_names: tuple[str, ...] = ()
    allowed_mcps: tuple[str, ...] = ()

    source: str = ""

    @property
    def owner(self):
        return self.source or self.name

    @property
    def orchestrator(self):
        return bool(self.key_names)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", env_ignore_empty=True,
                                     env_prefix=PREFIX)
    litellm_admin_virtual_key: SecretStr
    litellm_api_base_url: str = "http://litellm:4000"
    litellm_request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    webui_enabled: bool = True
    permissions_file: str = "permissions/permissions.json"
    icon_directory: str = "/opt/f24sales/litellm-mcp-switcher/icons"
    mcp_enabled: bool = True
    dummy_mcp: bool = True
    dummy_mcp_url: str = "http://litellm-mcp-switcher:8001/mcp"
    webui_refresh_seconds: int = Field(default=30, ge=5, le=3600)
    rotation_credentials_file: str = "/opt/f24sales/litellm-mcp-switcher/sqlite/rotation-credentials.json"
    mcp_switcher_db_backend: str = "sqlite"
    mcp_switcher_db_host: str = "host.containers.internal"
    mcp_switcher_db_port: int = 5432
    mcp_switcher_db_name: str = SQL_PREFIX
    mcp_switcher_db_user: str = SQL_PREFIX
    mcp_switcher_db_pw: SecretStr = SecretStr("")
    mcp_switcher_db_prefix: str = SQL_PREFIX

    def database_url(self) -> URL:
        backend = self.mcp_switcher_db_backend
        if backend == "sqlite":
            directory = Path("/opt/f24sales/litellm-mcp-switcher/sqlite")
            directory.mkdir(parents=True, exist_ok=True)
            return URL.create("sqlite", database=str(directory / "switcher.sqlite3"))
        if backend not in {"postgres", "mariadb"}:
            raise ValueError("Database backend must be sqlite, postgres or mariadb")
        if not all([self.mcp_switcher_db_host, self.mcp_switcher_db_name,
                    self.mcp_switcher_db_user, self.mcp_switcher_db_pw.get_secret_value()]):
            raise ValueError("Network database settings are incomplete")
        return URL.create("postgresql+psycopg" if backend == "postgres" else "mysql+pymysql",
                          username=self.mcp_switcher_db_user,
                          password=self.mcp_switcher_db_pw.get_secret_value(),
                          host=self.mcp_switcher_db_host, port=self.mcp_switcher_db_port,
                          database=self.mcp_switcher_db_name)


def csv_names(value, label):
    names = tuple(part.strip() for part in value.split(",") if part.strip())
    if len(set(names)) != len(names) or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", n) for n in names):
        raise ValueError(f"{label} must contain unique aliases or key names")
    return names


def load_configuration() -> tuple[Settings, list[Identity]]:
    values = {}
    for path in ("config.conf", ".env"):
        values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
    values.update(os.environ)
    short = {k.removeprefix(PREFIX): v for k, v in values.items() if k.startswith(PREFIX)}
    setting_names = {"ADMIN_VIRTUAL_KEY": "litellm_admin_virtual_key", "API_BASE_URL": "litellm_api_base_url",
                     "REQUEST_TIMEOUT_SECONDS": "litellm_request_timeout_seconds"}
    parsed = {setting_names.get(k, "mcp_switcher_" + k.lower() if k.startswith("DB_") else k.lower()): v
              for k, v in short.items() if v != ""}
    if parsed.get("mcp_switcher_db_backend", "sqlite") == "sqlite":
        for key in ("host", "port", "name", "user", "pw", "prefix"):
            parsed.pop("mcp_switcher_db_" + key, None)
    settings = Settings(**parsed)
    admin = settings.litellm_admin_virtual_key.get_secret_value()
    if len(admin) < 24:
        raise ValueError("An administrative virtual key is required")
    if not settings.webui_enabled and not settings.mcp_enabled:
        raise ValueError("Enable at least one service")
    identities = [Identity("admin", admin, admin=True)]
    names, tokens, keys = {"admin"}, {admin}, set()

    def add(identity):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", identity.name) or identity.name in names:
            raise ValueError("Names must be valid and unique across keys and orchestrators")
        if identity.bearer and (len(identity.bearer) < 24 or identity.bearer in tokens):
            raise ValueError("Switcher bearer tokens must be unique and at least 24 characters")
        if identity.bearer:
            tokens.add(identity.bearer)
        names.add(identity.name)
        identities.append(identity)

    def groups(kind):
        fields = ({"NAME": "BEARER_ALIAS", **{field: "KEY_" + field for field in
                   ("BEARER", "SWITCHER_BEARER", "ALLOWED_MCPS", "ALWAYS_ON_MCPS")}}
                  if kind == "KEY" else {"NAME": "ORCHESTRATOR_NAME", "BEARER": "ORCHESTRATOR_BEARER",
                                         "KEY_NAMES": "ORCHESTRATOR_BEARER_ALIASES"})
        pattern = "(?:" + "|".join(fields.values()) + r")(_\d{2,})?"
        suffixes = {m[1] or "" for key in short if (m := re.fullmatch(pattern, key))}
        for suffix in sorted(suffixes, key=lambda s: int(s[1:]) if s else 1):
            yield {field: short.get(key + suffix, "").strip() for field, key in fields.items()}

    targets = []
    permissions = read_permissions(settings.permissions_file)
    from .rotation import CredentialStore
    store = CredentialStore(settings.rotation_credentials_file)
    journal = store.read()
    catalog = journal.get("catalog", {})
    changes = journal.get("alias_changes", {})
    key_groups = list(groups("KEY"))
    original_names = {g["NAME"] for g in key_groups if g["NAME"]}
    for source, record in catalog.items():
        if source.startswith("+") and record.get("name"):
            key_groups.append({"NAME": source, "BEARER": record["seed"], "SWITCHER_BEARER": "",
                               "ALWAYS_ON_MCPS": ""})
    for group in key_groups:
        if not any(group.values()):
            continue
        source = group["NAME"]
        record = catalog.get(source, {})
        name = record.get("name", source)
        if name is None:
            continue
        key = group["BEARER"]
        if len(key) < 24 or key in keys:
            raise ValueError("Each managed key requires a complete, unique LiteLLM bearer")
        keys.add(key)
        saved = permissions.get(name)
        pending = changes.get(name, {})
        if saved is None and pending.get("source") == source and pending.get("confirmed"):
            saved = permissions.get(pending.get("new"), pending.get("allowed", []))
        if saved is None:
            raise ValueError("Missing permissions.json entry for configured alias")
        identity = Identity(name, record.get("login", group["SWITCHER_BEARER"]), key,
                            always_on_mcps=csv_names(group["ALWAYS_ON_MCPS"], "Always-on aliases"),
                            allowed_mcps=tuple(saved), source=source if source != name else "")
        if not set(identity.always_on_mcps) <= set(identity.allowed_mcps):
            raise ValueError("Always-on aliases must be included in the allowed MCP aliases")
        add(identity)
        targets.append(identity.name)
    if not targets and not catalog:
        raise ValueError("Configure at least one managed LiteLLM key")
    for group in groups("ORCHESTRATOR"):
        if not any(group.values()):
            continue
        allowed = csv_names(group["KEY_NAMES"], "Orchestrator bearer aliases")
        if not allowed or not set(allowed) <= (original_names | set(targets)) or len(group["BEARER"]) < 24:
            raise ValueError("An orchestrator requires a bearer and existing managed bearer aliases")
        allowed = tuple(name for source in allowed if (name := catalog.get(source, {}).get("name", source)))
        add(Identity(group["NAME"], group["BEARER"], key_names=allowed))
    if keys & tokens:
        raise ValueError("Switcher and LiteLLM bearer namespaces must be separate")
    identities = store.overlay(identities)
    effective_keys = [u.litellm_key for u in identities if u.litellm_key]
    if len(set(effective_keys)) != len(effective_keys) or set(effective_keys) & tokens:
        raise ValueError("Rotated keys must remain unique and separate from Switcher logins")
    return settings, identities


def authenticate(token: str, identities: list[Identity]) -> Identity | None:
    if not token or len(token) > 4096:
        return None
    return next((u for u in identities if (u.admin or u.orchestrator or u.litellm_key) and u.bearer and hmac.compare_digest(u.bearer, token)), None)
