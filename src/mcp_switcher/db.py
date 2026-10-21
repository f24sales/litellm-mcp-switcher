"""Portable project storage and cross-request operation leases."""
import re
import time
import uuid
from contextlib import contextmanager

from alembic import command
from alembic.config import Config
from sqlalchemy import (JSON, Column, Float, Integer, MetaData, String, Table, Text,
                        create_engine, event, insert, select, update)
from sqlalchemy.exc import IntegrityError
from pathlib import Path

from .models import Problem


class Database:
    def __init__(self, url, prefix="mcp_switcher"):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", prefix):
            raise ValueError("Invalid database table prefix")
        self.prefix = prefix
        self.engine = create_engine(url, pool_pre_ping=True,
                                    connect_args={"check_same_thread": False, "timeout": 15}
                                    if str(url).startswith("sqlite") else {})
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_setup(connection, _):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=15000")
        self.metadata = MetaData()
        self.projects = Table(prefix + "_projects", self.metadata,
            Column("id", String(36), primary_key=True), Column("owner", String(64), nullable=False),
            Column("name", String(100), nullable=False), Column("selection", JSON, nullable=False),
            Column("version", Integer, nullable=False))
        self.operations = Table(prefix + "_operations", self.metadata,
            Column("owner", String(64), primary_key=True), Column("id", String(36)),
            Column("kind", String(16)), Column("status", String(24), nullable=False),
            Column("expires", Float, nullable=False), Column("target", JSON), Column("message", Text))

    def migrate(self):
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        config.set_main_option("version_table", self.prefix + "_alembic_version")
        config.attributes["database"] = self
        with self.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")

    def initialize_owners(self, names):
        for owner in names:
            try:
                with self.engine.begin() as conn:
                    conn.execute(insert(self.operations).values(owner=owner, status="idle", expires=0))
            except IntegrityError:
                pass

    def operation(self, owner):
        with self.engine.connect() as conn:
            row = conn.execute(select(self.operations).where(self.operations.c.owner == owner)).mappings().one()
            return dict(row)

    @contextmanager
    def lease(self, owner, kind, target):
        operation_id = str(uuid.uuid4())
        with self.engine.begin() as conn:
            changed = conn.execute(update(self.operations).where(
                self.operations.c.owner == owner,
                self.operations.c.status.not_in(["pending", "uncertain"])
            ).values(id=operation_id, kind=kind, status="pending", expires=time.time() + 180,
                     target=target, message="Awaiting verified LiteLLM state")).rowcount
        if changed != 1:
            raise Problem(409, "An operation needs reconciliation before another change")
        try:
            yield operation_id
        except BaseException:
            # Never infer that a timed-out write was not applied upstream.
            self.finish(owner, operation_id, "uncertain", "Reconcile the upstream result before retrying")
            raise

    def finish(self, owner, operation_id, status, message):
        with self.engine.begin() as conn:
            conn.execute(update(self.operations).where(self.operations.c.owner == owner,
                         self.operations.c.id == operation_id).values(status=status, message=message, expires=0))

    def list_projects(self, owner):
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(self.projects).where(
                self.projects.c.owner == owner).order_by(self.projects.c.name)).mappings()]

    def save_project(self, owner, data, project_id=None):
        with self.engine.begin() as conn:
            if project_id:
                changed = conn.execute(update(self.projects).where(self.projects.c.owner == owner,
                    self.projects.c.id == project_id, self.projects.c.version == data.version)
                    .values(name=data.name.strip(), selection=data.selection.model_dump(),
                            version=self.projects.c.version + 1)).rowcount
                if not changed:
                    raise Problem(409, "Project is missing or has changed; refresh before saving")
            else:
                project_id = str(uuid.uuid4())
                conn.execute(insert(self.projects).values(id=project_id, owner=owner,
                    name=data.name.strip(), selection=data.selection.model_dump(), version=1))
        return next(p for p in self.list_projects(owner) if p["id"] == project_id)

    def delete_project(self, owner, project_id, version):
        with self.engine.begin() as conn:
            changed = conn.execute(self.projects.delete().where(self.projects.c.owner == owner,
                self.projects.c.id == project_id, self.projects.c.version == version)).rowcount
            if not changed:
                raise Problem(409, "Project is missing or has changed")

    def close(self):
        self.engine.dispose()
