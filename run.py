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
from translate import translate

# City slug -> (display name, Google search term). Slugs MUST match the web app's
# src/lib/cities.ts (the consumer app gates browsing on isCityId), so we only
# ship supported ones. Add a city there first before targeting it here.
CITY_CONFIG = {
    "cluj-napoca": ("Cluj-Napoca", "Cluj-Napoca"),
    "bucuresti":   ("București",   "Bucharest"),
    "iasi":        ("Iași",        "Iasi"),
    "botosani":    ("Botoșani",    "Botosani"),
}

# Google's Places searchText caps each query at ~60 results, so a single
# "restaurants in X" query can never feed a 300/day target. We fan out across
# many angles (cuisine, type, meal) per city; results are deduped by place id
# within the run, so overlap between angles is harmless. More angles = more
# unique candidates = more emails actually sent.
SEARCH_VARIANTS = [
    "restaurants in {city}, Romania",
    "best restaurants in {city}, Romania",
    "traditional Romanian restaurant in {city}",
    "pizza in {city}, Romania",
    "italian restaurant in {city}, Romania",
    "asian restaurant in {city}, Romania",
    "fast food in {city}, Romania",
    "cafe and brunch in {city}, Romania",
    "bistro in {city}, Romania",
    "pub and bar food in {city}, Romania",
    "fine dining in {city}, Romania",
    "burger in {city}, Romania",
]


# Neighborhood-level expansion. Each query gets its own ~60-result budget from
# Google, so scoping searches to neighborhoods/sectors digs out the long tail of
# venues that the city-wide top-60 never surfaces — the main lever for sustained
# daily volume. Keep these in the city's *search* spelling (matches CITY_CONFIG).
NEIGHBORHOODS = {
    "cluj-napoca": [
        "Centru", "Mănăștur", "Mărăști", "Gheorgheni", "Grigorescu", "Zorilor",
        "Andrei Mureșanu", "Bună Ziua", "Iris", "Plopilor", "Gruia", "Someșeni",
    ],
    "bucuresti": [
        "Sector 1", "Sector 2", "Sector 3", "Sector 4", "Sector 5", "Sector 6",
        "Centrul Vechi", "Floreasca", "Dorobanți", "Aviatorilor", "Herăstrău",
        "Cotroceni", "Unirii", "Universitate", "Victoriei", "Băneasa", "Pipera",
        "Militari", "Drumul Taberei", "Berceni", "Titan", "Crângași",
    ],
    "iasi": [
        "Centru", "Copou", "Tătărași", "Păcurari", "Nicolina", "Alexandru cel Bun",
        "Galata", "Podu Roș", "Tudor Vladimirescu", "Canta", "Dacia", "Bucium",
    ],
    "botosani": [
        "Centru", "Calea Națională", "Bucovina", "Primăverii", "Grivița",
        "Parcul Tineretului", "Pacea",
    ],
}

NEIGHBORHOOD_VARIANTS = [
    "restaurants in {hood}, {city}, Romania",
    "cafe bistro bar in {hood}, {city}, Romania",
]


def build_queries(city_slug: str, city_term: str):
    """
    City-wide angles first, then per-neighborhood angles. Each query is capped at
    ~60 results by Google, so more (deduped) queries = a larger reachable pool.
    The run stops once the target is hit, so the deeper neighborhood queries only
    execute when the broad ones didn't already fill the quota.
    """
    queries = [v.format(city=city_term) for v in SEARCH_VARIANTS]
    for hood in NEIGHBORHOODS.get(city_slug, []):
        queries += [v.format(hood=hood, city=city_term) for v in NEIGHBORHOOD_VARIANTS]
    return queries


def parse_args():
    p = argparse.ArgumentParser(description="Resto restaurant outreach pipeline")
    p.add_argument("--cities", default="cluj-napoca,bucuresti,iasi,botosani",
                   help="comma-separated city slugs (must match web src/lib/cities.ts)")
    p.add_argument("--max", type=int, default=300,
                   help="target NEW venues to import+email per run (the daily send cap)")
    p.add_argument("--per-city-search", type=int, default=60,
                   help="max results to scan per search query (Google caps this at ~60)")
    p.add_argument("--dry-run", action="store_true",
                   help="discover + crawl + map only; no DB write, no email send")
    return p.parse_args()


