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
import re
from html import escape
from pathlib import Path

import requests

API_URL = "https://api.brevo.com/v3/smtp/email"
TEMPLATE = Path(__file__).resolve().parent / "email_templates" / "ro.html"
FOLLOWUP_TEMPLATE = Path(__file__).resolve().parent / "email_templates" / "ro_followup.html"

SUBJECT_RO = "{venue} e deja pe Resto — activează pagina ta (gratuit)"
SUBJECT_FOLLOWUP_RO = "{venue}: pasul următor — arată câte mese ai libere ACUM"


class BrevoError(RuntimeError):
    pass


def _credentials_block(login_url: str, login_email: str, login_password: str) -> str:
    """
    The manager-login card, with the founder-specified Romanian sentence (full
    diacritics). Returns ready-to-inline HTML. Values are HTML-escaped because a
    venue email/password could in theory contain `<`/`&`.
    """
    url = escape(login_url, quote=True)
    email = escape(login_email)
    pwd = escape(login_password)
    mono = "font-weight:700;font-family:Menlo,Consolas,monospace;color:#1a1a1a;"
    return f"""
            <tr>
              <td style="padding:0 32px 24px 32px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                       style="background:#f4f8ff;border-radius:12px;border-left:4px solid #2e63e2;">
                  <tr>
                    <td style="padding:16px 20px;">
                      <p style="margin:0;font-size:14px;line-height:1.7;color:#333;">
                        Vă puteți modifica pagina accesând următorul link:
                        <a href="{url}" style="color:#2e63e2;font-weight:600;word-break:break-all;">{url}</a>
                        utilizând credențialele de mai jos.
                      </p>
                      <table role="presentation" cellpadding="0" cellspacing="0" style="margin:12px 0 0 0;">
                        <tr>
                          <td style="padding:2px 12px 2px 0;font-size:12px;color:#888;">Email</td>
                          <td style="padding:2px 0;font-size:14px;{mono}">{email}</td>
                        </tr>
                        <tr>
                          <td style="padding:2px 12px 2px 0;font-size:12px;color:#888;">Parolă</td>
                          <td style="padding:2px 0;font-size:14px;{mono}">{pwd}</td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>"""


def _render(venue_name: str, go_live_url: str, sender_name: str,
            unsubscribe_url: str, credentials_block: str) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    # Strip the marker comments, then drop the block entirely if we have no
    # credentials to show (provisioning failed upstream).
    html = (html
            .replace("<!--CREDS_START-->", "")
            .replace("<!--CREDS_END-->", ""))
    html = re.sub(r"\{\{credentials_block\}\}", lambda _m: credentials_block, html)
    return (html
            .replace("{{venue_name}}", venue_name)
            .replace("{{go_live_url}}", go_live_url)
            .replace("{{sender_name}}", sender_name)
            .replace("{{unsubscribe_url}}", unsubscribe_url))


def send_ro(to_email: str, venue_name: str, go_live_url: str,
            login_url: str | None = None, login_email: str | None = None,
            login_password: str | None = None) -> str:
    """
    Send the Romanian outreach email. Returns the Brevo messageId.
    Raises BrevoError on a non-2xx response.

    When login_url + login_email + login_password are all present, the email
    includes a manager-login card with the recipient's real credentials.
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
    credentials_block = ""
    if login_url and login_email and login_password:
        credentials_block = _credentials_block(login_url, login_email, login_password)
    html = _render(venue_name, go_live_url, sender_name, unsubscribe_url,
                   credentials_block)

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


def _render_followup(venue_name: str, login_url: str, sender_name: str,
                     unsubscribe_url: str, credentials_block: str) -> str:
    html = FOLLOWUP_TEMPLATE.read_text(encoding="utf-8")
    html = (html
            .replace("<!--CREDS_START-->", "")
            .replace("<!--CREDS_END-->", ""))
    html = re.sub(r"\{\{credentials_block\}\}", lambda _m: credentials_block, html)
    return (html
            .replace("{{venue_name}}", venue_name)
            .replace("{{login_url}}", login_url)
            .replace("{{sender_name}}", sender_name)
            .replace("{{unsubscribe_url}}", unsubscribe_url))


def send_followup_ro(to_email: str, venue_name: str, login_url: str,
                     login_email: str | None = None,
                     login_password: str | None = None) -> str:
    """
    Send the Romanian "connect your POS" follow-up. Returns the Brevo messageId.
    Raises BrevoError on a non-2xx response.

    Sent ~3 days after a venue goes live (see followup.py). When login_email +
    login_password are present, the email includes the manager-login card so the
    recipient can jump straight in and connect their POS.
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
    credentials_block = ""
    if login_url and login_email and login_password:
        credentials_block = _credentials_block(login_url, login_email, login_password)
    html = _render_followup(venue_name, login_url, sender_name, unsubscribe_url,
                            credentials_block)

    payload = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": [{"email": to_email, "name": venue_name}],
        "subject": SUBJECT_FOLLOWUP_RO.format(venue=venue_name),
        "htmlContent": html,
        "headers": {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        "tags": ["followup", "pos", "ro"],
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
