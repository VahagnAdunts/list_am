# list.am apartment monitor

Small monitor for detecting new listings on list.am and sending a notification
about each one. By default it monitors both sale and rent categories:

```text
sell: https://www.list.am/category/62/1?n=8&cmtype=0
rent: https://www.list.am/category/56/1?n=8&cmtype=0
```

Listing IDs that have already been seen are stored separately:

- `listam_listings.json` for `sell`
- `listam_category_56_ids.json` for `rent`

On every run the script:

1. Downloads the page.
2. Parses out every `/item/<id>` link.
3. Compares the IDs to the ones in storage.
4. For each new ID, sends a notification starting with `sell` or `rent`.
5. Saves only the IDs whose notification succeeded, so failures will retry.

## Local usage

```bash
python3 monitor_listam.py                    # monitor both categories
python3 monitor_listam.py --dry-run          # find new IDs, do not write or notify
python3 monitor_listam.py --category rent    # monitor only rentals
python3 monitor_listam.py --category sell    # monitor only sales
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

## Production on GitHub Actions

The cron lives in [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml)
and runs every 15 minutes on GitHub-hosted runners. Each run:

1. Checks out the repo (so it has the current ID JSON files).
2. Installs `curl_cffi`.
3. Runs `python monitor_listam.py` with `LISTAM_STORAGE=local` and
   `LISTAM_NOTIFIER=telegram`.
4. If either ID JSON file changed, commits and pushes the diff back as
   `github-actions[bot]`. The workflow's auto-provided `GITHUB_TOKEN` has
   `contents: write` so no Personal Access Token is needed.

### One-time setup

1. **Create a Telegram bot.**
   - Talk to [@BotFather](https://t.me/BotFather) in Telegram, run `/newbot`,
     copy the token.
   - Send any message to your new bot, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your
     `chat.id`. Or message **@userinfobot** to get the id instantly.
2. **Push this project to a GitHub repo** (private is fine). Make sure
   both ID files are committed at the repo root.
3. **Add the Telegram secrets to the repo.**
   - Repo → Settings → Secrets and variables → Actions.
   - Add `TELEGRAM_BOT_TOKEN` (BotFather token) and `TELEGRAM_CHAT_ID`.

### Trigger and verify

- Repo → **Actions** tab → **list.am monitor** → **Run workflow**.
- Successful logs print either `No new listings found.` or
  `Saved N new ID(s) via local.` followed by a `[skip ci]` commit by
  `github-actions[bot]` and N Telegram messages.

To change the cadence, edit the `cron:` line in
`.github/workflows/monitor.yml`. GitHub may delay scheduled runs by a few
minutes during high load, which is fine for this use case.

### Why curl_cffi instead of a webdriver

`curl_cffi` reuses libcurl-impersonate to mimic Chrome's TLS / HTTP/2
fingerprint, which is what list.am's anti-bot is actually checking. It's a
~10 MB pip install, no browser or webdriver needed.

If list.am ever blocks GitHub's runner IPs as well, fall back to running the
script on a residential IP (e.g. your own machine via `launchd` or `cron`).

## Configuration reference

| Variable / flag | Default | Notes |
|---|---|---|
| `LISTAM_CATEGORY` / `--category` | `all` | `all`, `sell`, or `rent`. |
| `LISTAM_SELL_URL` / `--sell-url` | category 62 URL | Sale page to monitor. |
| `LISTAM_RENT_URL` / `--rent-url` | category 56 URL | Rent page to monitor. |
| `LISTAM_URL` / `--url` | — | Legacy single URL override for the sell category. |
| `LISTAM_FETCHER` / `--fetcher` | `auto` | `auto`, `curl_cffi`, `urllib`, `curl`, `browser`. |
| `LISTAM_BROWSER_BINARY` / `--browser-binary` | autodetect | Path to a Chromium-based browser. |
| `LISTAM_STORAGE` / `--storage` | `local` | `local` or `github`. |
| `LISTAM_IDS_PATH` / `--ids-file` | `listam_listings.json` | Used by `local` storage. |
| `LISTAM_RENT_IDS_PATH` / `--rent-ids-file` | `listam_category_56_ids.json` | Used by `local` storage for rent listings. |
| `GITHUB_REPO` / `--github-repo` | — | `owner/name`. Required for `github` storage. |
| `GITHUB_PATH` / `--github-path` | `listam_listings.json` | Path of the JSON inside the repo. |
| `GITHUB_BRANCH` / `--github-branch` | `main` | Branch to read from and commit to. |
| `GITHUB_TOKEN` | — | PAT with Contents: Read and write. |
| `LISTAM_NOTIFIER` / `--notifier` | `stdout` | `stdout` or `telegram`. |
| `TELEGRAM_BOT_TOKEN` | — | Required for `telegram` notifier. |
| `TELEGRAM_CHAT_ID` | — | Required for `telegram` notifier. |
| `LISTAM_MAX_NEW_PER_RUN` / `--max-new-per-run` | `30` | Safety cap. Abort instead of notifying when more listings look new than this. Set `0` to disable. |
| `--html-file` | — | Parse a saved HTML file instead of fetching. |
| `--dry-run` | off | Print findings, do not notify or save. |

## Safeguards against message storms

Two protections are built in so a missing or empty IDs file can't blast your
phone:

- **Cold-start bootstrap.** If storage has 0 known IDs, the script saves the
  current page snapshot and exits without notifying. The next run only
  reports listings that appear after this snapshot.
- **Safety cap.** If more than `LISTAM_MAX_NEW_PER_RUN` listings look new
  (default 30), the run aborts before sending any notifications. Investigate,
  then either raise the cap, write a snapshot manually, or rerun with
  `--max-new-per-run 0`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No new listings, or all new listings notified and saved. |
| `1` | Fatal error (config invalid, fetch failed, no listings parsed, etc.). |
| `2` | Some notifications failed; failed IDs were not saved and will retry. |
