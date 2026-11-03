# etuovi-tilastokeskus-mcp

## Etuovi listing fetch pipeline

Daily job that fetches new apartment listings matching your saved criteria from
Etuovi's public search API and appends them to an Excel file kept in Google
Drive (so it syncs to your PC automatically via Google Drive desktop sync).
Runs on GitHub Actions — no server or always-on machine needed.

- `config/criteria.json` — your search filters (city, districts, price, rooms,
  year built). Edit this to change what gets fetched; it's not a secret.
- `fetch_etuovi.py` — fetches listings (no login required) and appends
  new/unseen ones to the Excel file.
- `.github/workflows/fetch-etuovi.yml` — daily cron (06:00 UTC) that downloads
  the Excel file from Google Drive, runs the fetch, and re-uploads it.

### One-time setup

1. **Install rclone locally** (on your own machine, not in CI):
   https://rclone.org/downloads/

2. **Create your own Google OAuth client** (rclone's shared client ID is being
   retired during 2026, and apps left in "Testing" mode get refresh tokens
   that expire after 7 days — both would silently break the daily cron, so
   don't skip this):
   - https://console.cloud.google.com/ → create a new project
   - APIs & Services → Library → enable **Google Drive API**
   - APIs & Services → OAuth consent screen → User type **External**, fill in
     app name + your email, skip scopes, then **set publishing status to
     "In production"** (still fine unverified — you'll just click through an
     "unverified app" warning once, since it's your own account)
   - APIs & Services → Credentials → Create Credentials → OAuth client ID →
     application type **Desktop app** → copy the Client ID and Client Secret

3. **Authorize a Google Drive remote** (use your personal Google account, not
   a managed/work account — those require admin consent and will likely be
   blocked):
   ```
   rclone config
   ```
   Choose `n` (new remote), name it `gdrive`, pick the `drive` storage type
   (Google Drive). Paste the Client ID / Client Secret from step 2. When
   asked for **scope**, choose `drive.file` ("Access to files created by
   rclone only") rather than full `drive` access — rclone only needs to
   manage the one file it creates, nothing else in your Drive. Leave
   `root_folder_id` and `service_account_file` blank, skip advanced config
   (`n`), and use auto config (`y`) — it opens a browser to log into Google
   and approve access (click through the unverified-app warning). Answer `n`
   when asked if this is a Shared/Team Drive, then confirm to keep the
   remote.

4. **Pick a folder/file name** for the sheet, e.g. `Etuovi/etuovi_listings.xlsx`.
   With `drive.file` scope nothing exists yet — the first real upload (step 7,
   or the workflow's first run) is what actually creates it in your Drive.

5. **Base64-encode your rclone config** so it can be stored as a GitHub secret:
   ```
   base64 -w0 ~/.config/rclone/rclone.conf
   ```
   (On macOS, drop `-w0`: `base64 ~/.config/rclone/rclone.conf | tr -d '\n'`)

6. **Add repo secrets/variables** (Settings → Secrets and variables → Actions):
   - Secret `RCLONE_CONF_B64` — the base64 string from step 5.
   - Variable `EXCEL_CLOUD_PATH` — e.g. `gdrive:Etuovi/etuovi_listings.xlsx`.

7. **Test it**: Actions tab → "Fetch Etuovi listings" → "Run workflow", then
   check the file appears in your Google Drive (Drive web UI, or the synced
   folder on your PC if you have Google Drive for desktop installed).

Note: Google can occasionally require re-authorization if the OAuth consent
is revoked or expires; if the workflow starts failing to authenticate after a
long time, just redo steps 3 and 5 to refresh the stored config.