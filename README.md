# resto-outreach

Standalone automation that grows the Resto catalog and does cold outreach.

For each target city it: scrapes restaurants from **Google Places**, crawls each
venue's website for a **contact email**, imports the venue into the Resto
Supabase DB as a **hidden** row (a DB trigger then mints a one-time go-live
link), and **emails** the venue in Romanian via **Brevo** with that link as the
call-to-action. Runs weekly on GitHub Actions, or on manual dispatch.

> This repo is intentionally separate from the Resto web app. It talks to the
> same Supabase project but ships no web code. The web app is hosted elsewhere.

## Pipeline

```
search (Places) → details → dedup(place_id) → crawl website for EMAIL
  → dedup(already-emailed) → map taxonomy + copy → insert hidden restaurant
  → upload Google photos    → read go-live token → Brevo send (Romanian) → log
```

Email is **required** — a venue with no discoverable email is skipped (logged
`no_email`), never imported.

## Files

| File | Role |
|------|------|
| `run.py` | Orchestrator / entrypoint (`--cities`, `--max`, `--dry-run`) |
| `places.py` | Google Places API (New): search, details, photos, hours |
| `email_crawler.py` | Crawl the venue website for a contact email |
| `taxonomy_map.py` | Map Places `types[]` → Resto cuisine/tag slugs |
| `inference.py` | Cuisine keyword fallback, bilingual copy, fallback imagery |
| `db.py` | Supabase service-role layer (insert, storage, go-live token, log) |
| `brevo.py` + `email_templates/ro.html` | Romanian email (placeholder copy) |
| `migrations/2026-06-26-outreach.sql` | One-time DB setup (run in Supabase) |

## One-time setup

1. **DB migration** — run `migrations/2026-06-26-outreach.sql` in the Supabase
   SQL editor of the Resto project. Adds `contact_email/phone/website/
   google_place_id` to `restaurants` + the `outreach_log` table.
2. **Google Maps API key** — enable *Places API (New)* in Google Cloud; restrict
   the key to that API.
3. **Brevo** — verify a sender domain (SPF/DKIM) before any real send, or
   deliverability/spam-flagging will suffer.
4. **GitHub repo secrets** (Settings → Secrets and variables → Actions):
   `GOOGLE_MAPS_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `PUBLIC_BASE_URL` (the consumer site,
   e.g. `https://restonow.app`). Optional: `BREVO_UNSUBSCRIBE_URL`.

Once pushed, the workflow appears under the repo's **Actions** tab. Trigger it
manually (with `dry_run=true` first) or wait for the Monday cron.

## Run locally (dry run — no writes, no sends)

```bash
pip install -r requirements.txt
export GOOGLE_MAPS_API_KEY=...           # dry run needs only this
python run.py --cities cluj-napoca --max 3 --dry-run
```

A live single-venue smoke test additionally needs the Supabase + Brevo env vars:

```bash
export SUPABASE_URL=...  SUPABASE_SERVICE_ROLE_KEY=...
export BREVO_API_KEY=... BREVO_SENDER_EMAIL=... PUBLIC_BASE_URL=https://restonow.app
python run.py --cities cluj-napoca --max 1
```

## Notes

- City slugs must match the web app's `src/lib/cities.ts` (`cluj-napoca`,
  `bucuresti`, `iasi`, `botosani`) — the consumer app gates browsing on them.
  Unsupported slugs are warned and skipped.
- `--max` caps Places Details calls and Brevo sends per run (cost + spam control).
- Re-runs are safe: `google_place_id` uniqueness + the `outreach_log` send
  history prevent duplicate imports and double-emails.
- Cold-emailing scraped EU addresses carries GDPR/ePrivacy risk; the email ships
  with a List-Unsubscribe header, a visible opt-out, and sender identity. Send
  responsibly.
