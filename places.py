#!/usr/bin/env python3
"""
Google Places API (New) client — text search, place details, and photo bytes.

Docs: https://developers.google.com/maps/documentation/places/web-service
Auth is the API key in the `X-Goog-Api-Key` header. Every request sends a
`X-Goog-FieldMask` so we are billed only for the fields we ask for.

Note: Places returns photos, website, phone, types, hours — but NEVER an email.
Email is sourced separately by email_crawler.py from the venue's website.
"""

from __future__ import annotations

import os
import time
import requests

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
PHOTO_URL = "https://places.googleapis.com/v1/{photo_name}/media"

# Fields requested for details. Trimmed to what the pipeline actually writes.
DETAILS_FIELDS = ",".join([
    "id", "displayName", "formattedAddress", "addressComponents",
    "location", "types", "primaryType", "nationalPhoneNumber",
    "internationalPhoneNumber", "websiteUri", "editorialSummary",
    "regularOpeningHours", "photos", "rating", "userRatingCount",
    "googleMapsUri", "businessStatus",
])

# Search only needs ids + a couple of cheap fields to pre-filter.
SEARCH_FIELDS = ",".join([
    "places.id", "places.displayName", "places.types",
    "places.businessStatus", "nextPageToken",
])


class PlacesError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise PlacesError("GOOGLE_MAPS_API_KEY is not set")
    return key


def search_text(query: str, max_results: int = 60, page_size: int = 20,
                region: str = "ro"):
    """
    Run a Places text search, paginating until max_results or no nextPageToken.
    Returns a list of place dicts ({id, displayName, types, businessStatus}).
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": SEARCH_FIELDS,
    }
    out = []
    page_token = None
    while len(out) < max_results:
        body = {
            "textQuery": query,
            "pageSize": min(page_size, 20),
            "regionCode": region.upper(),
            "includedType": "restaurant",
        }
        if page_token:
            body["pageToken"] = page_token
        resp = requests.post(SEARCH_URL, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            raise PlacesError(f"searchText {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        out.extend(data.get("places", []) or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        # The new page token needs a beat before it is valid.
        time.sleep(2)
    return out[:max_results]


def details(place_id: str, language_code: str = "ro"):
    """
    Fetch full details for one place id. Returns the place dict or None.

    `language_code` localizes Google's text fields (notably editorialSummary) —
    we ask for Romanian so the venue's own description comes back in RO when
    Google has it. The response still carries editorialSummary.languageCode so
    the caller knows which language it actually got.
    """
    headers = {
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": DETAILS_FIELDS,
    }
    url = DETAILS_URL.format(place_id=place_id)
    resp = requests.get(url, headers=headers, params={"languageCode": language_code}, timeout=30)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise PlacesError(f"details {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def photo_bytes(photo_name: str, max_width: int = 1200):
    """
    Download the bytes for a Places photo resource. `photo_name` is the
    `photos[].name` from a details response (e.g. "places/ABC/photos/XYZ").
    Returns raw image bytes, or None on failure.
    """
    headers = {"X-Goog-Api-Key": _api_key()}
    params = {"maxWidthPx": max_width, "skipHttpRedirect": "false"}
    url = PHOTO_URL.format(photo_name=photo_name)
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except requests.RequestException:
        pass
    return None


def opening_hours(place: dict):
    """
    Best-effort (opens_at, closes_at) as 'HH:MM' from regularOpeningHours.
    Falls back to the platform defaults ('11:00', '23:00') when absent.
    """
    periods = (place.get("regularOpeningHours") or {}).get("periods") or []
    for p in periods:
        o = p.get("open") or {}
        c = p.get("close") or {}
        if "hour" in o and "hour" in c:
            opens = f"{o['hour']:02d}:{o.get('minute', 0):02d}"
            closes = f"{c['hour']:02d}:{c.get('minute', 0):02d}"
            return opens, closes
    return "11:00", "23:00"


def location(place: dict) -> tuple[float, float] | None:
    """(lat, lng) from the details response's `location`, or None if absent."""
    loc = place.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


def neighborhood(place: dict) -> str | None:
    """
    Pull a neighborhood/sublocality from addressComponents, else the locality.
    Free-text field on the platform — best effort.
    """
    comps = place.get("addressComponents") or []
    by_type = {}
    for c in comps:
        for t in c.get("types", []):
            by_type.setdefault(t, c.get("longText") or c.get("shortText"))
    for t in ("sublocality_level_1", "sublocality", "neighborhood", "locality"):
        if by_type.get(t):
            return by_type[t]
    return None
