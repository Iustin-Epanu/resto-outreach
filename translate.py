#!/usr/bin/env python3
"""
Tiny Google Cloud Translation (v2) client — used to mirror a venue's real Google
Maps description across RO/EN instead of falling back to generic copy.

Auth is an API key passed as ?key=. It tries GOOGLE_TRANSLATE_API_KEY first, then
falls back to GOOGLE_MAPS_API_KEY (works if the same GCP project has the
Cloud Translation API enabled and the key isn't API-restricted).

Best-effort by design: any failure (no key, API disabled, network) returns None
so the caller can fall back to the generic template rather than abort the run.
"""

from __future__ import annotations

import os

import requests

TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"


def _key() -> str | None:
    return os.environ.get("GOOGLE_TRANSLATE_API_KEY") or os.environ.get("GOOGLE_MAPS_API_KEY")


def translate(text: str, target: str, source: str | None = None) -> str | None:
    """
    Translate `text` into `target` (e.g. 'en'/'ro'). `source` is optional; when
    omitted Google auto-detects. Returns the translated string, or None on any
    failure (caller falls back to generic copy).
    """
    key = _key()
    if not text or not key:
        return None
    params = {"key": key, "q": text, "target": target, "format": "text"}
    if source:
        params["source"] = source
    try:
        resp = requests.post(TRANSLATE_URL, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"    ! translate {resp.status_code}: {resp.text[:160]}")
            return None
        translations = resp.json().get("data", {}).get("translations", [])
        return translations[0]["translatedText"] if translations else None
    except Exception as e:  # noqa: BLE001
        print(f"    ! translate error: {e}")
        return None
