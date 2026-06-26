#!/usr/bin/env python3
"""
Brevo (ex-Sendinblue) transactional email sender.

POST https://api.brevo.com/v3/smtp/email with the `api-key` header. Includes a
List-Unsubscribe header + visible opt-out + sender postal identity in the
template footer (GDPR / ePrivacy hygiene for cold B2B outreach, and better
deliverability). Subject + body are Romanian.

Config via env:
  BREVO_API_KEY        (required)
  BREVO_SENDER_EMAIL   (required — must be a verified sender/domain in Brevo)
  BREVO_SENDER_NAME    (default "Resto")
  BREVO_UNSUBSCRIBE_URL (optional — a mailto: opt-out is used as fallback)
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

API_URL = "https://api.brevo.com/v3/smtp/email"
TEMPLATE = Path(__file__).resolve().parent / "email_templates" / "ro.html"

SUBJECT_RO = "{venue} e deja pe Resto — activează pagina ta (gratuit)"


class BrevoError(RuntimeError):
    pass


def _render(venue_name: str, go_live_url: str, sender_name: str,
            unsubscribe_url: str) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    return (html
            .replace("{{venue_name}}", venue_name)
            .replace("{{go_live_url}}", go_live_url)
            .replace("{{sender_name}}", sender_name)
            .replace("{{unsubscribe_url}}", unsubscribe_url))


def send_ro(to_email: str, venue_name: str, go_live_url: str) -> str:
    """
    Send the Romanian outreach email. Returns the Brevo messageId.
    Raises BrevoError on a non-2xx response.
    """
    api_key = os.environ.get("BREVO_API_KEY")
    sender_email = os.environ.get("BREVO_SENDER_EMAIL")
    sender_name = os.environ.get("BREVO_SENDER_NAME", "Resto")
    if not api_key or not sender_email:
        raise BrevoError("BREVO_API_KEY and BREVO_SENDER_EMAIL must be set")

    unsubscribe_url = os.environ.get(
        "BREVO_UNSUBSCRIBE_URL",
        f"mailto:{sender_email}?subject=Dezabonare",
    )
    html = _render(venue_name, go_live_url, sender_name, unsubscribe_url)

    payload = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": [{"email": to_email, "name": venue_name}],
        "subject": SUBJECT_RO.format(venue=venue_name),
        "htmlContent": html,
        # Surfaces an unsubscribe affordance to mailbox providers.
        "headers": {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        "tags": ["outreach", "ro"],
    }
    resp = requests.post(
        API_URL,
        headers={"api-key": api_key, "Content-Type": "application/json",
                 "accept": "application/json"},
        json=payload, timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise BrevoError(f"brevo {resp.status_code}: {resp.text[:300]}")
    return resp.json().get("messageId", "")