def apply_google_description(place: dict, copy: dict, do_translate: bool = True) -> None:
    """
    Mirror the venue's real Google Maps description across RO + EN, in place.

    Details are fetched in Romanian (places.details language_code='ro'), so when
    Google has a Romanian editorialSummary it lands in description_ro and we
    translate it to EN. When Google only has it in another language (usually EN),
    we keep that as description and translate it into RO. Either way both fields
    carry the *real* description — RO is the one shown to restaurants. If there's
    no summary, or translation is unavailable, the generic copy stays as fallback.
    """
    es = place.get("editorialSummary") or {}
    summary = es.get("text")
    if not summary:
        print("    · no Google description — using generic copy")
        return
    lang = (es.get("languageCode") or "").lower()
    if lang.startswith("ro"):
        copy["description_ro"] = summary
        en = translate(summary, target="en", source="ro") if do_translate else None
        if en:
            copy["description"] = en
        print(f"    · Google description [ro] -> EN translated: {bool(en)}")
    else:
        copy["description"] = summary
        ro = translate(summary, target="ro", source=lang or None) if do_translate else None
        if ro:
            copy["description_ro"] = ro
        print(f"    · Google description [{lang or '?'}] -> RO translated: {bool(ro)}")


def build_payload(place: dict, city_slug: str, city_name: str, email: str,
                  do_translate: bool = True):
    """Assemble the restaurants INSERT payload (visible=false) from a place."""
    name = (place.get("displayName") or {}).get("text") or "Restaurant"
    types = place.get("types") or []
    cuisine_key, cuisine_slugs, tag_slugs = map_taxonomy(types, name)
    copy = bilingual_copy(cuisine_key, name, city_name)
    opens_at, closes_at = places.opening_hours(place)
    hood = places.neighborhood(place) or city_name

    # Prefer Google's own description (RO-first), falling back to generic copy.
    apply_google_description(place, copy, do_translate=do_translate)

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
    target = args.max
    remaining = target
    seen: set[str] = set()  # place ids handled this run (dedup across query angles)
    print(f"TARGET: up to {target} emails this run · cities={cities}")

    for city_slug in cities:
        if remaining <= 0:
            break
        city_name, city_term = CITY_CONFIG[city_slug]
        queries = build_queries(city_slug, city_term)
        print(f"\n=== {city_name} ({city_slug}) — {len(queries)} search angles, "
              f"{remaining} of {target} still to send ===")

        for query in queries:
            if remaining <= 0:
                break
            try:
                candidates = places.search_text(query, max_results=args.per_city_search)
            except places.PlacesError as e:
                print(f"  ! search failed [{query}]: {e}")
                continue
            fresh = [c.get("id") for c in candidates if c.get("id") and c["id"] not in seen]
            print(f"  [{query}] {len(candidates)} found, {len(fresh)} new "
                  f"(remaining {remaining})")

            for place_id in fresh:
                if remaining <= 0:
                    break
                if place_id in seen:
                    continue
                seen.add(place_id)
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

                payload = build_payload(place, city_slug, city_name, email,
                                        do_translate=not args.dry_run)
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
                    print(f"  ✓ {name} <{email}> imported + emailed  ({stats['sent']}/{target})")
                except Exception as e:  # noqa: BLE001
                    stats["failed"] += 1
                    store.log_outreach(email=email, status="failed", restaurant_id=rid,
                                       error=str(e)[:300], city=city_slug)
                    print(f"  ! send failed for {name}: {e}")

                remaining -= 1

    print("\n=== summary ===")
    for k, v in stats.items():
        print(f"  {k:16}: {v}")
    print(f"\n  TARGET {target} — SENT {stats['sent']}")
    if stats["sent"] < target:
        print(f"  NOTE: reached {stats['sent']}/{target}. Out of fresh restaurants with "
              f"emails this run (no_email={stats['no_email']}, "
              f"already_contacted={stats['skipped_emailed']}, "
              f"already_imported={stats['skipped_dup']}). Add cities or search angles "
              f"to push closer to {target}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
