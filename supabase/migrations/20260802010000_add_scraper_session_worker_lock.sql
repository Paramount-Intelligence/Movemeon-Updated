-- Atomic multi-replica worker lock stored on scraper_sessions (shared schema).
-- Lock fields are independent of session_data (cookies) so cookie updates
-- never clear the lock and lock release never deletes cookies.

alter table public.scraper_sessions
    add column if not exists worker_lock_owner text,
    add column if not exists worker_lock_expires_at timestamptz,
    add column if not exists worker_lock_heartbeat_at timestamptz;

create index if not exists idx_scraper_sessions_worker_lock_expiry
on public.scraper_sessions (worker_lock_expires_at);

-- Acquire: succeed only when no owner, lock expired, or same owner.
create or replace function public.acquire_scraper_worker_lock(
    p_platform text,
    p_owner text,
    p_ttl_seconds integer default 180
)
returns table (
    acquired boolean,
    owner text,
    expires_at timestamptz,
    heartbeat_at timestamptz
)
language plpgsql
security definer
set search_path = public
as $$
declare
    v_now timestamptz := now();
    v_expires timestamptz := now() + make_interval(secs => greatest(coalesce(p_ttl_seconds, 180), 1));
    v_row public.scraper_sessions%rowtype;
begin
    if p_platform is null or length(trim(p_platform)) = 0 then
        raise exception 'platform is required';
    end if;
    if p_owner is null or length(trim(p_owner)) = 0 then
        raise exception 'owner is required';
    end if;

    insert into public.scraper_sessions as s (
        platform,
        session_data,
        session_version,
        metadata,
        worker_lock_owner,
        worker_lock_expires_at,
        worker_lock_heartbeat_at
    )
    values (
        p_platform,
        '{"cookies":[]}'::jsonb,
        1,
        '{"created_for":"worker_lock"}'::jsonb,
        p_owner,
        v_expires,
        v_now
    )
    on conflict (platform) do update
    set
        worker_lock_owner = excluded.worker_lock_owner,
        worker_lock_expires_at = excluded.worker_lock_expires_at,
        worker_lock_heartbeat_at = excluded.worker_lock_heartbeat_at,
        updated_at = now()
    where
        s.worker_lock_owner is null
        or s.worker_lock_expires_at is null
        or s.worker_lock_expires_at <= v_now
        or s.worker_lock_owner = p_owner
    returning * into v_row;

    if not found then
        select * into v_row from public.scraper_sessions where platform = p_platform;
        return query select false, v_row.worker_lock_owner, v_row.worker_lock_expires_at, v_row.worker_lock_heartbeat_at;
        return;
    end if;

    return query select true, v_row.worker_lock_owner, v_row.worker_lock_expires_at, v_row.worker_lock_heartbeat_at;
end;
$$;

create or replace function public.renew_scraper_worker_lock(
    p_platform text,
    p_owner text,
    p_ttl_seconds integer default 180
)
returns table (
    renewed boolean,
    owner text,
    expires_at timestamptz,
    heartbeat_at timestamptz
)
language plpgsql
security definer
set search_path = public
as $$
declare
    v_now timestamptz := now();
    v_expires timestamptz := now() + make_interval(secs => greatest(coalesce(p_ttl_seconds, 180), 1));
    v_row public.scraper_sessions%rowtype;
begin
    update public.scraper_sessions as s
    set
        worker_lock_expires_at = v_expires,
        worker_lock_heartbeat_at = v_now,
        updated_at = now()
    where s.platform = p_platform
      and s.worker_lock_owner = p_owner
      and s.worker_lock_expires_at is not null
      and s.worker_lock_expires_at > v_now
    returning * into v_row;

    if not found then
        select * into v_row from public.scraper_sessions where platform = p_platform;
        return query select false, v_row.worker_lock_owner, v_row.worker_lock_expires_at, v_row.worker_lock_heartbeat_at;
        return;
    end if;

    return query select true, v_row.worker_lock_owner, v_row.worker_lock_expires_at, v_row.worker_lock_heartbeat_at;
end;
$$;

create or replace function public.release_scraper_worker_lock(
    p_platform text,
    p_owner text
)
returns table (
    released boolean,
    owner text,
    expires_at timestamptz,
    heartbeat_at timestamptz
)
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row public.scraper_sessions%rowtype;
begin
    update public.scraper_sessions as s
    set
        worker_lock_owner = null,
        worker_lock_expires_at = null,
        worker_lock_heartbeat_at = null,
        updated_at = now()
    where s.platform = p_platform
      and s.worker_lock_owner = p_owner
    returning * into v_row;

    if not found then
        select * into v_row from public.scraper_sessions where platform = p_platform;
        return query select false, v_row.worker_lock_owner, v_row.worker_lock_expires_at, v_row.worker_lock_heartbeat_at;
        return;
    end if;

    -- Cookies / session_data intentionally untouched.
    return query select true, v_row.worker_lock_owner, v_row.worker_lock_expires_at, v_row.worker_lock_heartbeat_at;
end;
$$;

revoke all on function public.acquire_scraper_worker_lock(text, text, integer) from public, anon, authenticated;
revoke all on function public.renew_scraper_worker_lock(text, text, integer) from public, anon, authenticated;
revoke all on function public.release_scraper_worker_lock(text, text) from public, anon, authenticated;

grant execute on function public.acquire_scraper_worker_lock(text, text, integer) to service_role;
grant execute on function public.renew_scraper_worker_lock(text, text, integer) to service_role;
grant execute on function public.release_scraper_worker_lock(text, text) to service_role;

-- Also grant to authenticated service paths that use the secret/service_role key via PostgREST.
-- Dashboard anon/authenticated users remain revoked above.
