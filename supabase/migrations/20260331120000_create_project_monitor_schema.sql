create extension if not exists pgcrypto;

-- =========================================================
-- UPDATED_AT TRIGGER
-- =========================================================

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- =========================================================
-- SCRAPER RUNS
-- =========================================================

create table if not exists public.scraper_runs (
    id uuid primary key default gen_random_uuid(),

    platform text not null,
    scraper_name text not null,
    scraper_version text,

    started_at timestamptz not null default now(),
    completed_at timestamptz,

    status text not null default 'RUNNING'
        check (
            status in (
                'RUNNING',
                'COMPLETED',
                'PARTIAL',
                'FAILED',
                'AUTH_FAILED',
                'CANCELLED'
            )
        ),

    cards_found integer not null default 0,
    cards_parsed integer not null default 0,
    cards_failed integer not null default 0,

    details_attempted integer not null default 0,
    details_completed integer not null default 0,
    details_failed integer not null default 0,

    projects_inserted integer not null default 0,
    projects_skipped integer not null default 0,

    emails_sent integer not null default 0,
    emails_failed integer not null default 0,
    emails_suppressed integer not null default 0,

    failure_code text,
    failure_reason text,

    metadata jsonb not null default '{}'::jsonb,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_scraper_runs_platform_started
on public.scraper_runs (platform, started_at desc);

create index if not exists idx_scraper_runs_status
on public.scraper_runs (status, started_at desc);

drop trigger if exists set_scraper_runs_updated_at on public.scraper_runs;
create trigger set_scraper_runs_updated_at
before update on public.scraper_runs
for each row
execute function public.set_updated_at();

-- =========================================================
-- PROJECTS: MAIN SHARED TABLE
-- =========================================================

create table if not exists public.projects (
    -- Internal database identity
    id uuid primary key default gen_random_uuid(),

    -- Platform-neutral source identity
    platform text not null,
    project_id text not null,
    source_url text not null,

    -- Common project content
    title text not null,
    short_description text,
    description text,

    status text,

    platform_category text,
    platform_category_path text[] not null default '{}',
    platform_category_raw text,
    platform_category_source text,
    platform_category_confidence text
        check (
            platform_category_confidence is null
            or platform_category_confidence in ('HIGH', 'MEDIUM', 'LOW')
        ),
    platform_category_extraction_status text
        check (
            platform_category_extraction_status is null
            or platform_category_extraction_status in (
                'FOUND_STRUCTURED',
                'FOUND_DEDICATED_SELECTOR',
                'FOUND_BREADCRUMB',
                'FOUND_EMBEDDED_DATA',
                'FOUND_TEXT_FALLBACK',
                'MISSING',
                'REJECTED_INVALID_CANDIDATE'
            )
        ),

    location text,
    location_preference text,

    budget_text text,
    budget_min numeric,
    budget_max numeric,
    budget_currency text,

    duration_text text,
    project_length text,
    start_date_text text,
    source_start_date date,

    level_of_support text,
    industry text,
    contracting_process text,

    skills text[] not null default '{}',
    expertise text[] not null default '{}',
    deliverables text[] not null default '{}',

    engagement_type text,
    project_type text,
    workstream text,
    estimated_hours numeric,
    weekly_commitment text,
    remote_or_onsite text,
    country_or_region text,
    application_deadline timestamptz,

    -- Website-posted time
    time_posted_text text,
    source_posted_at timestamptz,
    source_posted_at_is_estimated boolean not null default false,

    -- Scraper occurrence timing
    scraped_at timestamptz not null default now(),
    first_detected_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),

    -- Extraction state
    card_extraction_status text not null default 'COMPLETE'
        check (
            card_extraction_status in (
                'COMPLETE',
                'PARTIAL',
                'FAILED'
            )
        ),

    detail_extraction_status text not null default 'NOT_ATTEMPTED'
        check (
            detail_extraction_status in (
                'NOT_ATTEMPTED',
                'COMPLETE',
                'PARTIAL',
                'FAILED',
                'TIMEOUT'
            )
        ),

    missing_fields text[] not null default '{}',
    extraction_warnings text[] not null default '{}',
    extraction_metadata jsonb not null default '{}'::jsonb,
    raw_data jsonb not null default '{}'::jsonb,

    -- Email lifecycle
    email_eligible boolean not null default true,

    email_status text not null default 'PENDING'
        check (
            email_status in (
                'PENDING',
                'SENDING',
                'SENT',
                'RETRY_PENDING',
                'FAILED',
                'SUPPRESSED',
                'NOT_REQUIRED'
            )
        ),

    email_sent boolean not null default false,
    email_not_sent_reason text,
    email_failure_code text,
    email_last_error text,

    email_attempt_count integer not null default 0,
    email_last_attempt_at timestamptz,
    email_next_retry_at timestamptz,
    email_sent_at timestamptz,
    email_message_id text,

    -- Run relationship
    scraper_run_id uuid references public.scraper_runs(id)
        on delete set null,

    -- General metadata
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    -- Basic validation
    constraint projects_platform_not_blank
        check (length(trim(platform)) > 0),

    constraint projects_project_id_not_blank
        check (length(trim(project_id)) > 0),

    constraint projects_title_not_blank
        check (length(trim(title)) > 0),

    constraint projects_source_url_not_blank
        check (length(trim(source_url)) > 0),

    constraint projects_email_attempt_count_nonnegative
        check (email_attempt_count >= 0)
);

