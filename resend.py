#!/usr/bin/env python3
"""
One-off op: resend an existing venue's outreach email to a new address.

Looks up the venue by its current contact_email, reads back the go-live token
the DB trigger already minted for it (so the link is the SAME one), and sends
the Romanian outreach email to the new address via Brevo. Optionally updates the
venue's stored contact_email to the new one.

Runs inside GitHub Actions where the Supabase + Brevo secrets live:
  python resend.py --from-email old@x.ro --to-email new@y.ro [--update-contact]
"""

from __future__ import annotations

import argparse
import os
import sys

import brevo
from db import Store


def parse_args():
    p = argparse.ArgumentParser(description="Resend a venue's go-live email to a new address")
    p.add_argument("--from-email", required=True,
                   help="the venue's current contact_email (used to find the venue)")
    p.add_argument("--to-email", required=True,
                   help="the new address to send the same go-live link to")
    p.add_argument("--update-contact", action="store_true",
                   help="also update the venue's stored contact_email to --to-email")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    store = Store()
    public_base = os.environ.get("PUBLIC_BASE_URL", "https://restonow.app").rstrip("/")

    # Find the venue by its current contact email (case-insensitive).
    res = (store.client.table("restaurants")
           .select("id,name,contact_email,city")
           .ilike("contact_email", args.from_email)
           .execute())
    rows = res.data or []
    if not rows:
        print(f"No venue found with contact_email={args.from_email!r}")
        return 1
    if len(rows) > 1:
        print(f"WARNING: {len(rows)} venues match {args.from_email!r}; "
              f"using the first: {[r['name'] for r in rows]}")
    r = rows[0]
    rid, name = r["id"], r["name"]

    token = store.get_golive_token(rid)
    if not token:
        print(f"No go-live token minted for {name!r} ({rid}) — cannot resend.")
        return 1
    url = f"{public_base}/go/{token}"

    print(f"Resending '{name}' go-live link -> {args.to_email}")
    print(f"  link: {url}")
    msg_id = brevo.send_ro(args.to_email, name, url)
    store.log_outreach(email=args.to_email, status="sent", restaurant_id=rid,
                       brevo_message_id=msg_id, city=r.get("city"))
    print(f"  ✓ sent (brevo msg {msg_id})")

    if args.update_contact:
        (store.client.table("restaurants")
         .update({"contact_email": args.to_email})
         .eq("id", rid).execute())
        print(f"  ✓ updated contact_email: {args.from_email} -> {args.to_email}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
