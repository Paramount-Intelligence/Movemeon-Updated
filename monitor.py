"""Thin entry point — keeps the Dockerfile/Railway CMD stable while the
actual implementation lives in script_clean.py."""

from script_clean import cli_main

if __name__ == "__main__":
    raise SystemExit(cli_main())
