#!/usr/bin/env python3
"""config.sh adapter; works from a source checkout or an installed package."""
import sys
from pathlib import Path

source = Path(__file__).resolve().parent / "src"
if source.is_dir():
    sys.path.insert(0, str(source))

from mcp_switcher.provision import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
