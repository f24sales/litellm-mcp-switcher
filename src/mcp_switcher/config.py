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
    mcp_enabled: bool = True
    webui_refresh_seconds: int = Field(default=30, ge=5, le=3600)
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
        suffixes = {m[1] or "" for k in short
                    if (m := re.fullmatch(kind + r"_(?:NAME|BEARER|SWITCHER_BEARER|ALLOWED_MCPS|ALWAYS_ON_MCPS|KEY_NAMES)(_\d{2,})?", k))}
        for suffix in sorted(suffixes, key=lambda s: int(s[1:]) if s else 1):
            yield {field: short.get(kind + "_" + field + suffix, "").strip()
                   for field in ("NAME", "BEARER", "SWITCHER_BEARER", "ALLOWED_MCPS", "ALWAYS_ON_MCPS", "KEY_NAMES")}

    targets = []
    for group in groups("KEY"):
        if not any(group.values()):
            continue
        key = group["BEARER"]
        if len(key) < 24 or key in keys:
            raise ValueError("Each managed key requires a complete, unique LiteLLM bearer")
        keys.add(key)
        identity = Identity(group["NAME"], group["SWITCHER_BEARER"], key,
                            always_on_mcps=csv_names(group["ALWAYS_ON_MCPS"], "Always-on aliases"))
        add(identity)
        targets.append(identity.name)
    if not targets:
        raise ValueError("Configure at least one managed LiteLLM key")
    for group in groups("ORCHESTRATOR"):
        if not any(group.values()):
            continue
        allowed = csv_names(group["KEY_NAMES"], "Orchestrator key names")
        if not allowed or not set(allowed) <= set(targets) or len(group["BEARER"]) < 24:
            raise ValueError("An orchestrator requires a bearer and existing managed key names")
        add(Identity(group["NAME"], group["BEARER"], key_names=allowed))
    if keys & tokens:
        raise ValueError("Switcher and LiteLLM bearer namespaces must be separate")
    return settings, identities


def authenticate(token: str, identities: list[Identity]) -> Identity | None:
    if not token or len(token) > 4096:
        return None
    return next((u for u in identities if u.bearer and hmac.compare_digest(u.bearer, token)), None)
