# apartment-listing-tracker

## Apartment listing fetch pipeline

Daily job that fetches new apartment listings matching saved criteria from a
public real-estate listing search and appends them to an Excel file kept in
Google Drive (syncs to PC via Google Drive desktop sync). Runs on GitHub
Actions.

- `config/criteria.json` — search filters (city, districts, price, rooms,
  year built). Not a secret.
- `fetch_listings.py` — fetches listings (no login required) and appends
  new/unseen ones to the Excel file. Needs `LISTING_SITE_BASE_URL` set (see
  below) — deliberately not hardcoded in source.
- `.github/workflows/fetch-listings.yml` — downloads the Excel file from
  Google Drive, runs the fetch, re-uploads it.

**Sheets**: `Unrenovated`/`Renovated` (one per profile), tracking active
listings with `Status`/`RemovedDate` for delisting, plus a shared
`BuildingHistory` sheet (per-building sold/withdrawn history, flat table
across all buildings, deduped by record `Id`). Note: `BuildingHistory` prices
are last-asking-price-before-delisting, not confirmed sale prices.

**Scheduling**: GitHub's native `schedule:` cron is unreliable for low-traffic
repos (confirmed — zero runs over two days despite a valid config), so the
real trigger is an external cron service ([cron-job.org](https://cron-job.org))
calling `workflow_dispatch` via the GitHub REST API. The in-file `schedule:`
is kept as a harmless fallback.

- Fine-grained GitHub token (github.com/settings/tokens?type=beta), scoped to
  this repo only, **Actions: Read and write**. Expires — note the date.
- cron-job.org job: POST to
  `https://api.github.com/repos/sampeksi/apartment-listing-tracker/actions/workflows/fetch-listings.yml/dispatches`,
  headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
  `Content-Type: application/json`, body `{"ref":"main"}`. Daily schedule
  (cron-job.org uses its own configured timezone, not UTC). Turn on the
  "execution fails" notification.

### One-time setup

1. Install rclone locally: https://rclone.org/downloads/

2. Create your own Google OAuth client (rclone's shared one is being retired
   in 2026; apps left in "Testing" mode get 7-day refresh-token expiry):
   - console.cloud.google.com → new project → enable **Google Drive API**
   - OAuth consent screen → External, app name + email, skip scopes, then
     **publish to production** (unverified is fine, click through the warning)
   - Credentials → OAuth client ID → **Desktop app** → copy Client ID/Secret

3. `rclone config` → new remote named `gdrive`, type `drive`, paste
   Client ID/Secret from step 2, scope `drive.file` (not full `drive`),
   leave `root_folder_id`/`service_account_file` blank, auto config → `y`
   (click through unverified-app warning), not a Shared Drive.

4. Pick a Drive folder/file path, e.g. `Listings/listings.xlsx` — only
   created on first real upload (drive.file scope).

5. `base64 -w0 ~/.config/rclone/rclone.conf` (macOS: drop `-w0`, pipe through
   `tr -d '\n'`).

6. Repo secrets/variables (Settings → Secrets and variables → Actions):
   - Secret `RCLONE_CONF_B64` — output of step 5.
   - Secret `LISTING_SITE_BASE_URL` — the target site's base URL (not written
     here on purpose — you know what it is).
   - Variable `EXCEL_CLOUD_PATH` — e.g. `gdrive:Listings/listings.xlsx`.

7. Actions tab → "Fetch apartment listings" → Run workflow → check Drive.

If auth starts failing after a long time, redo steps 3 and 5.

## Business listing pipeline (separate flow)

`fetch_business.py` + `config/business_criteria.json` + `fetch-business.yml`:
same mechanics as above (reuses `RCLONE_CONF_B64`/`LISTING_SITE_BASE_URL`),
different search shape (whole cities, not city+district) and a different
Drive file. Needs one more repo variable: `BUSINESS_EXCEL_CLOUD_PATH`.

This is the foundation for a larger, still-in-progress flow: DCF-based
investment calculus (ported from a reference spreadsheet), rent comps from
a sibling rental-listing site, an LLM analysis pass, and Telegram delivery
of a daily new-listings digest. None of that is built yet — this stage is
just the fetch + dedup layer, which a first run already uses as a backfill
(everything found gets recorded as seen; only genuinely new listings on
later runs are candidates for the analysis/notification steps once they
exist).
