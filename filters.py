#!/usr/bin/env python3
"""
Exclusion filter: keep supermarkets, hypermarkets and shopping malls out of the
outreach pipeline. Resto sells to sit-down/quick-service F&B venues — a Kaufland
or a mall building is never a customer, and emailing one burns a send + pollutes
the feed.

Two signals, checked cheaply on the search candidate BEFORE the paid details
call, then again on the full details payload:

  1. Google Places `types[]` — the reliable, structural signal. Google types
     Auchan/Kaufland/Lidl as `supermarket`/`grocery_store` and the mall building
     itself as `shopping_mall`/`department_store`. A real restaurant is never
     typed this way, so ANY excluded type => drop. This is the primary filter.

  2. Name keywords — a narrow backstop for the rare case where the retail type
     is missing (Google occasionally returns a supermarket with only `store`, or
     a food court POI mislabeled `restaurant`). Deliberately HIGH-PRECISION:
     brand names + "supermarket"/"hypermarket", matched on word boundaries so a
     food-court tenant is never caught. We do NOT keyword on "mall"/"promenada"/
     "iulius" — those appear in legitimate tenant names (e.g. "Teo's Cafe —
     Iulius Mall"); the mall *building* is removed by the type filter instead.
"""

from __future__ import annotations

import re

# Google Places (New) types that mean "retail / not a food-service venue".
# ANY of these on a place drops it. `restaurant`, `cafe`, `bar`, `bakery`,
# `meal_takeaway`, `coffee_shop` etc. are intentionally absent.
EXCLUDED_TYPES = {
    "supermarket",
    "grocery_store",
    "grocery_or_supermarket",
    "shopping_mall",
    "department_store",
    "discount_store",
    "warehouse_store",
    "wholesaler",
    "home_goods_store",
    "hardware_store",
    "furniture_store",
    "electronics_store",
    "convenience_store",
    "market",  # generic market / farmers market — not a restaurant
}

# High-precision name backstop. Brand chains + generic big-box words only.
# Word boundaries keep "MyAuchan" coffee kiosks and "Pizzeria Cora" out of the
# net — those match neither \bauchan\b (no boundary inside "MyAuchan") nor a bare
# brand token. Multi-word brands ("mega image", "penny market") are safe as-is.
_NAME_BLOCK = re.compile(
    r"\b("
    r"auchan|kaufland|lidl|carrefour|selgros|"
    r"mega\s?image|penny\s?market|profi\s+(super|market)|"
    r"dedeman|hornbach|leroy\s?merlin|brico\s?(depot|dépôt)|ikea|"
    r"hypermarket|hipermarket|supermarket|supermarché|"
    r"cash\s*(&|and)\s*carry"
    r")\b",
    re.IGNORECASE,
)


def is_excluded(name: str | None, types) -> tuple[bool, str | None]:
    """
    Should this venue be skipped as retail (supermarket / hypermarket / mall)?

    Returns (excluded, reason). `types` is the Places `types[]` list from either
    a search candidate or a details payload; `name` is the displayName text.
    """
    tset = {t for t in (types or [])}
    bad = tset & EXCLUDED_TYPES
    if bad:
        return True, f"excluded type {sorted(bad)}"
    if name:
        m = _NAME_BLOCK.search(name)
        if m:
            return True, f"excluded name keyword {m.group(0)!r}"
    return False, None


def candidate_name(candidate: dict) -> str | None:
    """displayName.text from a Places search candidate or details dict."""
    return (candidate.get("displayName") or {}).get("text")


# --------------------------------------------------------------------------- #
# Accommodation filter                                                         #
# --------------------------------------------------------------------------- #
# Resto is a *walk-in dining* signal — hotels, pensiuni, vile, hostels and
# campings are not venues we ever want, even when they run a restaurant/terrace.
# On the litoral this is the single biggest source of junk: half the "restaurant
# with terrace" results in a resort town are really an accommodation's terrace.
#
# No single signal is reliable on its own (a pensiune's terrace can be typed
# `restaurant`; a real restaurant can sit next to a hotel), so we combine four:
#   1. Places type — `primaryType` in the lodging family is decisive; a lodging
#      type only in the secondary `types[]` counts unless the primary is clearly
#      food (protects a real restaurant that carries a stray `lodging` tag).
#   2. Name keywords — hotel / motel / pensiune / camping / cabană / … are
#      decisive; vilă / resort are "soft" (too common on real restaurants) and
#      only bite when a second signal (below) confirms lodging.
#   3. The crawled contact email — localpart or domain containing hotel /
#      pension / cazare / recept(ie) / … (catches "hotel@x.ro",
#      "reception@x.ro", "amphorahotel@gmail.com" that have no other tell).
#   4. Google's own description — corroborating "cazare / rooms / overnight / …".

ACCOMMODATION_TYPES = {
    "lodging", "hotel", "motel", "resort_hotel", "extended_stay_hotel",
    "bed_and_breakfast", "guest_house", "hostel", "inn", "cottage",
    "campground", "camping_cabin", "rv_park", "farmstay", "japanese_inn",
    "budget_japanese_inn", "private_guest_room", "mobile_home_park",
    "apartment_complex", "apartment_building",
}

# Food-service primary types — if a place is PRIMARILY one of these we don't let
# a stray secondary `lodging` tag alone exclude it (needs a name/email/desc tell).
_FOOD_PRIMARY_TYPES = {
    "restaurant", "cafe", "coffee_shop", "bakery", "bar", "pub", "wine_bar",
    "bar_and_grill", "meal_takeaway", "meal_delivery", "fast_food_restaurant",
    "ice_cream_shop", "dessert_shop", "food_court", "fine_dining_restaurant",
    "cafeteria", "deli", "diner", "steak_house", "brunch_restaurant",
    "breakfast_restaurant",
}


