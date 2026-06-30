#!/usr/bin/env python3
"""
Supabase data layer for the outreach pipeline. Uses the SERVICE-ROLE key, which
bypasses RLS. Keep this server-side only — the key is a database root password.

Responsibilities:
  - dedup guards (place id + already-emailed)
  - insert a hidden restaurant row (a DB trigger then mints a go-live link)
  - upload Google photos into the `restaurant-images` bucket
  - read back the go-live token
  - write the outreach_log
"""

from __future__ import annotations

import os
import re
import secrets
import time

from supabase import create_client, Client

BUCKET = "restaurant-images"

# Unambiguous alphabet (no 0/O/1/l/I) — mirrors server/provisionManagers.ts so a
# manager password generated here is indistinguishable from one the web app's
# provisioner would mint. 16 chars ≈ 92 bits of entropy.
PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


def _generate_password(length: int = 16) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _sanitize_filename(name: str) -> str:
    """Mirror the web app's storage sanitizeFilename."""
    name = (name or "photo.jpg").lower()
    name = re.sub(r"[^a-z0-9.\-]+", "-", name).strip("-")
    return name or "photo.jpg"


class Store:
    def __init__(self):
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
        self.client: Client = create_client(url, key)

    # --- dedup guards ----------------------------------------------------- #
    def place_exists(self, google_place_id: str) -> bool:
        res = (self.client.table("restaurants")
               .select("id")
               .eq("google_place_id", google_place_id)
               .limit(1)
               .execute())
        return bool(res.data)

    def email_already_contacted(self, email: str) -> bool:
        res = (self.client.table("outreach_log")
               .select("id")
               .eq("email", email.lower())
               .eq("status", "sent")
               .limit(1)
               .execute())
        return bool(res.data)

    # --- writes ----------------------------------------------------------- #
    def insert_restaurant(self, payload: dict) -> str:
        """Insert a restaurant row (caller sets visible=false). Returns its id."""
        res = self.client.table("restaurants").insert(payload).execute()
        if not res.data:
            raise RuntimeError("insert_restaurant returned no row")
        return res.data[0]["id"]

    def upload_image(self, restaurant_id: str, data: bytes,
                     filename: str = "photo.jpg") -> str | None:
        """
        Upload image bytes to {restaurant_id}/{ts}-{name} and return the public
        URL. Best-effort — never lets a bad upload abort the run.
        """
        path = f"{restaurant_id}/{int(time.time() * 1000)}-{_sanitize_filename(filename)}"
        try:
            self.client.storage.from_(BUCKET).upload(
                path, data,
                {"content-type": "image/jpeg", "cache-control": "31536000"},
            )
            return self.client.storage.from_(BUCKET).get_public_url(path)
        except Exception as e:  # noqa: BLE001
            print(f"    ! image upload failed: {e}")
            return None

    def update_images(self, restaurant_id: str, hero: str, gallery: list[str]) -> None:
        self.client.table("restaurants").update(
            {"image_url": hero, "gallery_image_urls": gallery}
        ).eq("id", restaurant_id).execute()

    def get_golive_token(self, restaurant_id: str) -> str | None:
        """Read the token minted by the DB trigger for a hidden venue."""
        res = (self.client.table("golive_links")
               .select("token")
               .eq("restaurant_id", restaurant_id)
               .limit(1)
               .execute())
        return res.data[0]["token"] if res.data else None

    # --- manager account provisioning ------------------------------------ #
    def provision_manager(self, restaurant_id: str, email: str) -> tuple[str, str]:
        """
        Create (or reuse) the restaurant-manager login for a venue and return
        (email, password) so the caller can put the credentials in the outreach
        email. Mirrors web/server/provisionManagers.ts, but runs at outreach time
        because the Bun provisioner only acts on *live* venues — ours are hidden
        until the recipient redeems the go-live link.

        Steps (all service-role, RLS-bypassing):
          1. short-circuit if a credential already exists (idempotent re-runs),
          2. create the Supabase Auth user (email + generated password),
          3. upsert the platform `users` row (role 'manager', pinned to venue),
          4. store the plaintext password in `manager_credentials`.

        Raises on any unrecoverable failure so the caller can log it and still
        decide whether to send.
        """
        email = email.strip().lower()

        existing = (self.client.table("manager_credentials")
                    .select("email, password")
                    .eq("restaurant_id", restaurant_id)
                    .limit(1)
                    .execute())
        if existing.data:
            row = existing.data[0]
            return row["email"], row["password"]

        password = _generate_password()

        # 2. Auth user. On a duplicate-email error (a previous partial run, or a
        #    venue that already has an account), recover by reusing the existing
        #    platform user id and resetting its password so re-runs converge.
        user_id: str | None = None
        try:
            created = self.client.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True,
            })
            user_id = created.user.id if created and created.user else None
        except Exception:  # noqa: BLE001 — duplicate email / transient: try recovery
            existing_user = (self.client.table("users")
                             .select("id")
                             .eq("email", email)
                             .limit(1)
                             .execute())
            if not existing_user.data:
                raise
            user_id = existing_user.data[0]["id"]
            self.client.auth.admin.update_user_by_id(user_id, {"password": password})
        if not user_id:
            raise RuntimeError("provision_manager: no auth user id")

        # 3. Platform users row — role 'manager', pinned to this venue (RLS scope).
        self.client.table("users").upsert(
            {"id": user_id, "restaurant_id": restaurant_id,
             "role": "manager", "email": email},
            on_conflict="id",
        ).execute()

        # 4. Revealable credential (plaintext, like go-live tokens).
        self.client.table("manager_credentials").upsert(
            {"restaurant_id": restaurant_id, "email": email, "password": password},
            on_conflict="restaurant_id",
        ).execute()

        return email, password

    def log_outreach(self, *, email: str, status: str, restaurant_id: str | None = None,
                     brevo_message_id: str | None = None, error: str | None = None,
                     city: str | None = None) -> None:
        self.client.table("outreach_log").insert({
            "email": email.lower(),
            "status": status,
            "restaurant_id": restaurant_id,
            "brevo_message_id": brevo_message_id,
            "error": error,
            "city": city,
        }).execute()
