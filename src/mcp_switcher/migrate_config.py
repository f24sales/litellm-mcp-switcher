"""One-time, value-preserving rename of the former public configuration keys."""
import argparse
import os
import re
import tempfile
from pathlib import Path

P = "LITELLM_MCP_SWITCHER_"
RENAMES = {
    "LITELLM_ADMIN_VIRTUAL_KEY": P + "ADMIN_VIRTUAL_KEY",
    "LITELLM_API_BASE_URL": P + "API_BASE_URL",
    "LITELLM_REQUEST_TIMEOUT_SECONDS": P + "REQUEST_TIMEOUT_SECONDS",
    "USER_NAME": P + "KEY_NAME",
    "USER_SWITCHER_BEARER": P + "KEY_SWITCHER_BEARER",
    "USER_LITELLM_BEARER": P + "KEY_BEARER",
    "USER_ALLOWED_MCPS": P + "KEY_ALLOWED_MCPS",
    "USER_ALWAYS_ON_MCPS": P + "KEY_ALWAYS_ON_MCPS",
    **{key: P + key for key in ("WEBUI_ENABLED", "MCP_ENABLED", "WEBUI_REFRESH_SECONDS",
                               "WEBUI_PUBLISH_PORT", "MCP_PUBLISH_PORT", "CONTAINER_NAME",
                               "IMAGE", "ADDITIONAL_LINE")},
    **{"MCP_SWITCHER_DB_" + field: P + "DB_" + field
       for field in ("BACKEND", "HOST", "PORT", "NAME", "USER", "PW", "PREFIX")},
    "LITELLM_MCP_AUTO_TOOL_NOTIFICATIONS": P + "AUTO_TOOL_NOTIFICATIONS",
}


def migrate_text(text):
    seen = set()

    def replace(match):
        key = match[2]
        suffix = re.search(r"(_\d{2,})$", key)
        base = key[:suffix.start()] if suffix else key
        renamed = RENAMES.get(base, base) + (suffix[0] if suffix else "")
        if renamed in seen:
            raise ValueError("Duplicate or conflicting configuration field: " + renamed)
        seen.add(renamed)
        value = match[4]
        if key == "MCP_SWITCHER_DB_PREFIX" and value.strip().strip("\"'") == "mcp_switcher":
            value = "litellm_mcp_switcher"
        return match[1] + renamed + match[3] + value

    return re.sub(r"(?m)^(\s*(?:export[ \t]+)?)([A-Za-z_][A-Za-z0-9_]*)([ \t]*=)([^\n]*)$", replace, text)


def write_private(path, content):
    path = path.resolve()
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".switcher-config-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    # Check every file before replacing any; never print credential values.
    changes = [(path, migrate_text(path.read_text())) for path in args.files]
    for path, text in changes:
        if path.read_text() != text:
            write_private(path, text)
            print(str(path) + ": configuration names migrated")


if __name__ == "__main__":
    main()
