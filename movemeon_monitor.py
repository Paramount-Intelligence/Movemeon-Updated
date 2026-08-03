"""Legacy entrypoint — redirects to the Supabase-backed monitor.

Prefer: python monitor.py
"""

from script_clean import cli_main

if __name__ == "__main__":
    raise SystemExit(cli_main())
