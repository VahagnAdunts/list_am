# list.am apartment monitor

Small monitor for detecting new listings on list.am and sending a notification
about each one. The default URL is the apartments-for-sale category in Yerevan:

```text
https://www.list.am/category/62/1?n=8&cmtype=0
```

Listing IDs that have already been seen are stored in `listam_listings.json`.
On every run the script:

1. Downloads the page.
2. Parses out every `/item/<id>` link.
3. Compares the IDs to the ones in storage.
4. For each new ID, sends a notification.
5. Saves only the IDs whose notification succeeded, so failures will retry.

## Local usage

```bash
python3 monitor_listam.py                    # auto fetcher, local file, stdout notifier
python3 monitor_listam.py --dry-run          # find new IDs, do not write or notify
python3 monitor_listam.py --fetcher browser  # force headless Chrome
python3 monitor_listam.py --html-file page.html  # parse a saved HTML file
```

### Fetchers

list.am blocks plain Python and `curl` requests with HTTP 403, so the script
ships several fetchers and tries them in order:

1. `curl_cffi` (recommended) — impersonates a real browser's TLS handshake.
   Requires `pip install curl_cffi`.
2. `urllib` — Python stdlib request. Almost always 403 on list.am, but useful
   for other URLs.
3. `curl` — same idea as `urllib` via the system `curl`.
4. `browser` — headless Google Chrome / Chromium / Brave / Edge using
   `--dump-dom`. No webdriver needed.

Force one with `--fetcher curl_cffi|urllib|curl|browser`. Point at a specific
browser with `--browser-binary "/Applications/Google Chrome.app/..."`.

### Notifications

`--notifier stdout` (default) prints new listings to the terminal.
`--notifier telegram` sends a Telegram message per listing using
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

The IDs file is updated only for listings whose notification actually
succeeded; the rest are retried on the next run.

## Production on Render (Cron Job)

Render's filesystem is ephemeral, so the IDs JSON has to live somewhere
durable. The simplest option is to keep it in your GitHub repo and have the
cron job commit changes back to it via the GitHub API.

### One-time setup

1. **Create a Telegram bot.**
   - Talk to [@BotFather](https://t.me/BotFather) in Telegram, run `/newbot`,
     copy the token.
   - Send any message to your new bot, then visit
     `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your
     `chat.id`.
2. **Create a GitHub fine-grained PAT.**
   - GitHub → Settings → Developer settings → Personal access tokens →
     Fine-grained tokens.
   - Repository access: only this repo.
   - Repository permissions: **Contents: Read and write**.
   - Copy the token.
3. **Push this project to a GitHub repo** (private is fine). Make sure
   `listam_listings.json` is committed at the repo root.

### Deploy

The repo includes [`render.yaml`](render.yaml). On Render:

1. New → Blueprint → connect this repo.
2. Render reads `render.yaml` and offers a Cron Job named `listam-monitor`.
3. Fill in the secrets it asks for:
   - `GITHUB_REPO` — `your-username/your-repo`.
   - `GITHUB_TOKEN` — the PAT from step 2.
   - `TELEGRAM_BOT_TOKEN` — the BotFather token.
   - `TELEGRAM_CHAT_ID` — your chat id.
4. Apply. The job runs every 15 minutes (`*/15 * * * *`); change the
   `schedule` field to adjust.

The cron container boots, runs `python monitor_listam.py`, and exits. Each
new listing triggers a Telegram message and the IDs JSON is committed back to
your repo.

### Why curl_cffi instead of a webdriver

`curl_cffi` reuses libcurl-impersonate to mimic Chrome's TLS / HTTP/2
fingerprint, which is what list.am's anti-bot is actually checking. It's a
~10 MB pip install, runs fine on Render's free Python plan, and needs no
browser or webdriver. If list.am ever blocks it, switch to the `browser`
fetcher — it requires bundling Chrome with a custom Dockerfile (Render
Native Runtimes don't include it).

## Configuration reference

| Variable / flag | Default | Notes |
|---|---|---|
| `LISTAM_URL` / `--url` | category 62 apartments URL | The page to monitor. |
| `LISTAM_FETCHER` / `--fetcher` | `auto` | `auto`, `curl_cffi`, `urllib`, `curl`, `browser`. |
| `LISTAM_BROWSER_BINARY` / `--browser-binary` | autodetect | Path to a Chromium-based browser. |
| `LISTAM_STORAGE` / `--storage` | `local` | `local` or `github`. |
| `LISTAM_IDS_PATH` / `--ids-file` | `listam_listings.json` | Used by `local` storage. |
| `GITHUB_REPO` / `--github-repo` | — | `owner/name`. Required for `github` storage. |
| `GITHUB_PATH` / `--github-path` | `listam_listings.json` | Path of the JSON inside the repo. |
| `GITHUB_BRANCH` / `--github-branch` | `main` | Branch to read from and commit to. |
| `GITHUB_TOKEN` | — | PAT with Contents: Read and write. |
| `LISTAM_NOTIFIER` / `--notifier` | `stdout` | `stdout` or `telegram`. |
| `TELEGRAM_BOT_TOKEN` | — | Required for `telegram` notifier. |
| `TELEGRAM_CHAT_ID` | — | Required for `telegram` notifier. |
| `--html-file` | — | Parse a saved HTML file instead of fetching. |
| `--dry-run` | off | Print findings, do not notify or save. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No new listings, or all new listings notified and saved. |
| `1` | Fatal error (config invalid, fetch failed, no listings parsed, etc.). |
| `2` | Some notifications failed; failed IDs were not saved and will retry. |
