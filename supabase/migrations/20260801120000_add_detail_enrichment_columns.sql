-- Enrichment tracking + optional budget rate columns for Catalant detail extraction.

alter table public.projects
    add column if not exists detail_last_attempt_at timestamptz,
    add column if not exists detail_attempt_count integer not null default 0,
    add column if not exists detail_failure_code text,
    add column if not exists detail_last_error text,
    add column if not exists detail_completed_at timestamptz,
    add column if not exists billing_type text,
    add column if not exists hourly_rate numeric,
    add column if not exists daily_rate numeric,
    add column if not exists rate_currency text,
    add column if not exists budget_source text,
    add column if not exists budget_confidence text
        check (
            budget_confidence is null
            or budget_confidence in ('HIGH', 'MEDIUM', 'LOW')
        );

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'projects_detail_attempt_count_nonnegative'
    ) then
        alter table public.projects
            add constraint projects_detail_attempt_count_nonnegative
            check (detail_attempt_count >= 0);
    end if;
end $$;

create index if not exists idx_projects_detail_enrichment
on public.projects (platform, detail_extraction_status, scraped_at desc);
