#!/usr/bin/env python3
"""Detect new list.am listing IDs and notify before persisting them.

The script is split into small pieces:

* Fetchers download the page (curl_cffi, urllib, curl, headless browser).
* Parsers extract listings from the HTML.
* Storages read/write the known-IDs JSON (local file or GitHub repo).
* Notifiers send messages (stdout or Telegram).
* `monitor()` glues everything together.

Most behaviour is configured via environment variables so the same script can
run locally and on a Render Cron Job. CLI flags override env vars.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_URL = "https://www.list.am/category/62/1?n=8&cmtype=0"
ROOT = Path(__file__).resolve().parent
DEFAULT_LOCAL_IDS_FILE = ROOT / "listam_listings.json"

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "hy-AM,hy;q=0.9,en-US;q=0.8,en;q=0.7",
}

BROWSER_CANDIDATES: tuple[str, ...] = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

FETCHER_AUTO = "auto"
FETCHER_CURL_CFFI = "curl_cffi"
FETCHER_URLLIB = "urllib"
FETCHER_CURL = "curl"
FETCHER_BROWSER = "browser"
FETCHER_CHOICES = (
    FETCHER_AUTO,
    FETCHER_CURL_CFFI,
    FETCHER_URLLIB,
    FETCHER_CURL,
    FETCHER_BROWSER,
)

STORAGE_LOCAL = "local"
STORAGE_GITHUB = "github"
STORAGE_CHOICES = (STORAGE_LOCAL, STORAGE_GITHUB)

NOTIFIER_STDOUT = "stdout"
NOTIFIER_TELEGRAM = "telegram"
NOTIFIER_CHOICES = (NOTIFIER_STDOUT, NOTIFIER_TELEGRAM)


# ---------------------------------------------------------------------------
# Models + parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Listing:
    id: str
    url: str
    title: str


class _ListingParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self._listings_by_id: dict[str, Listing] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return

        href = dict(attrs).get("href")
        if listing_id_from_href(href) is None:
            return

        self._current_href = href
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current_href is None:
            return

        listing_id = listing_id_from_href(self._current_href)
        if listing_id and listing_id not in self._listings_by_id:
            title = " ".join("".join(self._current_text).split())
            self._listings_by_id[listing_id] = Listing(
                id=listing_id,
                url=urljoin(self.base_url, self._current_href),
                title=title,
            )

        self._current_href = None
        self._current_text = []

    @property
    def listings(self) -> list[Listing]:
        return list(self._listings_by_id.values())


def listing_id_from_href(href: str | None) -> str | None:
    if not href:
        return None

    marker = "/item/"
    if marker not in href:
        return None

    listing_id = href.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
    return listing_id if listing_id.isdigit() else None


def parse_listings(content: str, base_url: str) -> list[Listing]:
    parser = _ListingParser(base_url)
    parser.feed(content)
    return parser.listings


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_html(url: str, fetcher: str, browser_binary: str | None) -> str:
    fetchers: Sequence[str]
    if fetcher == FETCHER_AUTO:
        fetchers = (FETCHER_CURL_CFFI, FETCHER_URLLIB, FETCHER_CURL, FETCHER_BROWSER)
    else:
        fetchers = (fetcher,)

    errors: list[str] = []
    for name in fetchers:
        try:
            if name == FETCHER_CURL_CFFI:
                return _fetch_with_curl_cffi(url)
            if name == FETCHER_URLLIB:
                return _fetch_with_urllib(url)
            if name == FETCHER_CURL:
                return _fetch_with_curl(url)
            if name == FETCHER_BROWSER:
                return _fetch_with_browser(url, browser_binary)
            raise ValueError(f"Unknown fetcher: {name}")
        except Exception as error:  # noqa: BLE001 - capture and continue
            errors.append(f"{name}: {error}")

    joined = "; ".join(errors)
    raise RuntimeError(f"Failed to fetch {url}: {joined}")


def _fetch_with_curl_cffi(url: str) -> str:
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as error:
        raise RuntimeError(
            "curl_cffi is not installed. Install with: pip install curl_cffi"
        ) from error

    response = cffi_requests.get(url, impersonate="chrome", timeout=30)
    response.raise_for_status()
    return response.text


def _fetch_with_urllib(url: str) -> str:
    request = Request(url, headers=DEFAULT_HEADERS)
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError(str(error)) from error


def _fetch_with_curl(url: str) -> str:
    curl_path = shutil.which("curl")
    if curl_path is None:
        raise RuntimeError("curl is not available on PATH")

    command = [
        curl_path,
        "--fail",
        "--location",
        "--compressed",
        "--silent",
        "--show-error",
    ]
    for name, value in DEFAULT_HEADERS.items():
        command.extend(["--header", f"{name}: {value}"])
    command.append(url)

    result = subprocess.run(command, capture_output=True, check=False, text=True, timeout=30)
    if result.returncode != 0:
        message = result.stderr.strip() or f"curl exited with {result.returncode}"
        raise RuntimeError(message)

    return result.stdout


def _resolve_browser_binary(browser_binary: str | None) -> str:
    if browser_binary:
        if not Path(browser_binary).is_file():
            raise RuntimeError(f"Browser binary not found: {browser_binary}")
        return browser_binary

    on_path = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    if on_path:
        return on_path

    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate

    raise RuntimeError(
        "No Chromium-based browser found. Install Google Chrome or pass --browser-binary."
    )


def _fetch_with_browser(url: str, browser_binary: str | None) -> str:
    binary = _resolve_browser_binary(browser_binary)

    command = [
        binary,
        "--headless",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--virtual-time-budget=15000",
        f"--user-agent={DEFAULT_HEADERS['User-Agent']}",
        "--dump-dom",
        url,
    ]

    result = subprocess.run(command, capture_output=True, check=False, text=True, timeout=90)
    if result.returncode != 0 or not result.stdout.strip():
        message = result.stderr.strip() or f"browser exited with {result.returncode}"
        raise RuntimeError(message)

    return result.stdout


# ---------------------------------------------------------------------------
# Storage backends
# ---------------------------------------------------------------------------


class Storage(Protocol):
    def read(self) -> list[str]: ...

    def write(self, ids: Iterable[str], message: str) -> None: ...


class LocalStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> list[str]:
        if not self.path.exists():
            return []

        with self.path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ValueError(f"{self.path} must contain a JSON array of string IDs")

        return data

    def write(self, ids: Iterable[str], message: str) -> None:
        unique_ids = list(dict.fromkeys(ids))
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            delete=False,
        ) as file:
            json.dump(unique_ids, file, ensure_ascii=False, indent=2)
            file.write("\n")
            temp_path = Path(file.name)

        temp_path.replace(self.path)


class GitHubStorage:
    """Read and write the IDs JSON in a GitHub repository.

    Uses the Contents API (https://docs.github.com/en/rest/repos/contents).
    """

    def __init__(self, repo: str, path: str, token: str, branch: str = "main") -> None:
        if "/" not in repo:
            raise ValueError("GitHub repo must be in the form 'owner/name'")

        self.repo = repo
        self.path = path.lstrip("/")
        self.token = token
        self.branch = branch
        self._sha: str | None = None

    @property
    def _api_url(self) -> str:
        return f"https://api.github.com/repos/{self.repo}/contents/{self.path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "listam-monitor",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def read(self) -> list[str]:
        url = f"{self._api_url}?ref={self.branch}"
        request = Request(url, headers=self._headers())

        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            if error.code == 404:
                self._sha = None
                return []
            raise RuntimeError(f"GitHub read failed: {error}") from error
        except URLError as error:
            raise RuntimeError(f"GitHub read failed: {error}") from error

        self._sha = payload.get("sha")
        encoded_content: str = payload.get("content", "")
        raw_content = base64.b64decode(encoded_content).decode("utf-8") if encoded_content else "[]"
        data = json.loads(raw_content) if raw_content.strip() else []

        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ValueError(f"{self.repo}:{self.path} must contain a JSON array of string IDs")

        return data

    def write(self, ids: Iterable[str], message: str) -> None:
        unique_ids = list(dict.fromkeys(ids))
        content = json.dumps(unique_ids, ensure_ascii=False, indent=2) + "\n"
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        body: dict[str, object] = {
            "message": message,
            "content": encoded,
            "branch": self.branch,
        }
        if self._sha is not None:
            body["sha"] = self._sha

        request = Request(
            self._api_url,
            method="PUT",
            data=json.dumps(body).encode("utf-8"),
            headers={**self._headers(), "Content-Type": "application/json"},
        )

        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace") if error.fp else ""
            raise RuntimeError(f"GitHub write failed ({error.code}): {detail}") from error
        except URLError as error:
            raise RuntimeError(f"GitHub write failed: {error}") from error

        self._sha = payload.get("content", {}).get("sha")


# ---------------------------------------------------------------------------
# Notifiers
# ---------------------------------------------------------------------------


class Notifier(Protocol):
    def __call__(self, listings: list[Listing]) -> list[Listing]: ...


def stdout_notifier(listings: list[Listing]) -> list[Listing]:
    print(f"Found {len(listings)} new listing(s):")
    for listing in listings:
        title = f" - {listing.title}" if listing.title else ""
        print(f"{listing.id}: {listing.url}{title}")
    return list(listings)


@dataclass
class TelegramNotifier:
    bot_token: str
    chat_id: str
    timeout: float = 20.0

    def __call__(self, listings: list[Listing]) -> list[Listing]:
        sent: list[Listing] = []
        for listing in listings:
            try:
                self._send(self._format(listing))
                sent.append(listing)
                time.sleep(0.2)
            except Exception as error:  # noqa: BLE001 - log + continue
                print(
                    f"Telegram notify failed for {listing.id}: {error}",
                    file=sys.stderr,
                )
        return sent

    @staticmethod
    def _format(listing: Listing) -> str:
        title = listing.title or f"Listing {listing.id}"
        if len(title) > 250:
            title = title[:247] + "..."
        return (
            "<b>New list.am listing</b>\n"
            f"{html.escape(title)}\n"
            f"{html.escape(listing.url)}"
        )

    def _send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        body = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace") if error.fp else ""
            raise RuntimeError(f"Telegram error ({error.code}): {detail}") from error
        except URLError as error:
            raise RuntimeError(str(error)) from error

        if not payload.get("ok"):
            raise RuntimeError(f"Telegram error: {payload}")


# ---------------------------------------------------------------------------
# Config + CLI
# ---------------------------------------------------------------------------


@dataclass
class Config:
    url: str
    fetcher: str
    browser_binary: str | None
    storage: str
    local_ids_path: Path
    github_repo: str | None
    github_path: str
    github_branch: str
    github_token: str | None
    notifier: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    html_file: Path | None
    dry_run: bool


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor list.am for new apartment listings.")
    parser.add_argument("--url", default=env("LISTAM_URL", DEFAULT_URL))
    parser.add_argument(
        "--fetcher",
        choices=FETCHER_CHOICES,
        default=env("LISTAM_FETCHER", FETCHER_AUTO),
    )
    parser.add_argument(
        "--browser-binary",
        default=env("LISTAM_BROWSER_BINARY"),
    )
    parser.add_argument(
        "--storage",
        choices=STORAGE_CHOICES,
        default=env("LISTAM_STORAGE", STORAGE_LOCAL),
    )
    parser.add_argument(
        "--ids-file",
        type=Path,
        default=Path(env("LISTAM_IDS_PATH", str(DEFAULT_LOCAL_IDS_FILE))),
    )
    parser.add_argument("--github-repo", default=env("GITHUB_REPO"))
    parser.add_argument("--github-path", default=env("GITHUB_PATH", "listam_listings.json"))
    parser.add_argument("--github-branch", default=env("GITHUB_BRANCH", "main"))
    parser.add_argument(
        "--notifier",
        choices=NOTIFIER_CHOICES,
        default=env("LISTAM_NOTIFIER", NOTIFIER_STDOUT),
    )
    parser.add_argument("--html-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def load_config(args: argparse.Namespace) -> Config:
    return Config(
        url=args.url,
        fetcher=args.fetcher,
        browser_binary=args.browser_binary,
        storage=args.storage,
        local_ids_path=args.ids_file,
        github_repo=args.github_repo,
        github_path=args.github_path,
        github_branch=args.github_branch,
        github_token=env("GITHUB_TOKEN"),
        notifier=args.notifier,
        telegram_bot_token=env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=env("TELEGRAM_CHAT_ID"),
        html_file=args.html_file,
        dry_run=args.dry_run,
    )


def build_storage(config: Config) -> Storage:
    if config.storage == STORAGE_LOCAL:
        return LocalStorage(config.local_ids_path)

    if config.storage == STORAGE_GITHUB:
        if not config.github_repo:
            raise RuntimeError("GitHub storage requires --github-repo or GITHUB_REPO")
        if not config.github_token:
            raise RuntimeError("GitHub storage requires GITHUB_TOKEN")
        return GitHubStorage(
            repo=config.github_repo,
            path=config.github_path,
            token=config.github_token,
            branch=config.github_branch,
        )

    raise ValueError(f"Unknown storage: {config.storage}")


def build_notifier(config: Config) -> Notifier:
    if config.notifier == NOTIFIER_STDOUT:
        return stdout_notifier

    if config.notifier == NOTIFIER_TELEGRAM:
        if not config.telegram_bot_token or not config.telegram_chat_id:
            raise RuntimeError(
                "Telegram notifier requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
            )
        return TelegramNotifier(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
        )

    raise ValueError(f"Unknown notifier: {config.notifier}")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def monitor(config: Config) -> int:
    storage = build_storage(config)
    notifier = build_notifier(config)

    known_ids = storage.read()
    known_id_set = set(known_ids)

    if config.html_file:
        content = config.html_file.read_text(encoding="utf-8")
    else:
        content = fetch_html(
            config.url,
            fetcher=config.fetcher,
            browser_binary=config.browser_binary,
        )

    listings = parse_listings(content, config.url)
    if not listings:
        raise RuntimeError("No listing IDs were found on the page")

    new_listings = [listing for listing in listings if listing.id not in known_id_set]
    if not new_listings:
        print("No new listings found.")
        return 0

    if config.dry_run:
        stdout_notifier(new_listings)
        print("Dry run enabled; storage was not updated.")
        return 0

    sent_listings = notifier(new_listings)
    if not sent_listings:
        print("No notifications were sent; storage was not updated.", file=sys.stderr)
        return 1

    sent_ids = [listing.id for listing in sent_listings]
    storage.write(
        [*known_ids, *sent_ids],
        message=f"Add {len(sent_ids)} new list.am listing id(s)",
    )
    print(f"Saved {len(sent_ids)} new ID(s) via {config.storage}.")

    if len(sent_listings) != len(new_listings):
        print(
            f"Notification failed for {len(new_listings) - len(sent_listings)} listing(s); "
            "they will be retried next run.",
            file=sys.stderr,
        )
        return 2

    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args)
        return monitor(config)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
