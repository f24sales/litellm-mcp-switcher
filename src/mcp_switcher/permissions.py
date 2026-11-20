"""Persistent per-alias boundaries, separate from login credentials."""
import argparse
import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from dotenv import dotenv_values


def validate(entries):
    if not isinstance(entries, dict):
        raise ValueError("Expected an object mapping key aliases to MCP aliases")
    for name, servers in entries.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError("Invalid permission target")
        if not isinstance(servers, list) or not all(
            isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", s) for s in servers
        ) or len(set(servers)) != len(servers):
            raise ValueError(f"Invalid MCP list for {name}")
    return entries


def read(path):
    return validate(json.loads(Path(path).read_text()))


@contextmanager
def edit(path, *, create=False):
    path = Path(path)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = None if create and not path.exists() else read(path)
        entries = {} if original is None else deepcopy(original)
        yield entries
        if entries == original:
            return
        validate(entries)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
            try:
                json.dump(entries, tmp, indent=2, sort_keys=True)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp.close()
                os.replace(tmp.name, path)
            finally:
                Path(tmp.name).unlink(missing_ok=True)


def migrate(env, output, *, clean=False):
    output = Path(output)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = Path(env)
    original = env.read_bytes()
    values = dotenv_values(env, interpolate=False)
    targets = {}
    for key, alias in values.items():
        m = re.fullmatch(r"LITELLM_MCP_SWITCHER_BEARER_ALIAS(_\d{2,})?", key)
        if m and alias:
            if alias in targets:
                raise ValueError("Duplicate bearer alias")
            targets[alias] = "LITELLM_MCP_SWITCHER_KEY_ALLOWED_MCPS" + (m[1] or "")
    if not targets:
        raise ValueError("No bearer aliases found")
    imported = {}
    with edit(output, create=True) as entries:
        for alias, field in targets.items():
            if field not in values and alias in entries:
                continue
            desired = [s.strip() for s in (values.get(field) or "").split(",") if s.strip()]
            if alias in entries and set(entries[alias]) != set(desired):
                raise ValueError(f"Existing JSON disagrees with legacy grants for {alias}")
            entries[alias] = desired
            imported[alias] = desired
    saved = read(output)
    if any(alias not in saved for alias in targets) or any(saved[k] != v for k, v in imported.items()):
        raise ValueError("Migration verification failed")
    if clean:
        if original != env.read_bytes():
            raise ValueError("Environment changed during migration")
        fields = {field.encode() for field in targets.values()}
        remaining = b"".join(line for line in original.splitlines(keepends=True)
                             if line.partition(b"=")[0].strip() not in fields)
        if remaining != original:
            with tempfile.NamedTemporaryFile(dir=env.parent, delete=False) as tmp:
                try:
                    tmp.write(remaining)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    tmp.close()
                    os.replace(tmp.name, env)
                finally:
                    Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    migrate(args.env, args.output, clean=args.clean)
