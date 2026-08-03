"""
DEPRECATED: MoveMeOn no longer uses MongoDB.

Database schema and indexes are managed in Supabase. Apply migrations under
supabase/migrations/ and verify with:

  python monitor.py --test-supabase

This stub remains so legacy deploy steps that call init_db_movemeon.py exit
successfully without creating Mongo indexes.
"""

from __future__ import annotations


def main() -> int:
    print(
        "init_db_movemeon.py is deprecated: MoveMeOn uses Supabase, not MongoDB.\n"
        "Apply supabase/migrations/*.sql in the Supabase SQL editor, then run:\n"
        "  python monitor.py --test-supabase"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
