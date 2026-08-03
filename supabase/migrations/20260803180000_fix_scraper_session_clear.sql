-- Defense-in-depth for cookie clear / session save.
-- Application code must still always send a non-null saved_at; PostgreSQL
-- defaults are NOT applied when the client explicitly supplies NULL.
-- Base schema already has: saved_at timestamptz not null default now()

alter table public.scraper_sessions
    alter column saved_at set default now();

comment on column public.scraper_sessions.saved_at is
'Timestamp of the latest browser-session state update. Must remain non-null even when cookies are cleared.';
