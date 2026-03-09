create extension if not exists pgcrypto;
create extension if not exists vector;
create schema if not exists auth;

create table if not exists auth.users (
  id uuid primary key,
  email text null,
  created_at timestamptz not null default now()
);