-- Do not make platform + project_id unique.
-- The same source project may be inserted again after more than 3 days.

create index if not exists idx_projects_latest
on public.projects (scraped_at desc);

create index if not exists idx_projects_platform_latest
on public.projects (platform, scraped_at desc);

create index if not exists idx_projects_platform_project_latest
on public.projects (platform, project_id, scraped_at desc);

create index if not exists idx_projects_email_retry
on public.projects (email_status, email_next_retry_at)
where email_status = 'RETRY_PENDING';

create index if not exists idx_projects_email_status_latest
on public.projects (email_status, scraped_at desc);

create index if not exists idx_projects_category_latest
on public.projects (platform_category, scraped_at desc);

create index if not exists idx_projects_source_posted_at
on public.projects (source_posted_at desc);

create index if not exists idx_projects_scraper_run
on public.projects (scraper_run_id);

create index if not exists idx_projects_title_search
on public.projects using gin (
    to_tsvector(
        'simple',
        coalesce(title, '') || ' ' || coalesce(description, '')
    )
);

drop trigger if exists set_projects_updated_at on public.projects;
create trigger set_projects_updated_at
before update on public.projects
for each row
execute function public.set_updated_at();

-- =========================================================
-- EMAIL ATTEMPTS
-- =========================================================

create table if not exists public.email_attempts (
    id uuid primary key default gen_random_uuid(),

    project_id uuid not null references public.projects(id)
        on delete cascade,

    attempt_number integer not null,

    status text not null
        check (
            status in (
                'SENDING',
                'SENT',
                'FAILED'
            )
        ),

    attempted_at timestamptz not null default now(),
    completed_at timestamptz,

    recipients text[] not null default '{}',
    provider text,
    message_id text,

    failure_code text,
    failure_reason text,

    metadata jsonb not null default '{}'::jsonb,

    created_at timestamptz not null default now(),

    constraint email_attempt_number_positive
        check (attempt_number > 0)
);

create index if not exists idx_email_attempts_project
on public.email_attempts (project_id, attempt_number desc);

create index if not exists idx_email_attempts_status
on public.email_attempts (status, attempted_at desc);

-- =========================================================
-- SCRAPER SESSIONS
-- =========================================================

create table if not exists public.scraper_sessions (
    platform text primary key,

    session_data jsonb not null,
    saved_at timestamptz not null default now(),
    expires_at timestamptz,

    session_version integer not null default 1,
    metadata jsonb not null default '{}'::jsonb,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

drop trigger if exists set_scraper_sessions_updated_at on public.scraper_sessions;
create trigger set_scraper_sessions_updated_at
before update on public.scraper_sessions
for each row
execute function public.set_updated_at();

-- =========================================================
-- ROW LEVEL SECURITY
-- =========================================================

alter table public.projects enable row level security;
alter table public.scraper_runs enable row level security;
alter table public.email_attempts enable row level security;
alter table public.scraper_sessions enable row level security;

-- Authenticated dashboard users may read project data.
drop policy if exists "authenticated users can read projects" on public.projects;
create policy "authenticated users can read projects"
on public.projects
for select
to authenticated
using (true);

-- Authenticated dashboard users may read run health.
drop policy if exists "authenticated users can read scraper runs" on public.scraper_runs;
create policy "authenticated users can read scraper runs"
on public.scraper_runs
for select
to authenticated
using (true);

-- Authenticated dashboard users may read email attempt history.
drop policy if exists "authenticated users can read email attempts" on public.email_attempts;
create policy "authenticated users can read email attempts"
on public.email_attempts
for select
to authenticated
using (true);

-- Do not create an authenticated or anonymous policy for scraper_sessions.
-- Only the trusted backend secret-key client should access session data.

revoke all on table public.scraper_sessions from anon, authenticated;
revoke insert, update, delete on table public.projects from anon, authenticated;
revoke insert, update, delete on table public.scraper_runs from anon, authenticated;
revoke insert, update, delete on table public.email_attempts from anon, authenticated;
