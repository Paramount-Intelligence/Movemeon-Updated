-- Allow NOT_EXPOSED for platforms that do not show category (e.g. BTG).
-- Preserve all previously allowed status values for Catalant compatibility.

do $$
declare
    constraint_name text;
begin
    select con.conname
      into constraint_name
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
    where nsp.nspname = 'public'
      and rel.relname = 'projects'
      and con.contype = 'c'
      and pg_get_constraintdef(con.oid) ilike '%platform_category_extraction_status%'
    limit 1;

    if constraint_name is not null then
        execute format(
            'alter table public.projects drop constraint %I',
            constraint_name
        );
    end if;
end $$;

alter table public.projects
    add constraint projects_platform_category_extraction_status_check
    check (
        platform_category_extraction_status is null
        or platform_category_extraction_status in (
            'FOUND_STRUCTURED',
            'FOUND_DEDICATED_SELECTOR',
            'FOUND_BREADCRUMB',
            'FOUND_EMBEDDED_DATA',
            'FOUND_TEXT_FALLBACK',
            'MISSING',
            'REJECTED_INVALID_CANDIDATE',
            'NOT_EXPOSED'
        )
    );

comment on column public.projects.platform_category_extraction_status is
'Category extraction outcome. NOT_EXPOSED means the platform does not show category (e.g. BTG).';
