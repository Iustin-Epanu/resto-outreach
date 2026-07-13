#!/usr/bin/env python3
"""
Onboarding follow-up pipeline — the "connect your POS" upsell.

Companion to run.py. run.py imports hidden venues and emails a go-live link;
when the manager opens that link the venue flips visible=true (it "onboards").
This script sends ONE follow-up email ~1 day later, pitching the next step:
connect the POS so Resto shows live table capacity.

Flow (all service-role, RLS-bypassing, via db.Store):
  1. Find onboarded outreach venues (visible=true + contact_email set).
  2. Stamp each into onboarding_followup the first time it's seen live
     (detected_live_at = now()). Idempotent — never re-stamps.
  3. Pick the ones due: detected_live_at older than --delay-days (default 1),
     unless --backfill sends every pending row immediately (initial catch-up).
  4. For each, skip if it already connected POS, else send via Brevo with the
     manager-login card and mark the row sent/failed/skipped.

Dedup: onboarding_followup has one row per restaurant (primary key), and only
`pending` rows are ever sent — so a venue is followed up at most once.

Runs daily via GitHub Actions (.github/workflows/followup.yml) plus manual
dispatch. --dry-run does everything except the DB write of send status + the
actual send. Env mirrors run.py (SUPABASE_*, BREVO_*, PUBLIC_BASE_URL).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Resto onboarding follow-up (connect POS)")
    p.add_argument("--delay-days", type=int, default=1,
                   help="Send the follow-up this many days after a venue is first "
                        "seen live (default 1). Ignored when --backfill is set.")
    p.add_argument("--max", type=int, default=300,
                   help="Max follow-ups to send this run (safety cap).")
    p.add_argument("--backfill", action="store_true",
                   help="Send to EVERY pending venue now, ignoring the delay. Use "
                        "once to clear the backlog of already-live venues.")
    p.add_argument("--dry-run", action="store_true",
                   help="Detect + choose recipients but write nothing and send nothing.")
    return p.parse_args()


def _parse_ts(value: str) -> datetime:
    """Parse a Supabase ISO timestamp into an aware UTC datetime."""
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main() -> int:
    args = parse_args()
    public_base = os.environ.get("PUBLIC_BASE_URL", "https://restonow.app").rstrip("/")
    login_url = f"{public_base}/signin"

    from db import Store
    store = Store()

    stats = {"onboarded": 0, "newly_stamped": 0, "pending": 0, "due": 0,
             "sent": 0, "skipped_pos": 0, "no_creds": 0, "failed": 0}

    # 1 + 2. Detect onboarded venues and stamp any first-seen-live ones.
    onboarded = store.find_onboarded_outreach_venues()
    name_by_id = {r["restaurant_id"]: r["name"] for r in onboarded}
    stats["onboarded"] = len(onboarded)
    already: set[str] = set()
    if args.dry_run:
        already = store._existing_followup_ids([r["restaurant_id"] for r in onboarded])
        stats["newly_stamped"] = sum(1 for r in onboarded
                                     if r["restaurant_id"] not in already)
    else:
        stats["newly_stamped"] = store.record_onboarding_detections(onboarded)
    print(f"ONBOARDED outreach venues live: {stats['onboarded']} "
          f"(newly stamped this run: {stats['newly_stamped']})")

    # 3. Which pending rows are due?
    pending = store.pending_followups()
    if args.dry_run:
        # Nothing was written, so simulate what stamping would produce (newly
        # detected venues, stamped now) on top of the real pending rows, so the
        # preview reflects post-stamp reality instead of an empty table.
        now_iso = datetime.now(timezone.utc).isoformat()
        pending = pending + [
            {"restaurant_id": r["restaurant_id"], "email": r["email"],
             "detected_live_at": now_iso}
            for r in onboarded if r["restaurant_id"] not in already
        ]
    stats["pending"] = len(pending)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.delay_days)
    due = []
    for row in pending:
        if args.backfill or _parse_ts(row["detected_live_at"]) <= cutoff:
            due.append(row)
    stats["due"] = len(due)
    mode = "BACKFILL (ignoring delay)" if args.backfill else f"delay {args.delay_days}d"
    print(f"PENDING: {stats['pending']} · DUE now [{mode}]: {stats['due']} "
          f"· cap {args.max}")

    if args.dry_run:
        for row in due[:args.max]:
            print(f"  [dry-run] would follow up: {row['email']} ({row['restaurant_id']})")
        _summary(stats)
        return 0

    import brevo

    # 4. Send.
    for row in due:
        if stats["sent"] >= args.max:
            print(f"  reached send cap ({args.max}); stopping.")
            break
        rid = row["restaurant_id"]
        email = row["email"]

        # Skip if the venue already connected its POS — the offer is moot.
        if store.venue_pos_enabled(rid):
            store.mark_followup(rid, "skipped", error="pos already connected")
            stats["skipped_pos"] += 1
            print(f"  · skip {email}: POS already connected")
            continue

        login_email, login_password = store.get_manager_credentials(rid)
        if not (login_email and login_password):
            # No credentials to hand them — still worth the email, just without
            # the login card. Note it so we can see it in the log.
            stats["no_creds"] += 1

        # Venue display name: fall back to the email's local part if absent.
        name = name_by_id.get(rid) or email.split("@")[0]

        try:
            msg_id = brevo.send_followup_ro(
                email, name, login_url,
                login_email=login_email, login_password=login_password,
            )
            store.mark_followup(rid, "sent", brevo_message_id=msg_id)
            store.log_outreach(email=email, status="followup_sent",
                               restaurant_id=rid, brevo_message_id=msg_id)
            stats["sent"] += 1
            print(f"  ✓ {name} <{email}> follow-up sent ({stats['sent']}/{args.max})")
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            store.mark_followup(rid, "failed", error=str(e)[:300])
            print(f"  ! send failed for {name} <{email}>: {e}")

    _summary(stats)
    return 0


def _summary(stats: dict) -> None:
    print("\n=== follow-up summary ===")
    for k, v in stats.items():
        print(f"  {k:14}: {v}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: {e}")
        sys.exit(1)
