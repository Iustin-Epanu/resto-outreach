#!/usr/bin/env python3
"""
DIY email discovery — Google Places never returns an email, so we crawl the
venue's own website. Strategy (cheapest-first, stop on first hit):

  1. Fetch the homepage; collect `mailto:` hrefs (highest confidence), then
     plain-text email matches.
  2. If none, try a few common contact paths (/contact, /contacte, ...).
  3. Filter out junk (asset filenames, tracking/CDN domains, sentry, etc.).

Pure-stdlib parsing + `requests`. Short timeouts and a single retry so one slow
or broken site never stalls the whole run.
"""

from __future__ import annotations

import re
import html
from urllib.parse import urljoin, urlparse

import requests

UA = (
    "Mozilla/5.0 (compatible; RestoOutreachBot/1.0; +https://restonow.app) "
    "Python-requests"
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r'mailto:([^"\'?>\s]+)', re.IGNORECASE)

# Paths to try if the homepage has no email. Romanian + English variants.
CONTACT_PATHS = ["", "contact", "contacte", "contact-us", "kontakt",
                 "despre", "about", "rezervari", "reservations"]

# Domains/substrings that are never a real contact address.
JUNK_DOMAINS = (
    "sentry.io", "sentry-cdn", "wixpress.com", "example.com", "example.org",
    "domain.com", "email.com", "yourdomain", "sentry.wixpress", "schema.org",
    "googleapis.com", "gstatic.com", "cloudflare", "w3.org", "jquery",
    "fontawesome", "googletagmanager", "google-analytics",
)
# Asset-extension giveaways (e.g. logo@2x.png matched as an email).
JUNK_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css",
               ".js", ".ico", ".woff", ".woff2", ".mp4", ".pdf")


def _clean_candidate(raw: str) -> str | None:
    e = html.unescape(raw).strip().strip(".,;:<>()[]\"'").lower()
    # Strip a leading "mailto:" if it slipped through.
    if e.startswith("mailto:"):
        e = e[len("mailto:"):]
    # Drop query/extra after the address.
    e = e.split("?")[0]
    if not EMAIL_RE.fullmatch(e):
        return None
    low = e.lower()
    if any(j in low for j in JUNK_DOMAINS):
        return None
    if low.endswith(JUNK_SUFFIX):
        return None
    # Retina/asset artifacts like "icon@2x" or pure-number localparts.
    local = low.split("@", 1)[0]
    if local.isdigit() or local.endswith("2x") or local.endswith("3x"):
        return None
    return e


def _emails_from_html(text: str) -> list[str]:
    found: list[str] = []
    # mailto: first — highest confidence.
    for m in MAILTO_RE.findall(text):
        c = _clean_candidate(m)
        if c and c not in found:
            found.append(c)
    # then plain-text matches.
    for m in EMAIL_RE.findall(text):
        c = _clean_candidate(m)
        if c and c not in found:
            found.append(c)
    return found


def _score(email: str, site_host: str) -> int:
    """Prefer same-domain + role addresses (contact@, office@, rezervari@)."""
    local, _, host = email.partition("@")
    s = 0
    if site_host and (host == site_host or host.endswith("." + site_host)
                      or site_host.endswith("." + host)):
        s += 10
    if local in ("contact", "office", "rezervari", "reservations", "hello",
                 "info", "booking", "rezervare"):
        s += 5
    if host.endswith(".ro"):
        s += 1
    # Deprioritise generic free-mail catch-alls slightly.
    if host in ("gmail.com", "yahoo.com", "yahoo.ro", "hotmail.com", "outlook.com"):
        s -= 1
    return s


def _fetch(url: str, timeout: float = 8.0) -> str | None:
    for attempt in range(2):
        try:
            resp = requests.get(
                url, headers={"User-Agent": UA}, timeout=timeout,
                allow_redirects=True,
            )
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and "html" in ctype.lower():
                return resp.text
            return None
        except requests.RequestException:
            if attempt == 0:
                continue
    return None


def find(website_url: str | None) -> str | None:
    """
    Return the best contact email for a venue website, or None if none found.
    """
    if not website_url:
        return None
    parsed = urlparse(website_url if "://" in website_url else "https://" + website_url)
    site_host = (parsed.netloc or "").lower().lstrip("www.")
    base = f"{parsed.scheme or 'https'}://{parsed.netloc}"

    candidates: list[str] = []
    for path in CONTACT_PATHS:
        url = base if path == "" else urljoin(base + "/", path)
        page = _fetch(url)
        if not page:
            continue
        for e in _emails_from_html(page):
            if e not in candidates:
                candidates.append(e)
        # Homepage hit on a strong (same-domain) address — good enough, stop.
        if path == "" and any(_score(e, site_host) >= 10 for e in candidates):
            break
        if candidates and path != "":
            break

    if not candidates:
        return None
    candidates.sort(key=lambda e: _score(e, site_host), reverse=True)
    return candidates[0]
