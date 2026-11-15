"""Native key rotation with a private, crash-recoverable credentials journal."""
import fcntl
import hashlib
import json
import os
import secrets
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .models import Problem


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class CredentialStore:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            with self.path.open() as stream:
                if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & 0o077:
                    raise ValueError("Rotation credentials must be private (mode 0600)")
                data = json.load(stream)
            if data.get("version") != 1 or not isinstance(data.get("keys"), dict) or not isinstance(
                    data.get("operations"), dict):
                raise ValueError("Invalid rotation credentials journal")
            return data
        except FileNotFoundError:
            return {"version": 1, "keys": {}, "operations": {}}

    def overlay(self, identities):
        records = self.read()["keys"]
        result = []
        for identity in identities:
            record = records.get(identity.name) if identity.litellm_key else None
            if record:
                if digest(identity.litellm_key) not in record["config_hashes"]:
                    raise ValueError("Configured key conflicts with rotation credentials; reconcile privately")
                identity = replace(identity, litellm_key=record["key"])
            result.append(identity)
        return result

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Problem(409, "Another rotation is in progress; retry the same operation later") from None
            yield
        finally:
            os.close(fd)

    def write(self, data):
        fd, temporary = tempfile.mkstemp(prefix=".rotation-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


# Compare authority and configured limits, not counters/timestamps changed by use.
GRANT_FIELDS = ("team_id", "user_id", "organization_id", "key_alias", "models", "aliases",
                "allowed_routes", "permissions", "object_permission", "metadata", "budget_id",
                "max_budget", "soft_budget", "budget_duration", "model_max_budget", "model_rpm_limit",
                "model_tpm_limit", "tpm_limit", "rpm_limit", "max_parallel_requests", "expires", "blocked")


def grants(info):
    return digest({field: info.get(field) for field in GRANT_FIELDS})


def public(operation_id, operation):
    return {"operation_id": operation_id, "target_key": operation["alias"], "status": operation["status"],
            "external_update_required": operation["status"] in {"verified", "conflict", "uncertain"},
            "message": operation["message"]}


class Rotation:
    def __init__(self, service):
        self.service = service
        self.store = CredentialStore(service.settings.rotation_credentials_file)

    def authorize(self, actor, key_name):
        if not actor.admin and not actor.orchestrator:
            raise Problem(403, "Bearer rotation requires an administrator or assigned orchestrator")
        if not key_name:
            raise Problem(422, "An explicit bearer alias is required")
        return self.service.target(actor, key_name)

    async def preview(self, target):
        inventory = await self.service.upstream.inventory()
        info, _, _, _ = await self.service.upstream.scope(target, inventory)
        return {"target_key": target.name, "revision": digest([target.name, target.litellm_key, grants(info)]),
                "operation_id": str(uuid.uuid4()), "old_key_revoked_immediately": True,
                "external_update_required": True,
                "message": "Rotate only on explicit request. Afterwards update external consumers (including "
                           "Citadel email mappings) and run their normal scan manually. No scan is triggered."}, info

    def activate(self, data, operation):
        alias, key = operation["alias"], operation["new_key"]
        previous = data["keys"].get(alias, {})
        hashes = set(previous.get("config_hashes", [])) | {digest(operation["old_key"]), digest(key)}
        data["keys"][alias] = {"key": key, "config_hashes": sorted(hashes)}

    def refresh(self, data):
        for alias, record in data["keys"].items():
            if alias in self.service.users:
                self.service.users[alias] = replace(self.service.users[alias], litellm_key=record["key"])

    async def reconcile(self, data, operation):
        """Probe both credentials; never repeat a remote write during startup recovery."""
        upstream = self.service.upstream
        new = await upstream.inspect_key(operation["new_key"])
        if new is not None:
            self.activate(data, operation)
            if grants(new) != operation["grants"]:
                operation.update(status="conflict", message="Rotated key is valid but its grants changed; "
                                 "review LiteLLM and update external consumers manually")
            elif await upstream.inspect_key(operation["old_key"]) is not None:
                operation.update(status="uncertain", message="New key is valid; old-key revocation is not "
                                 "confirmed yet. Retry this operation; do not start another rotation")
            else:
                operation.update(status="verified", message="New key persisted and verified; old key revoked. "
                                 "Update external consumers and run the Citadel scan manually")
                operation.pop("old_key")
                operation.pop("new_key")
        else:
            old = await upstream.inspect_key(operation["old_key"])
            if old is None:
                raise Problem(502, "Neither rotation credential could be verified; reconcile LiteLLM privately")
            if grants(old) != operation["grants"]:
                operation.update(status="conflict", message="Key grants changed during rotation; review "
                                 "LiteLLM before retrying")
            else:
                operation.update(status="retryable", message="Old key remains valid. Retry rotate with the "
                                 "same operation_id and revision to reuse the persisted candidate")
        self.store.write(data)
        self.refresh(data)

    async def recover(self):
        data = self.store.read()
        pending = [op for op in data["operations"].values() if op["status"] in {"pending", "uncertain", "retryable"}]
        if not pending:
            return
        with self.store.locked():
            data = self.store.read()
            for operation in data["operations"].values():
                if operation["status"] in {"pending", "uncertain", "retryable"}:
                    if operation["alias"] not in self.service.users:
                        continue
                    await self.reconcile(data, operation)

    async def execute(self, actor, key_name, action, revision=None, operation_id=None):
        target = self.authorize(actor, key_name)
        if action == "preview":
            return (await self.preview(target))[0]
        if action not in {"rotate", "status"}:
            raise Problem(422, "Use action preview, rotate or status")
        try:
            operation_id = str(uuid.UUID(operation_id or ""))
        except ValueError:
            raise Problem(422, "Supply the UUID operation_id returned by preview") from None
        if action == "status":
            operation = self.store.read()["operations"].get(operation_id)
            if not operation or operation["alias"] != target.name:
                raise Problem(404, "Rotation operation not found for this bearer alias")
            return public(operation_id, operation)
        async with self.service.key_locks[target.name]:
            with self.store.locked():
                data = self.store.read()
                self.refresh(data)
                target = self.service.target(actor, key_name)
                operation = data["operations"].get(operation_id)
                if operation:
                    if operation["alias"] != target.name or operation["revision"] != revision:
                        raise Problem(409, "Operation ID already belongs to another rotation request")
                    if operation["status"] in {"verified", "conflict"}:
                        return public(operation_id, operation)
                    await self.reconcile(data, operation)
                    if operation["status"] != "retryable":
                        return public(operation_id, operation)
                else:
                    if any(op["alias"] == target.name and op["status"] != "verified"
                           for op in data["operations"].values()):
                        raise Problem(409, "Resolve the existing rotation operation before starting another")
                    preview, info = await self.preview(target)
                    if not revision or preview["revision"] != revision:
                        raise Problem(409, "Key or grants changed; obtain a fresh rotation preview")
                    operation = {"alias": target.name, "revision": revision, "grants": grants(info),
                                 "old_key": target.litellm_key, "new_key": "sk-" + secrets.token_hex(32),
                                 "status": "pending", "message": "Rotation journal saved; result pending"}
                    data["operations"][operation_id] = operation
                # Durable intent precedes the API call, including every retry.
                operation.update(status="pending", message="Rotation journal saved; result pending")
                self.store.write(data)
                try:
                    await self.service.upstream.regenerate_key(operation["old_key"], operation["new_key"])
                except Problem:
                    operation.update(status="uncertain", message="LiteLLM write result is uncertain; retry "
                                     "this operation_id and revision")
                    self.store.write(data)
                try:
                    await self.reconcile(data, operation)
                except Problem:
                    operation.update(status="uncertain", message="Rotation verification is unavailable; retry "
                                     "this operation_id and revision")
                    self.store.write(data)
                    self.refresh(data)
                return public(operation_id, operation)
