-- Supabase/Postgres control-plane schema for container orchestration.
-- Namespaced under `orchestration` to avoid collisions with existing public tables.

create extension if not exists pgcrypto;
create schema if not exists orchestration;

create table if not exists orchestration.hosts (
  host_id uuid primary key default gen_random_uuid(),
  name text not null,
  private_addr text null,
  docker_context jsonb not null default '{}'::jsonb,
  status text not null check (status in ('active', 'draining', 'down')),
  capacity jsonb null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists orchestration.releases (
  release_id uuid primary key default gen_random_uuid(),
  image_ref text not null,
  created_at timestamptz not null default now(),
  notes text null,
  skills_release text null
);

create table if not exists orchestration.agents (
  user_id uuid primary key references auth.users(id) on delete cascade,
  host_id uuid not null references orchestration.hosts(host_id) on delete restrict,
  volume_name text not null,
  active_container_name text not null,
  active_release_id uuid null references orchestration.releases(release_id) on delete set null,
  status text not null check (status in ('creating', 'ready', 'updating', 'error', 'stopped')),
  last_ready_at timestamptz null,
  last_error text null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists ix_orchestration_agents_host_id on orchestration.agents(host_id);
create index if not exists ix_orchestration_agents_status on orchestration.agents(status);
create index if not exists ix_orchestration_agents_host_id_status on orchestration.agents(host_id, status);

create table if not exists orchestration.schedules (
  schedule_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  cron text not null,
  payload jsonb not null default '{}'::jsonb,
  enabled boolean not null default true,
  last_run_at timestamptz null,
  next_run_at timestamptz null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists ix_orchestration_schedules_user_id on orchestration.schedules(user_id);
create index if not exists ix_orchestration_schedules_enabled_next_run_at on orchestration.schedules(enabled, next_run_at);

create table if not exists orchestration.jobs (
  job_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  schedule_id uuid null references orchestration.schedules(schedule_id) on delete set null,
  type text not null,
  status text not null check (status in ('queued', 'running', 'succeeded', 'failed')),
  started_at timestamptz null,
  finished_at timestamptz null,
  error text null,
  metrics jsonb null,
  created_at timestamptz not null default now()
);

create index if not exists ix_orchestration_jobs_user_id_status on orchestration.jobs(user_id, status);
create index if not exists ix_orchestration_jobs_schedule_id on orchestration.jobs(schedule_id);

create table if not exists orchestration.oauth_tokens (
  token_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null check (provider in ('google', 'spotify', 'notion')),
  access_token_encrypted text null,
  refresh_token_encrypted text null,
  token_type text null,
  scope text null,
  expires_at timestamptz null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, provider)
);

create index if not exists ix_orchestration_oauth_tokens_user_id on orchestration.oauth_tokens(user_id);
create index if not exists ix_orchestration_oauth_tokens_provider on orchestration.oauth_tokens(provider);

-- Runtime control-plane tables for ace-net-manager.
create table if not exists orchestration.runtime_operations (
  operation_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  operation_type text not null check (operation_type in ('new_user', 'deploy', 'rollback', 'restart')),
  requested_by text not null check (requested_by in ('system', 'admin', 'gateway')),
  requested_image_tag text null,
  status text not null check (status in ('queued', 'running', 'succeeded', 'failed', 'rolled_back', 'cancelled')),
  step text null,
  error_code text null,
  error_message text null,
  old_generation integer null,
  new_generation integer null,
  metadata jsonb not null default '{}'::jsonb,
  started_at timestamptz null,
  finished_at timestamptz null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists orchestration.user_runtimes (
  user_id uuid primary key references auth.users(id) on delete cascade,
  status text not null check (status in ('provisioning', 'active', 'deploying', 'restarting', 'rollback_pending', 'failed', 'disabled')),
  network_name text not null,
  volume_name text not null,
  current_generation integer null check (current_generation is null or current_generation >= 0),
  active_container_name text null,
  active_container_id text null,
  active_image_tag text null,
  previous_generation integer null check (previous_generation is null or previous_generation >= 0),
  previous_container_name text null,
  previous_container_id text null,
  previous_image_tag text null,
  desired_image_tag text null,
  last_health_status text null,
  last_health_checked_at timestamptz null,
  last_operation_id uuid null references orchestration.runtime_operations(operation_id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists orchestration.runtime_generations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  generation integer not null check (generation >= 0),
  container_name text not null,
  container_id text null,
  image_tag text not null,
  role text not null check (role in ('active', 'standby', 'retired', 'failed')),
  lifecycle_state text not null check (lifecycle_state in ('creating', 'starting', 'healthy', 'standby', 'promoted', 'draining', 'retired', 'failed', 'stopped', 'removed')),
  started_at timestamptz null,
  promoted_at timestamptz null,
  retired_at timestamptz null,
  health_status text null,
  health_summary text null,
  docker_labels_json jsonb not null default '{}'::jsonb,
  rollback_to_image_tag text null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, generation)
);

alter table if exists orchestration.runtime_generations
  drop constraint if exists runtime_generations_container_name_key;

create index if not exists ix_orchestration_user_runtimes_status on orchestration.user_runtimes(status);
create index if not exists ix_orchestration_runtime_generations_container_name on orchestration.runtime_generations(container_name);
create index if not exists ix_orchestration_runtime_generations_user_id_lifecycle_state on orchestration.runtime_generations(user_id, lifecycle_state);
create index if not exists ix_orchestration_runtime_generations_user_id_role on orchestration.runtime_generations(user_id, role);
create index if not exists ix_orchestration_runtime_operations_user_id_created_at on orchestration.runtime_operations(user_id, created_at desc);
create index if not exists ix_orchestration_runtime_operations_status_created_at on orchestration.runtime_operations(status, created_at);
