-- OpenClaw-only queue schema.
-- Do not wire these tables into Groq/Cerebras code paths.

create table if not exists jobs (
  id uuid primary key,
  user_id text,
  status text not null check (status in ('queued', 'running', 'succeeded', 'failed')),
  worker_id text null,
  lease_expires_at timestamptz null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  input jsonb not null,
  result jsonb null,
  error text null
);

create table if not exists job_events (
  id bigserial primary key,
  job_id uuid not null references jobs(id) on delete cascade,
  ts timestamptz not null default now(),
  type text not null check (type in ('log', 'progress', 'token', 'final', 'error', 'status')),
  data jsonb not null
);

create table if not exists job_logs (
  id bigserial primary key,
  job_id uuid not null references jobs(id) on delete cascade,
  ts timestamptz not null default now(),
  level text not null check (level in ('info', 'warn', 'error', 'token', 'progress')),
  message text null,
  data jsonb null
);

create index if not exists ix_job_events_job_id_id on job_events(job_id, id);
create index if not exists ix_job_logs_job_id_id on job_logs(job_id, id);
create index if not exists ix_jobs_status_lease_expires_created_at on jobs(status, lease_expires_at, created_at);
create index if not exists ix_jobs_input_target on jobs ((input->>'target'));
