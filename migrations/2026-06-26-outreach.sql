-- =============================================================================
-- Restaurant outreach automation — contact columns + outreach log
-- =============================================================================
--  Run ONCE in the Supabase SQL editor (the same Supabase project the web app
--  uses). Additive + idempotent — safe to re-run.
--
--  Adds the columns/table the outreach script needs. It does NOT touch the web
--  app's behaviour: these columns are written only by this script.
-- =============================================================================

-- Contact + provenance columns on restaurants (all nullable, additive).
alter table restaurants add column if not exists contact_email   text;
alter table restaurants add column if not exists phone           text;
alter table restaurants add column if not exists website         text;
alter table restaurants add column if not exists google_place_id text;

-- Dedup guard: one row per Google place. Partial index so existing rows with a
-- NULL place_id (hand-added / seeded) don't collide with each other.
create unique index if not exists restaurants_google_place_id_key
  on restaurants (google_place_id) where google_place_id is not null;

-- Outreach log — one row per send attempt. Doubles as the "already contacted
-- this email" history so a re-run never emails anyone twice.
create table if not exists outreach_log (
  id               uuid primary key default uuid_generate_v4(),
  restaurant_id    uuid references restaurants(id) on delete set null,
  email            text not null,
  status           text not null,        -- sent | no_email | failed | bounced | skipped_dup
  brevo_message_id text,
  error            text,
  city             text,
  created_at       timestamptz not null default now()
);

create index if not exists outreach_log_email_idx on outreach_log (lower(email));
create index if not exists outreach_log_restaurant_idx on outreach_log (restaurant_id);

alter table outreach_log enable row level security;

-- Platform-only read; all writes go through the service-role key (bypasses RLS),
-- so there is intentionally no insert policy for anon/authenticated.
-- NOTE: requires the web app's is_platform_user() helper to already exist (it
-- does, from the web schema). If running against a DB without it, drop this
-- policy block — the service-role script does not need it to write.
drop policy if exists "platform read outreach_log" on outreach_log;
create policy "platform read outreach_log" on outreach_log for select
  using (public.is_platform_user());
