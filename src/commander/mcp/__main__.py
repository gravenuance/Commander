"""`python -m commander.mcp`, and the script the `cmdr-mcp` exe is built from."""

from commander.mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
