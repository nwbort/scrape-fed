#!/usr/bin/env python3
"""
Scrape Federal Court files from the Commonwealth Courts Portal.

For each file listed in matters.txt (or given on the command line):

1. fetch everything the portal shows the public (file details, applications,
   court events and orders, documents filed, parties) into
   matters/<slug>/file.json; see portal.py;
2. download each "View orders" PDF into matters/<slug>/orders/;
3. download each "View judgment" from judgments.fedcourt.gov.au into
   matters/<slug>/judgments/. That site is behind Cloudflare, so this needs
   Chrome (see judgments.py), which is only started when a judgment hasn't
   been saved yet;
4. push anything that changed since the last run to ntfy.sh; see notify.py.

The portal itself needs no browser: everything but the judgments is plain
HTTP.

If a file's pages can't all be loaded, its saved state is left untouched,
which avoids false alarms, and the run exits non-zero. A judgment that can't
be fetched (often one that isn't published yet) is just retried next run.

Usage: scrape.py [FILE ...]     (defaults to the files in matters.txt)

FILE is a file number (NSD1310/2026) or a portal URL.
"""

import asyncio
import json
import os
import re
import sys
from email.message import Message
from urllib.parse import urlparse

import notify
import portal

MATTERS_FILE = "matters.txt"
MATTERS_DIR = "matters"


def read_matters() -> list[str]:
    with open(MATTERS_FILE, encoding="utf-8") as f:
        lines = (line.split("#", 1)[0].strip() for line in f)
        return [line for line in lines if line]


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def previous_url_gh(previous: dict | None, kind: str) -> dict[str, str]:
    """url -> url_gh for every order or judgment ('orders'/'judgments')
    saved by an earlier run."""
    out = {}
    for app in (previous or {}).get("applications", []):
        for event in app.get("events", []):
            for item in event.get(kind, []):
                if item.get("url") and item.get("url_gh"):
                    out[item["url"]] = item["url_gh"]
    return out


def _saved(url_gh: str | None) -> bool:
    return bool(url_gh) and os.path.exists(url_gh.lstrip("/"))


def order_filename(url: str, headers: dict, data: bytes) -> str:
    """The name the portal gives the file (P_NSD1310_2026_2835642.pdf), or
    the document id from the URL."""
    msg = Message()
    msg["content-disposition"] = headers.get("Content-Disposition", "")
    name = msg.get_filename()
    if name:
        name = portal_safe(os.path.basename(name))
    if not name:
        name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        name += ".pdf" if data.startswith(b"%PDF") else ""
    return name


def portal_safe(name: str) -> str | None:
    name = re.sub(r"[^\w.()-]+", "-", name).strip("-.")
    return name or None


def download_orders(session: portal.Session, data: dict, previous: dict | None, matter_dir: str) -> None:
    """Fill in url_gh for each order, downloading the ones not saved yet. A
    download that fails is left without url_gh and tried again next run."""
    saved = previous_url_gh(previous, "orders")
    orders_dir = os.path.join(matter_dir, "orders")
    for app in data["applications"]:
        for event in app["events"]:
            for order in event.get("orders", []):
                url_gh = saved.get(order["url"])
                if _saved(url_gh):
                    order["url_gh"] = url_gh
                    continue
                try:
                    body, headers = session.get(order["url"])
                except Exception as e:
                    print(f"  WARNING: could not download {order['url']}: {e}", flush=True)
                    continue
                if body.lstrip()[:1] == b"<":
                    print(f"  WARNING: {order['url']} returned a page, not a document", flush=True)
                    continue
                filename = order_filename(order["url"], headers, body)
                os.makedirs(orders_dir, exist_ok=True)
                dest = os.path.join(orders_dir, filename)
                with open(dest, "wb") as f:
                    f.write(body)
                print(f"  downloaded {dest} ({len(body)} bytes)", flush=True)
                order["url_gh"] = f"/{dest}"


async def download_judgments(get_browser, data: dict, previous: dict | None, matter_dir: str, cache: dict) -> None:
    """Fill in url_gh for each judgment, fetching the ones not saved yet.
    get_browser() starts Chrome, so it's only called for one of those.
    cache holds this run's downloads by URL, so a judgment linked from two
    listings (or two files) is only fetched once."""
    saved = previous_url_gh(previous, "judgments")
    judgments_dir = os.path.join(matter_dir, "judgments")
    for app in data["applications"]:
        for event in app["events"]:
            for j in event.get("judgments", []):
                if not j.get("url"):
                    continue
                url_gh = saved.get(j["url"])
                if _saved(url_gh):
                    j["url_gh"] = url_gh
                    continue
                if j["url"] not in cache:
                    import judgments

                    cache[j["url"]] = await judgments.download_judgment(await get_browser(), j["url"])
                result = cache[j["url"]]
                if result is None:
                    continue
                filename, body = result
                dest = os.path.join(judgments_dir, filename)
                if not os.path.exists(dest):
                    os.makedirs(judgments_dir, exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(body)
                    print(f"  downloaded {dest} ({len(body)} bytes)", flush=True)
                j["url_gh"] = f"/{dest}"


async def scrape_file(session, get_browser, text: str, judgment_cache: dict) -> bool:
    ref = portal.parse_file_ref(text)
    slug = portal.file_slug(ref)
    matter_dir = os.path.join(MATTERS_DIR, slug)
    json_path = os.path.join(matter_dir, "file.json")
    previous = load_json(json_path, None)

    print(f"Scraping {ref['number']}/{ref['year']}", flush=True)
    data = portal.fetch_file(session, ref, previous)
    n_events = sum(len(a["events"]) for a in data["applications"])
    n_docs = sum(len(a["documents"]) for a in data["applications"])
    print(
        f"  {len(data['applications'])} application(s), {n_events} listing(s), {n_docs} document(s) filed",
        flush=True,
    )
    if previous and previous["applications"] and not data["applications"]:
        print("ERROR: applications are now empty; assuming a bad page, not updating", flush=True)
        return False

    download_orders(session, data, previous, matter_dir)
    await download_judgments(get_browser, data, previous, matter_dir, judgment_cache)

    if previous is not None:
        changes = notify.diff_file(previous, data)
        if changes:
            number = data["file"].get("number") or f"{ref['number']}/{ref['year']}"
            notify.send(
                notify.title_for(number, changes),
                "\n".join(line for _, line in changes),
                click=data["file"]["url"],
                tags=["scales"],
                priority=4 if any(line.startswith("+") for _, line in changes) else 3,
            )

    write_json(json_path, data)
    print(f"Wrote {json_path}", flush=True)
    return True


async def scrape(files: list[str]) -> int:
    session = portal.Session()
    browser = None

    async def get_browser():
        nonlocal browser
        if browser is None:
            import judgments

            browser = judgments.Browser()
        return browser

    failures = []
    judgment_cache: dict = {}
    try:
        for text in files:
            try:
                ok = await scrape_file(session, get_browser, text, judgment_cache)
            except Exception as e:
                print(f"ERROR scraping {text}: {e!r}", flush=True)
                ok = False
            if not ok:
                failures.append(text)
    finally:
        if browser is not None:
            browser.close()

    if failures:
        print(f"Failed: {', '.join(failures)}", flush=True)
        return 2
    return 0


def main() -> int:
    files = sys.argv[1:] or read_matters()
    return asyncio.run(scrape(files))


if __name__ == "__main__":
    sys.exit(main())
