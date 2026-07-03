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
