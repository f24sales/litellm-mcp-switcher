"""Validated configuration, including config.sh repeat groups."""
import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


@dataclass(frozen=True)
class Identity:
    name: str
    bearer: str
    litellm_key: str = ""
    admin: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", env_ignore_empty=True)
    litellm_admin_virtual_key: SecretStr
    litellm_api_base_url: str = "http://litellm:4000"
    litellm_request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    webui_enabled: bool = True
    mcp_enabled: bool = True
    webui_refresh_seconds: int = Field(default=30, ge=5, le=3600)
    mcp_switcher_db_backend: str = "sqlite"
    mcp_switcher_db_host: str = "host.containers.internal"
    mcp_switcher_db_port: int = 5432
    mcp_switcher_db_name: str = "litellm_mcp_switcher"
    mcp_switcher_db_user: str = "litellm_mcp_switcher"
    mcp_switcher_db_pw: SecretStr = SecretStr("")
    mcp_switcher_db_prefix: str = "mcp_switcher"

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


def load_configuration() -> tuple[Settings, list[Identity]]:
    # Working-directory files work identically for systemd and the container.
    values = {}
    for path in ("config.conf", ".env"):
        values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    values.update(os.environ)
    parsed = {k.lower(): v for k, v in values.items()}
    if parsed.get("mcp_switcher_db_backend", "sqlite") == "sqlite":
        for key in ("host", "port", "name", "user", "pw", "prefix"):
            parsed.pop("mcp_switcher_db_" + key, None)
    settings = Settings(**parsed)
    admin = settings.litellm_admin_virtual_key.get_secret_value()
    if len(admin) < 24:
        raise ValueError("An administrative virtual key is required")
    if not settings.webui_enabled and not settings.mcp_enabled:
        raise ValueError("Enable at least one service")
    users = [Identity("admin", admin, admin=True)]
    suffixes = sorted({m.group(1) or "" for k in values
                       if (m := re.fullmatch(r"USER_(?:NAME|SWITCHER_BEARER|LITELLM_BEARER)(_\d{2,})?", k))})
    keys = set()
    names = {"admin"}
    for suffix in suffixes:
        name, bearer, key = [values.get(k + suffix, "").strip() for k in
                             ("USER_NAME", "USER_SWITCHER_BEARER", "USER_LITELLM_BEARER")]
        if not any((name, bearer, key)):
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) or min(len(bearer), len(key)) < 24:
            raise ValueError("Each user requires a valid name and two complete bearer tokens")
        if name in names or key in keys or key == admin or key == bearer:
            raise ValueError("User names and assigned LiteLLM keys must be unique and separate")
        if any(hmac.compare_digest(bearer, u.bearer) for u in users):
            raise ValueError("Switcher bearer tokens must be unique")
        names.add(name)
        keys.add(key)
        users.append(Identity(name, bearer, key))
    if any(u.bearer in keys for u in users if not u.admin):
        raise ValueError("Switcher and LiteLLM bearer namespaces must be separate")
    if len(users) == 1:
        raise ValueError("Configure at least one user")
    return settings, users


def authenticate(token: str, identities: list[Identity]) -> Identity | None:
    if not token or len(token) > 4096:
        return None
    return next((u for u in identities if hmac.compare_digest(u.bearer, token)), None)