def _is_food_primary(primary_type: str) -> bool:
    pt = (primary_type or "").lower()
    return pt in _FOOD_PRIMARY_TYPES or pt.endswith("_restaurant")


# Decisive accommodation name keywords (RO + EN). Leading \b + prefix match so
# "hotelul"/"pensiunea"/"campingul" are caught. `casa`/`complex` are deliberately
# absent — too many real restaurants use them. `vila`/`resort` are NOT here
# either: they're too common in restaurant names, so they're demoted to the soft
# set below and only bite with a second, lodging-confirming signal.
_ACCOM_NAME = re.compile(
    r"\b("
    r"hotel\w*|motel\w*|hostel\w*|aparthotel\w*|apart[-\s]hotel|"
    r"pensiun\w*|"                                  # pensiune / pensiunea / pensiuni
    r"guest[-\s]?house|b&b|bed\s+and\s+breakfast|"
    r"camping\w*|caban[aăe]\w*|bungalow\w*|"
    r"casa\s+de\s+vacan[țt]\w*|"
    r"cazare|camere\s+de\s+(inchiriat|închiriat)"
    r")",
    re.IGNORECASE,
)

# Soft accommodation name words. Common on the coast but also on genuine
# restaurants ("Vila Rustica Restaurant", "X Beach Resort"), so a match alone is
# NOT enough — is_accommodation() excludes only when a SECOND signal confirms
# lodging: Google's own Places classification (a lodging type on the place), an
# accommodation email, or an accommodation description. vilă/villa are boundary-
# anchored so "Vilamar" doesn't trip.
_ACCOM_NAME_SOFT = re.compile(r"\b(vil[aă]\b|villa\b|resort\w*)", re.IGNORECASE)

# Substrings that, in an email localpart OR domain, mark an accommodation.
# `rezervari`/`reservations` are intentionally NOT here — restaurants use them.
_ACCOM_EMAIL_HINTS = (
    "hotel", "motel", "hostel", "pension", "pensiune", "cazare", "recept",
    "aparthotel", "camping", "guesthouse",
)

# Description phrases that, in the venue's OWN Google description, are decisive
# (a place that describes itself as offering lodging is an accommodation).
_ACCOM_DESC_STRONG = (
    "cazare", "camere de inchiriat", "camere de închiriat", "rooms available",
    "overnight stay", "bed and breakfast", "self-catering", "guest house",
    "guesthouse", "accommodation", "pensiune",
)
# Weaker phrases — only decisive when a lodging type is also present ("hotel"
# alone can mean "near the hotel"; "nights"/"check-in" are ambiguous).
_ACCOM_DESC_WEAK = ("hotel", "camere", "overnight", "nights", "check-in", "rooms")


def is_accommodation(name, types, primary_type=None, description=None,
                     email=None) -> tuple[bool, str | None]:
    """
    Is this venue really an accommodation (hotel/pensiune/vilă/hostel/camping),
    rather than a standalone restaurant/café/bar? Returns (excluded, reason).

    Pass whatever you have: at the search stage that's (name, types,
    primary_type); after details add `description`; after the email crawl add
    `email` — each extra signal catches cases the others miss.
    """
    tset = {t for t in (types or [])}
    pt = (primary_type or "").lower()
    accom_types = tset & ACCOMMODATION_TYPES     # Google-Maps lodging tell
    desc = (description or "").lower()

    # 1. Primary type is lodging -> decisive (Google classifies it as lodging).
    if pt in ACCOMMODATION_TYPES:
        return True, f"primary type {pt!r}"

    # 2. Decisive name keyword (hotel/pensiune/camping/…; NOT vila/resort).
    if name:
        m = _ACCOM_NAME.search(name)
        if m:
            return True, f"name keyword {m.group(0)!r}"

    # 3. Email localpart/domain tells (strong on the litoral).
    if email and "@" in email:
        local, _, dom = email.lower().partition("@")
        if any(h in local for h in _ACCOM_EMAIL_HINTS):
            return True, f"email localpart {local!r}"
        if any(h in dom for h in _ACCOM_EMAIL_HINTS):
            return True, f"email domain {dom!r}"

    # 4. The venue's own description says it offers lodging -> decisive.
    if desc:
        hit = next((h for h in _ACCOM_DESC_STRONG if h in desc), None)
        if hit:
            return True, f"description {hit!r}"

    # 5. Lodging in the secondary types and the place is not primarily food.
    if accom_types and not _is_food_primary(pt):
        return True, f"lodging type {sorted(accom_types)}"

    # 6. Soft name (vila/resort) — exclude ONLY with a second signal confirming
    #    it's really lodging, not a restaurant that happens to be named "Vila":
    #    a Google Places lodging type on the place, or an accommodation word in
    #    its own description. (An accommodation email already matched at step 3.)
    soft = _ACCOM_NAME_SOFT.search(name or "")
    if soft:
        if accom_types:
            return True, (f"soft name {soft.group(0)!r} + Google lodging type "
                          f"{sorted(accom_types)}")
        hit = next((h for h in (_ACCOM_DESC_STRONG + _ACCOM_DESC_WEAK) if h in desc), None)
        if hit:
            return True, f"soft name {soft.group(0)!r} + description {hit!r}"

    # 7. Corroboration: a lodging tag (even under a food primary) plus a weaker
    #    description tell.
    if accom_types and any(h in desc for h in _ACCOM_DESC_WEAK):
        return True, "lodging type + accommodation description"

    return False, None
