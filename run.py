#!/usr/bin/env python3
"""
Restaurant outreach pipeline — entrypoint.

For each target city:
  1. Places text search        -> candidate place ids
  2. Place details             -> name, address, coords, types, phone, website, photos, hours
  3. dedup (google_place_id)   -> skip places already imported
 3b. geo guard                 -> coords must be nearest to + within radius of the
                                  searched city, else skip (no cross-city imports)
  4. crawl website for email   -> REQUIRED; no email => skip + log 'no_email'
  5. dedup (already emailed)   -> skip + log 'skipped_dup'
  6. map taxonomy + copy       -> cuisine_slugs, tag_slugs, bilingual labels
  7. upload Google photos      -> restaurant-images bucket (Unsplash fallback)
  8. insert restaurant (hidden)-> DB trigger mints a go-live link; `email` set
  9. read go-live token        -> {PUBLIC_BASE_URL}/go/{token}
 9b. provision manager login   -> auth user + manager_credentials (email+password)
 10. Brevo send (Romanian)     -> go-live link + login link + credentials;
                                  log 'sent' | 'failed'

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


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a sibling .env into os.environ for local runs.

    No-op if the file is missing (e.g. GitHub Actions, where secrets are already
    in the environment). Never overrides variables that are already set, so real
    CI secrets always win over a stale local .env.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

import places
import email_crawler
import geo
import filters
from taxonomy_map import map_taxonomy
from inference import bilingual_copy, fallback_image_urls
from translate import translate

# City slug -> (display name, Google search term). Slugs MUST match the web app's
# src/lib/cities.ts (the consumer app gates browsing on isCityId) AND geo.py's
# CITY_CENTROIDS (the geo guard rejects slugs it doesn't know). Add a city to
# both before targeting it here. Order = allocation order for non-priority
# cities (roughly by population, mirroring cities.ts).
CITY_CONFIG = {
    "bucuresti":             ("București",             "Bucharest"),
    "cluj-napoca":           ("Cluj-Napoca",           "Cluj-Napoca"),
    "timisoara":             ("Timișoara",             "Timisoara"),
    "iasi":                  ("Iași",                  "Iasi"),
    "constanta":             ("Constanța",             "Constanta"),
    "craiova":               ("Craiova",               "Craiova"),
    "brasov":                ("Brașov",                "Brasov"),
    "galati":                ("Galați",                "Galati"),
    "ploiesti":              ("Ploiești",              "Ploiesti"),
    "oradea":                ("Oradea",                "Oradea"),
    "braila":                ("Brăila",                "Braila"),
    "arad":                  ("Arad",                  "Arad"),
    "pitesti":               ("Pitești",               "Pitesti"),
    "sibiu":                 ("Sibiu",                 "Sibiu"),
    "bacau":                 ("Bacău",                 "Bacau"),
    "targu-mures":           ("Târgu Mureș",           "Targu Mures"),
    "baia-mare":             ("Baia Mare",             "Baia Mare"),
    "buzau":                 ("Buzău",                 "Buzau"),
    "botosani":              ("Botoșani",              "Botosani"),
    "satu-mare":             ("Satu Mare",             "Satu Mare"),
    "ramnicu-valcea":        ("Râmnicu Vâlcea",        "Ramnicu Valcea"),
    "suceava":               ("Suceava",               "Suceava"),
    "piatra-neamt":          ("Piatra Neamț",          "Piatra Neamt"),
    "drobeta-turnu-severin": ("Drobeta-Turnu Severin", "Drobeta-Turnu Severin"),
    "focsani":               ("Focșani",               "Focsani"),
    "targoviste":            ("Târgoviște",            "Targoviste"),
    "bistrita":              ("Bistrița",              "Bistrita"),
    "resita":                ("Reșița",                "Resita"),
    "tulcea":                ("Tulcea",                "Tulcea"),
    "slatina":               ("Slatina",               "Slatina"),
    "calarasi":              ("Călărași",              "Calarasi"),
    "alba-iulia":            ("Alba Iulia",            "Alba Iulia"),
    "deva":                  ("Deva",                  "Deva"),
    "giurgiu":               ("Giurgiu",               "Giurgiu"),
    "zalau":                 ("Zalău",                 "Zalau"),
    "alexandria":            ("Alexandria",            "Alexandria"),
    "vaslui":                ("Vaslui",                "Vaslui"),
    "sfantu-gheorghe":       ("Sfântu Gheorghe",       "Sfantu Gheorghe"),
    "targu-jiu":             ("Târgu Jiu",             "Targu Jiu"),
    "slobozia":              ("Slobozia",              "Slobozia"),
    "miercurea-ciuc":        ("Miercurea Ciuc",        "Miercurea Ciuc"),
    "buftea":                ("Buftea",                "Buftea"),
    # --- Romanian litoral (Black Sea coast) ------------------------------- #
    # Peak-season targets. Constanța (above) is the county seat; the resorts
    # below are geo-separated with their own centroids in geo.py so a Mamaia or
    # Năvodari venue is never filed under Constanța. Small satellite resorts are
    # folded into the nearest listed town (see geo.py): Jupiter/Venus/Cap Aurora
    # -> neptun-olimp, Saturn -> mangalia, Eforie Sud -> eforie.
    "mamaia":                ("Mamaia",                "Mamaia"),
    "navodari":              ("Năvodari",              "Navodari"),
    "eforie":                ("Eforie",                "Eforie"),
    "techirghiol":           ("Techirghiol",           "Techirghiol"),
    "costinesti":            ("Costinești",            "Costinesti"),
    "neptun-olimp":          ("Neptun-Olimp",          "Neptun Olimp"),
    "mangalia":              ("Mangalia",              "Mangalia"),
    "2mai":                  ("2 Mai",                 "2 Mai"),
    "vama-veche":            ("Vama Veche",            "Vama Veche"),
}

# Romanian coast. Constanța stays in PRIORITY (it's a top-10 city), but the
# resort towns get their own budget tier so the coast is actually covered in
# season instead of being starved by the big inland cities. See allocate_quotas.
COASTAL_CITIES = ["mamaia", "navodari", "eforie", "techirghiol", "costinesti",
                  "neptun-olimp", "mangalia", "2mai", "vama-veche"]

# Cities that get the lion's share of each run's send budget (see
# allocate_quotas). Everything else in CITY_CONFIG still gets a small
# guaranteed slice so the long tail keeps growing.
PRIORITY_CITIES = ["cluj-napoca", "bucuresti", "iasi", "timisoara", "brasov"]
# Three-tier budget split (see allocate_quotas): the big inland cities lead but
# no longer hog the run, the coast gets a dedicated in-season slice, and the
# long tail keeps its guaranteed share. Rest gets whatever's left (1 - the two).
PRIORITY_SHARE = 0.45  # priority inland cities
COASTAL_SHARE = 0.25   # COASTAL_CITIES as a group (Constanța is priority, not here)

# Google's Places searchText caps each query at ~60 results, so a single
# "restaurants in X" query can never feed a 300/day target. We fan out across
# many angles (cuisine, type, meal) per city; results are deduped by place id
# within the run, so overlap between angles is harmless. More angles = more
# unique candidates = more emails actually sent.
#
# Each variant is (template, includedType). includedType="restaurant" was
# previously hard-coded for ALL queries, which silently excluded every cafe and
# coffee shop from results — cafe queries need "cafe"/"coffee_shop", terrace
# queries need None (those venues span restaurant/cafe/bar types).
#
# Queries run in order and the run stops when the city's quota fills, so the
# coffee-shop and terrace angles go FIRST — they get first crack at each
# city's budget.
SEARCH_VARIANTS = [
    ("specialty coffee in {city}, Romania",          "cafe"),
    ("coffee shop in {city}, Romania",               "coffee_shop"),
    ("cafenea in {city}, Romania",                   "cafe"),
    ("cafe and brunch in {city}, Romania",           "cafe"),
    ("terasa in {city}, Romania",                    None),
    ("restaurant cu terasa in {city}, Romania",      "restaurant"),
    ("restaurants in {city}, Romania",               "restaurant"),
    ("best restaurants in {city}, Romania",          "restaurant"),
    ("traditional Romanian restaurant in {city}",    "restaurant"),
    ("pizza in {city}, Romania",                     "restaurant"),
    ("italian restaurant in {city}, Romania",        "restaurant"),
    ("asian restaurant in {city}, Romania",          "restaurant"),
    ("fast food in {city}, Romania",                 "restaurant"),
    ("bistro in {city}, Romania",                    "restaurant"),
    ("pub and bar food in {city}, Romania",          "restaurant"),
    ("fine dining in {city}, Romania",               "restaurant"),
    ("burger in {city}, Romania",                    "restaurant"),
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
        "Centru", "Calea Națională", "Primăverii", "Grivița",
        "Parcul Tineretului", "Pacea",
    ],
    "timisoara": [
        "Cetate", "Fabric", "Iosefin", "Elisabetin", "Complexul Studențesc",
        "Circumvalațiunii", "Aradului", "Lipovei", "Șagului", "Girocului",
    ],
    "brasov": [
        "Centrul Vechi", "Șchei", "Centrul Civic", "Astra", "Tractorul",
        "Bartolomeu", "Răcădău", "Noua", "Poiana Brașov",
    ],
}

NEIGHBORHOOD_VARIANTS = [
    ("coffee shop cafenea in {hood}, {city}, Romania", "cafe"),
    ("restaurants in {hood}, {city}, Romania",         "restaurant"),
    ("cafe bistro bar in {hood}, {city}, Romania",     None),
]


def build_queries(city_slug: str, city_term: str):
    """
    City-wide angles first, then per-neighborhood angles, as (query, includedType)
    tuples. Each query is capped at ~60 results by Google, so more (deduped)
    queries = a larger reachable pool. The run stops once the city quota is hit,
    so the deeper neighborhood queries only execute when the broad ones didn't
    already fill it.
    """
    queries = [(v.format(city=city_term), t) for v, t in SEARCH_VARIANTS]
    for hood in NEIGHBORHOODS.get(city_slug, []):
        queries += [(v.format(hood=hood, city=city_term), t)
                    for v, t in NEIGHBORHOOD_VARIANTS]
    return queries


def allocate_quotas(cities: list[str], target: int) -> dict[str, int]:
    """
    Split the run's send target across three tiers — priority inland cities
    (PRIORITY_SHARE), the coast (COASTAL_SHARE), and everyone else (the
    remainder) — dividing each tier's slice evenly among its members (at least 1
    each while the target allows). Unused quota rolls forward to the next city at
    run time, so an under-filled city never strands budget.

    A tier absent from `cities` has its share redistributed proportionally to the
    tiers that ARE present, so e.g. a coast-only run (`--cities mamaia,mangalia`)
    still spends the whole target. When only one tier is present we just split
    evenly, matching the old single-tier behavior.
    """
    pri = [c for c in cities if c in PRIORITY_CITIES]
    coast = [c for c in cities if c in COASTAL_CITIES]
    rest = [c for c in cities if c not in PRIORITY_CITIES and c not in COASTAL_CITIES]

    present = [(g, s) for g, s in
               ((pri, PRIORITY_SHARE), (coast, COASTAL_SHARE),
                (rest, max(0.0, 1 - PRIORITY_SHARE - COASTAL_SHARE)))
               if g]
    if len(present) <= 1:
        share = max(1, target // max(1, len(cities)))
        return {c: share for c in cities}

    total_share = sum(s for _, s in present) or 1.0
    quotas: dict[str, int] = {}
    for group, share in present:
        tier_budget = round(target * share / total_share)
        each = max(1, tier_budget // len(group))
        for c in group:
            quotas[c] = each
    return quotas


def parse_args():
    p = argparse.ArgumentParser(description="Resto restaurant outreach pipeline")
    p.add_argument("--cities", default="all",
                   help="comma-separated city slugs (must match web src/lib/cities.ts), "
                        "or 'all' for every supported city (priority cities first)")
    p.add_argument("--max", type=int, default=300,
                   help="target NEW venues to import+email per run (the daily send cap)")
    p.add_argument("--per-city-search", type=int, default=60,
                   help="max results to scan per search query (Google caps this at ~60)")
    p.add_argument("--city-give-up", type=int, default=60,
                   help="abandon a city after this many consecutive candidates yield no "
                        "fresh emailable lead (dups/no-email/already-contacted). Stops a "
                        "mined-out priority city from eating the whole run window so the "
                        "tail (coast, long-tail cities) still gets reached. 0 = never.")
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
                  coords: tuple[float, float],
                  do_translate: bool = True):
    """Assemble the restaurants INSERT payload (visible=false) from a place."""
    name = (place.get("displayName") or {}).get("text") or "Restaurant"
    types = place.get("types") or []
    cuisine_key, cuisine_slugs, tag_slugs = map_taxonomy(types, name)
    # Google explicitly says the venue has outdoor seating -> terrace tag.
    if place.get("outdoorSeating") and "terrace" not in tag_slugs:
        tag_slugs = tag_slugs + ["terrace"]
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
        # Coordinates come straight from Places details, so the map pin and
        # distance sorting work the moment the venue goes live — no
        # resolve-coords backfill needed for outreach imports.
        "lat": coords[0], "lng": coords[1],
        "neighborhood": hood,
        "city": city_slug,
        "visible": False,
        "current_capacity": "GREEN", "capacity_seats": 40,
        "currently_occupied_tables": 0,
        "opens_at": opens_at, "closes_at": closes_at,
        # `email` is the manager LOGIN identifier (unique, hidden from anon reads);
        # `contact_email` is the outreach/provenance copy. We set both to the same
        # crawled address so the venue can sign in with the address we mailed.
        "email": email,
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
    if args.cities.strip().lower() == "all":
        cities = list(CITY_CONFIG)
    else:
        cities = [c.strip() for c in args.cities.split(",") if c.strip()]
        unknown = [c for c in cities if c not in CITY_CONFIG]
        if unknown:
            print(f"WARNING: skipping unsupported city slugs (not in cities.ts): {unknown}")
            cities = [c for c in cities if c in CITY_CONFIG]
    if not cities:
        print("No valid cities to process.")
        return 1
    # Priority cities run first; the rest keep CITY_CONFIG's population order.
    # Quota a city can't fill rolls forward to the cities after it.
    cities = ([c for c in PRIORITY_CITIES if c in cities]
              + [c for c in cities if c not in PRIORITY_CITIES])

    public_base = os.environ.get("PUBLIC_BASE_URL", "https://restonow.app").rstrip("/")

    store = None
    if not args.dry_run:
        from db import Store
        store = Store()

    stats = {"discovered": 0, "retail_skipped": 0, "skipped_dup": 0,
             "geo_skipped": 0, "no_email": 0, "skipped_emailed": 0,
             "inserted": 0, "sent": 0, "failed": 0}
    target = args.max
    remaining = target
    seen: set[str] = set()  # place ids handled this run (dedup across query angles)
    print(f"TARGET: up to {target} emails this run · cities={cities}")

    quotas = allocate_quotas(cities, target)
    carry = 0  # unused quota rolled forward from under-filled cities
    print(f"QUOTAS: {quotas}")

    for city_slug in cities:
        if remaining <= 0:
            break
        city_name, city_term = CITY_CONFIG[city_slug]
        queries = build_queries(city_slug, city_term)
        city_budget = min(remaining, quotas[city_slug] + carry)
        print(f"\n=== {city_name} ({city_slug}) — {len(queries)} search angles, "
              f"city budget {city_budget} ({remaining} of {target} still to send) ===")

        # Early-exit guard: `barren` counts consecutive *in-city* candidates that
        # were dead ends — already imported, no reachable email, or already
        # emailed — the signals that a city's fresh-lead pool is mined out. It
        # resets the moment a real lead is found. Once it crosses --city-give-up
        # we abandon the city (across all its remaining angles) so a mined-out
        # city can't eat the whole run window. Deliberately does NOT count
        # geo-skips (wrong-city search drift) or retail/transient skips: those say
        # nothing about this city's pool, and for a small resort next to a big
        # city they'd otherwise trip the guard before its few real venues appear.
        # Persists across the city's query angles — an exhausted city stays so.
        barren = 0
        city_exhausted = False

        for query, included_type in queries:
            if remaining <= 0 or city_budget <= 0 or city_exhausted:
                break
            try:
                candidates = places.search_text(query, max_results=args.per_city_search,
                                                included_type=included_type)
            except places.PlacesError as e:
                print(f"  ! search failed [{query}]: {e}")
                continue
            # Drop supermarkets / hypermarkets / malls up front, on the cheap
            # search fields (name + types), so we never spend a details call on
            # them. seen-tag the excluded ids so a later angle doesn't re-check.
            fresh = []
            for c in candidates:
                cid = c.get("id")
                if not cid or cid in seen:
                    continue
                excluded, why = filters.is_excluded(filters.candidate_name(c), c.get("types"))
                if excluded:
                    seen.add(cid)
                    stats["retail_skipped"] += 1
                    continue
                fresh.append(cid)
            print(f"  [{query}] {len(candidates)} found, {len(fresh)} new "
                  f"(city budget {city_budget})")

            for place_id in fresh:
                if remaining <= 0 or city_budget <= 0:
                    break
                if args.city_give_up and barren >= args.city_give_up:
                    print(f"  · {city_name}: no fresh lead in last {barren} candidates "
                          f"— city looks mined out, moving on")
                    city_exhausted = True
                    break
                if place_id in seen:
                    continue
                seen.add(place_id)
                stats["discovered"] += 1

                # 3. dedup on place id (skip the details call entirely when known)
                if store and store.place_exists(place_id):
                    stats["skipped_dup"] += 1
                    barren += 1  # in-city venue already imported — exhaustion signal
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

                # Re-run the retail filter on the FULL details types (richer than
                # the search candidate's) — catches a supermarket/mall whose
                # search-stage types were too sparse to flag.
                excluded, why = filters.is_excluded(name, place.get("types"))
                if excluded:
                    stats["retail_skipped"] += 1
                    print(f"  - {name}: retail/mall — {why} (skip)")
                    continue

                # 3b. geo guard — text search is fuzzy and drifts across cities,
                #     so a venue only imports if its coordinates say it really
                #     is in the city we searched (nearest centroid + radius).
                #     No coordinates = can't verify = skip.
                coords = places.location(place)
                if not coords:
                    stats["geo_skipped"] += 1
                    print(f"  - {name}: no coordinates from Places (skip)")
                    continue
                geo_ok, geo_reason = geo.check_city(coords[0], coords[1], city_slug)
                if not geo_ok:
                    stats["geo_skipped"] += 1
                    print(f"  - {name}: wrong city — {geo_reason} (skip)")
                    continue

                # 4. email is required
                email = email_crawler.find(place.get("websiteUri"))
                if not email:
                    stats["no_email"] += 1
                    barren += 1  # in-city venue, no reachable email — exhaustion signal
                    print(f"  - {name}: no email (skip)")
                    if store:
                        store.log_outreach(email="unknown@" + (place.get("websiteUri") or "no-site"),
                                           status="no_email", city=city_slug)
                    continue

                # 5. dedup on already-emailed
                if store and store.email_already_contacted(email):
                    stats["skipped_emailed"] += 1
                    barren += 1  # in-city venue already emailed — exhaustion signal
                    print(f"  - {name}: {email} already contacted (skip)")
                    continue

                payload = build_payload(place, city_slug, city_name, email, coords,
                                        do_translate=not args.dry_run)
                go_live_url = None

                if args.dry_run:
                    print(f"  ~ DRY {name} <{email}> [{','.join(payload['cuisine_slugs'])}"
                          f" | {','.join(payload['tag_slugs'])}] phone={payload['phone']}"
                          f" geo={payload['lat']:.4f},{payload['lng']:.4f} ({geo_reason})")
                    remaining -= 1
                    city_budget -= 1
                    barren = 0  # fresh emailable lead — city is still productive
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

                # 9b. Provision the manager login now (venue is hidden, so the Bun
                #     provisioner won't), so we can put real credentials in the
                #     email. Done only once we know we'll actually send, to avoid
                #     orphan accounts. Failure here is non-fatal: we still send the
                #     go-live email, just without the login block.
                login_email = login_password = None
                try:
                    login_email, login_password = store.provision_manager(rid, email)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! manager provisioning failed for {name}: {e}")
                login_url = f"{public_base}/signin"

                try:
                    import brevo
                    msg_id = brevo.send_ro(
                        email, payload["name"], go_live_url,
                        login_url=login_url,
                        login_email=login_email,
                        login_password=login_password,
                    )
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
                city_budget -= 1
                barren = 0  # found + processed a fresh emailable lead

        # Whatever this city couldn't fill rolls forward to the next one.
        carry = max(0, city_budget)

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
