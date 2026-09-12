"""fronts/mcp.py — CLI-точка входу для локального MCP-сервера Балачок.

Дозволяє запускати сервер як `python -m fronts.mcp` або через майбутній exe.
"""
from __future__ import annotations

import sys
from whisper_core import mcp_server


def main(argv=None):
    return mcp_server.main(argv)


if __name__ == "__main__":
    sys.exit(main())
