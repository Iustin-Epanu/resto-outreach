#!/usr/bin/env python3
"""
Restaurant outreach pipeline — entrypoint.

For each target city:
  1. Places text search        -> candidate place ids
  2. Place details             -> name, address, types, phone, website, photos, hours
  3. dedup (google_place_id)   -> skip places already imported
  4. crawl website for email   -> REQUIRED; no email => skip + log 'no_email'
  5. dedup (already emailed)   -> skip + log 'skipped_dup'
  6. map taxonomy + copy       -> cuisine_slugs, tag_slugs, bilingual labels
  7. upload Google photos      -> restaurant-images bucket (Unsplash fallback)
  8. insert restaurant (hidden)-> DB trigger mints a go-live link
  9. read go-live token        -> {PUBLIC_BASE_URL}/go/{token}
 10. Brevo send (Romanian)     -> log 'sent' | 'failed'

--dry-run does everything EXCEPT the DB write + Brevo send (prints what it would
do), so you can test discovery/email/taxonomy without spending or sending.

Usage:
  python run.py --cities cluj-napoca,bucuresti --max 30
  python run.py --cities cluj-napoca --max 3 --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

import places
import email_crawler
from taxonomy_map import map_taxonomy
from inference import bilingual_copy, fallback_image_urls

# City slug -> (display name, search query). Slugs MUST match the web app's
# src/lib/cities.ts (the consumer app gates browsing on isCityId), so we only
# ship supported ones. Add a city there first before targeting it here.
CITY_CONFIG = {
    "cluj-napoca": ("Cluj-Napoca", "restaurants in Cluj-Napoca, Romania"),
    "bucuresti":   ("București",   "restaurants in Bucharest, Romania"),
    "iasi":        ("Iași",        "restaurants in Iasi, Romania"),
    "botosani":    ("Botoșani",    "restaurants in Botosani, Romania"),
}


def parse_args():
    p = argparse.ArgumentParser(description="Resto restaurant outreach pipeline")
    p.add_argument("--cities", default="cluj-napoca",
                   help="comma-separated city slugs (must match web src/lib/cities.ts)")
    p.add_argument("--max", type=int, default=30,
                   help="max NEW venues to import+email per run (across all cities)")
    p.add_argument("--per-city-search", type=int, default=60,
                   help="max search results to scan per city")
    p.add_argument("--dry-run", action="store_true",
                   help="discover + crawl + map only; no DB write, no email send")
    return p.parse_args()


def build_payload(place: dict, city_slug: str, city_name: str, email: str):
    """Assemble the restaurants INSERT payload (visible=false) from a place."""
    name = (place.get("displayName") or {}).get("text") or "Restaurant"
    types = place.get("types") or []
    cuisine_key, cuisine_slugs, tag_slugs = map_taxonomy(types, name)
    copy = bilingual_copy(cuisine_key, name, city_name)
    opens_at, closes_at = places.opening_hours(place)
    hood = places.neighborhood(place) or city_name

    summary = (place.get("editorialSummary") or {}).get("text")
    if summary:  # prefer Google's own description when present
        copy["description"] = summary

    # image_url is NOT NULL — seed it with the cuisine fallback so the insert is
    # always valid; attach_images() upgrades it to real Google photos after.
    hero, gallery = fallback_image_urls(cuisine_key)

    payload = {
        "name": name,
        "image_url": hero, "gallery_image_urls": gallery,
        "tagline": copy["tagline"], "tagline_ro": copy["tagline_ro"],
        "description": copy["description"], "description_ro": copy["description_ro"],
        "cuisine": copy["cuisine"], "cuisine_ro": copy["cuisine_ro"],
        "cuisine_slugs": cuisine_slugs, "tag_slugs": tag_slugs,
        "maps_url": place.get("googleMapsUri"),
        "neighborhood": hood,
        "city": city_slug,
        "visible": False,
        "current_capacity": "GREEN", "capacity_seats": 40,
        "currently_occupied_tables": 0,
        "opens_at": opens_at, "closes_at": closes_at,
        "contact_email": email,
        "phone": place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber"),
        "website": place.get("websiteUri"),
        "google_place_id": place.get("id"),
    }
    return payload


def attach_images(store, restaurant_id: str, place: dict) -> bool:
    """
    Upload up to 4 Google photos to storage and point the row at them. Returns
    True if at least one real photo was attached; False means the row keeps the
    cuisine fallback hero/gallery already set at insert time.
    """
    photos = place.get("photos") or []
    urls = []
    for ph in photos[:4]:
        data = places.photo_bytes(ph["name"])
        if not data:
            continue
        url = store.upload_image(restaurant_id, data)
        if url:
            urls.append(url)
    if not urls:
        return False
    store.update_images(restaurant_id, urls[0], urls[1:4])
    return True


def main() -> int:
    args = parse_args()
    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    unknown = [c for c in cities if c not in CITY_CONFIG]
    if unknown:
        print(f"WARNING: skipping unsupported city slugs (not in cities.ts): {unknown}")
        cities = [c for c in cities if c in CITY_CONFIG]
    if not cities:
        print("No valid cities to process.")
        return 1

    public_base = os.environ.get("PUBLIC_BASE_URL", "https://restonow.app").rstrip("/")

    store = None
    if not args.dry_run:
        from db import Store
        store = Store()

    stats = {"discovered": 0, "skipped_dup": 0, "no_email": 0,
             "skipped_emailed": 0, "inserted": 0, "sent": 0, "failed": 0}
    remaining = args.max

    for city_slug in cities:
        if remaining <= 0:
            break
        city_name, query = CITY_CONFIG[city_slug]
        print(f"\n=== {city_name} ({city_slug}) — searching ===")
        try:
            candidates = places.search_text(query, max_results=args.per_city_search)
        except places.PlacesError as e:
            print(f"  ! search failed: {e}")
            continue
        print(f"  {len(candidates)} candidates")

        for cand in candidates:
            if remaining <= 0:
                break
            place_id = cand.get("id")
            if not place_id:
                continue
            stats["discovered"] += 1

            # 3. dedup on place id (skip the details call entirely when known)
            if store and store.place_exists(place_id):
                stats["skipped_dup"] += 1
                continue

            try:
                place = places.details(place_id)
            except places.PlacesError as e:
                print(f"  ! details failed for {place_id}: {e}")
                continue
            if not place:
                continue
            name = (place.get("displayName") or {}).get("text") or place_id
            if place.get("businessStatus") not in (None, "OPERATIONAL"):
                continue

            # 4. email is required
            email = email_crawler.find(place.get("websiteUri"))
            if not email:
                stats["no_email"] += 1
                print(f"  - {name}: no email (skip)")
                if store:
                    store.log_outreach(email="unknown@" + (place.get("websiteUri") or "no-site"),
                                       status="no_email", city=city_slug)
                continue

            # 5. dedup on already-emailed
            if store and store.email_already_contacted(email):
                stats["skipped_emailed"] += 1
                print(f"  - {name}: {email} already contacted (skip)")
                continue

            payload = build_payload(place, city_slug, city_name, email)
            go_live_url = None

            if args.dry_run:
                print(f"  ~ DRY {name} <{email}> [{','.join(payload['cuisine_slugs'])}"
                      f" | {','.join(payload['tag_slugs'])}] phone={payload['phone']}")
                remaining -= 1
                continue

            # 7-8. insert (hidden, with fallback hero) — trigger mints the
            #      go-live link — then upgrade to real Google photos if any.
            try:
                rid = store.insert_restaurant(payload)
                attach_images(store, rid, place)
                stats["inserted"] += 1
                token = store.get_golive_token(rid)
                go_live_url = f"{public_base}/go/{token}" if token else None
            except Exception as e:  # noqa: BLE001
                print(f"  ! insert failed for {name}: {e}")
                store.log_outreach(email=email, status="failed",
                                   error=str(e)[:300], city=city_slug)
                continue

            # 10. send
            if not go_live_url:
                store.log_outreach(email=email, status="failed", restaurant_id=rid,
                                   error="no go-live token minted", city=city_slug)
                continue
            try:
                import brevo
                msg_id = brevo.send_ro(email, payload["name"], go_live_url)
                store.log_outreach(email=email, status="sent", restaurant_id=rid,
                                   brevo_message_id=msg_id, city=city_slug)
                stats["sent"] += 1
                print(f"  ✓ {name} <{email}> imported + emailed")
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                store.log_outreach(email=email, status="failed", restaurant_id=rid,
                                   error=str(e)[:300], city=city_slug)
                print(f"  ! send failed for {name}: {e}")

            remaining -= 1

    print("\n=== summary ===")
    for k, v in stats.items():
        print(f"  {k:16}: {v}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
