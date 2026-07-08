-- =============================================================================
-- Onboarding follow-up automation — "connect your POS" upsell, 3 days after go-live
-- =============================================================================
--  Run ONCE in the Supabase SQL editor (the same Supabase project the web app +
--  the outreach pipeline use). Additive + idempotent — safe to re-run.
--
--  Companion to 2026-06-26-outreach.sql. The initial outreach inserts a HIDDEN
--  venue and emails a go-live link. When the manager opens that link,
--  `redeem_golive_link()` flips the venue to visible=true and deletes its
--  golive_links row — but records NO timestamp of when that happened. This table
--  supplies that missing signal: the follow-up job (followup.py) stamps each
--  onboarded outreach venue the first time it sees it live, then sends a single
--  "connect your POS" follow-up `delay_days` (default 3) later.
--
--  It does NOT touch the web app's behaviour: this table is written only by the
--  follow-up script (service-role, bypasses RLS).
-- =============================================================================

create table if not exists onboarding_followup (
  restaurant_id     uuid primary key references restaurants(id) on delete cascade,
  email             text not null,          -- who we email the follow-up to
  detected_live_at  timestamptz not null default now(),  -- first time we saw it live (onboard proxy)
  followup_status   text not null default 'pending',     -- pending | sent | failed
  followup_sent_at  timestamptz,
  brevo_message_id  text,
  error             text,
  created_at        timestamptz not null default now()
);

-- Fast lookup of what is still owed a follow-up.
create index if not exists onboarding_followup_pending_idx
  on onboarding_followup (followup_status) where followup_status = 'pending';

alter table onboarding_followup enable row level security;

-- Platform-only read; all writes go through the service-role key (bypasses RLS),
-- so there is intentionally no insert/update policy for anon/authenticated.
-- Requires the web app's is_platform_user() helper (already present from the web
-- schema). Drop this policy block if running against a DB without it — the
-- service-role script does not need it to write.
drop policy if exists "platform read onboarding_followup" on onboarding_followup;
create policy "platform read onboarding_followup" on onboarding_followup for select
  using (public.is_platform_user());
