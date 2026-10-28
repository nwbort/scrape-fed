#!/usr/bin/env python3
"""
Download Federal Court judgments from judgments.fedcourt.gov.au.

Unlike the Courts Portal, the judgments site sits behind Cloudflare's managed
challenge, so a plain request only ever sees "Just a moment...". We drive a
real Chrome via nodriver (headful under Xvfb), wait for the challenge to
clear, and follow the judgment page's "Original Word Document" link:

  www.judgments.fedcourt.gov.au/judgments/Judgments/fca/single/2026/2026fca1000
    -> "Original Word Document" -> .../2026fca1000/2026fca1000
    -> 302 to www.fedcourt.gov.au/file-store/Judgments/.../2026FCA1000.docx

Both Federal Court hosts have their own challenge and the redirect crosses
between them, so a fetch() from the judgment page is refused (CORS); the
destination is read from the browser's network events instead, its host's
challenge is cleared by loading its home page, and the file is fetched from
there.

This is the approach nwbort/tribunal-tracker uses for the Competition
Tribunal's decisions, which are the same Federal Court judgment pages.

Chrome is only started when there's a judgment to fetch (see Browser), so a
run with nothing new never pays for it.

Usage: judgments.py JUDGMENT_URL [OUT_DIR]     (fetch one judgment)
"""

import asyncio
import base64
import binascii
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from html import unescape
from html.parser import HTMLParser
from shutil import which
from urllib.parse import unquote, urljoin, urlparse

import nodriver as uc

JUDGMENT_DOMAIN = "fedcourt.gov.au"

CHALLENGE_MARKERS = (
    "Just a moment",
    "challenge-platform",
    "cf_chl_opt",
    "Enable JavaScript and cookies to continue",
    "Verifying you are human",
)

MAX_WAIT_SECONDS = 90
POLL_SECONDS = 0.5
TURNSTILE_CLICK_INTERVAL = 3
# Ceiling on any single nodriver/CDP call, so a stalled round-trip can't hang
# the run until Actions kills it.
CDP_CALL_TIMEOUT_SECONDS = 30
TURNSTILE_FIND_TIMEOUT_SECONDS = 5
REQUEST_TIMEOUT = 60

# Cloudflare decides per request, so a 403 is often gone a few seconds later.
BROWSER_FETCH_ATTEMPTS = 3
BROWSER_FETCH_RETRY_SECONDS = 5
RETRYABLE_FETCH_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})

JUDGMENT_LINK_WAIT_SECONDS = 15
JUDGMENT_REDIRECT_WAIT_SECONDS = 20

# What a judgment's file link says on its page. The Word document is the
# original; anything else offered is a rendering of it.
_FILE_LINK_RE = re.compile(r"\b(?:word document|pdf|rtf)\b", re.IGNORECASE)

_SNIFFED_EXTENSIONS = (
    (b"PK\x03\x04", ".docx"),
    (b"%PDF", ".pdf"),
    (b"{\\rtf", ".rtf"),
    (b"\xd0\xcf\x11\xe0", ".doc"),
)
JUDGMENT_EXTENSIONS = {".docx", ".doc", ".pdf", ".rtf"}

# nodriver's default args, which make the browser look like a normal session.
CHROME_ARGS = [
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-service-autorun",
    "--no-default-browser-check",
    "--homepage=about:blank",
    "--no-pings",
    "--password-store=basic",
    "--disable-infobars",
    "--disable-breakpad",
    "--disable-dev-shm-usage",
    "--disable-session-crashed-bubble",
    "--disable-search-engine-choice-screen",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-gpu",
    "--window-size=1920,1080",
    "--no-sandbox",  # CI runs as root
]


# --- Helpers -----------------------------------------------------------------


def is_judgment_host_url(url: str) -> bool:
    """True if url is https on the Federal Court's domain or a subdomain."""
    parsed = urlparse(url)
    host = parsed.hostname
    return (
        parsed.scheme == "https"
        and host is not None
        and (host == JUDGMENT_DOMAIN or host.endswith("." + JUDGMENT_DOMAIN))
    )


def safe_filename(name: str) -> str | None:
    name = re.sub(r"[^\w.()-]+", "-", name).strip("-.")
    return name or None


def looks_like_challenge(html: str) -> bool:
    return any(marker in html for marker in CHALLENGE_MARKERS)


def is_challenge_payload(data: bytes) -> bool:
    return b"Just a moment" in data[:2048] or b"challenge-platform" in data[:8192]


def sniff_extension(data: bytes) -> str | None:
    return next((e for magic, e in _SNIFFED_EXTENSIONS if data.startswith(magic)), None)


def judgment_filename(final_url: str, data: bytes) -> str | None:
    """The file-store basename (2026FCA1000.docx), with an extension sniffed
    from the bytes when the URL doesn't carry a usable one."""
    name = os.path.basename(unquote(urlparse(final_url).path)).strip()
    stem, ext = os.path.splitext(name)
    if ext.lower() not in JUDGMENT_EXTENSIONS:
        ext = sniff_extension(data)
        if not ext:
            return None
        stem = name
    return safe_filename(stem + ext)


def follow_redirects(url: str, hops: dict[str, str]) -> str:
    seen = {url}
    while url in hops and hops[url] not in seen:
        url = hops[url]
        seen.add(url)
    return url


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", unescape("".join(self._text))).strip()
            self.links.append((self._href, text))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)


def judgment_file_link(html: str, page_url: str) -> str | None:
    """The judgment's file link, preferring Word. The file sits beneath the
    judgment's own path (.../2026fca1000/2026fca1000), which is what tells it
    apart from the site's other links."""
    parser = _LinkParser()
    parser.feed(html)
    base_path = urlparse(page_url).path.rstrip("/").lower() + "/"
    candidates = []
    for href, text in parser.links:
        href = urljoin(page_url, href)
        if not is_judgment_host_url(href):
            continue
        if not urlparse(href).path.lower().startswith(base_path):
            continue
        if _FILE_LINK_RE.search(text):
            candidates.append((0 if "word" in text.lower() else 1, href))
    return min(candidates)[1] if candidates else None


def page_not_found(html: str) -> bool:
    """The judgments site's answer for a judgment that isn't published yet."""
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return bool(title and re.search(r"not found|404", title.group(1), re.I))


async def with_timeout(coro, what: str, seconds: float = CDP_CALL_TIMEOUT_SECONDS):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{what} did not respond within {seconds}s")


# --- Browser -----------------------------------------------------------------


def find_chrome() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and os.path.exists(env):
        return env
    for candidate in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        path = which(candidate)
        if path:
            return path
    raise FileNotFoundError("Could not find a Chrome/Chromium binary (set CHROME_PATH)")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for_devtools(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    # The DevTools endpoint is local: never send it through a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.time() < deadline:
        try:
            with opener.open(url, timeout=2) as r:
                print(f"DevTools ready: {json.load(r).get('Browser')}", flush=True)
                return True
        except Exception:
            time.sleep(0.5)
    return False


class Browser:
    """A Chrome started on first use and stopped by close().

    We launch Chrome ourselves and wait for its DevTools port before
    attaching nodriver: nodriver's own launcher only waits ~2.5s, which loses
    the race against Chrome's cold start on CI runners."""

    def __init__(self):
        self._proc = None
        self._browser = None

    async def get(self):
        if self._browser is not None:
            return self._browser
        chrome_path = find_chrome()
        port = free_port()
        user_data_dir = tempfile.mkdtemp(prefix="cf-scrape-")
        args = [
            chrome_path,
            *CHROME_ARGS,
            f"--user-data-dir={user_data_dir}",
            "--remote-debugging-host=127.0.0.1",
            f"--remote-debugging-port={port}",
        ]
        # Chrome ignores the proxy environment variables, so pass any HTTPS
        # proxy on explicitly (not needed on GitHub's runners).
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            args.append(f"--proxy-server={proxy}")
        args.append("about:blank")
        print(f"Launching Chrome: {chrome_path} (port {port})", flush=True)
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_for_devtools(port):
            self.close()
            raise RuntimeError("Chrome DevTools endpoint never became ready")
        self._browser = await uc.start(host="127.0.0.1", port=port, browser_executable_path=chrome_path)
        return self._browser

    def close(self):
        if self._browser is not None:
            try:
                self._browser.stop()
            except Exception:
                pass
            self._browser = None
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


async def try_click_turnstile(tab):
    """Best-effort click of the Turnstile 'Verify you are human' checkbox."""
    for text in ("Verify you are human", "Verify you are a human", "human"):
        try:
            el = await with_timeout(
                tab.find(text, best_match=True, timeout=3), "tab.find", TURNSTILE_FIND_TIMEOUT_SECONDS
            )
            if el:
                await with_timeout(el.mouse_click(), "mouse_click")
                print(f"  clicked element matching '{text}'", flush=True)
                return True
        except Exception:
            pass
    try:
        iframe = await with_timeout(
            tab.find("challenges.cloudflare.com", best_match=True, timeout=3),
            "tab.find",
            TURNSTILE_FIND_TIMEOUT_SECONDS,
        )
        if iframe:
            await with_timeout(iframe.mouse_click(), "mouse_click")
            print("  clicked cloudflare iframe", flush=True)
            return True
    except Exception:
        pass
    return False


async def fetch_page(tab, url: str, same_host: bool = False) -> str | None:
    """Load url in tab and wait for the challenge to clear. Returns the HTML,
    or None if it never did. With same_host, any page on url's host counts as
    arrived (a home page that redirects elsewhere on the site)."""
    print(f"Navigating to {url}", flush=True)
    try:
        await with_timeout(tab.get(url), "tab.get")
    except Exception as e:
        print(f"  navigation failed: {e}", flush=True)
        return None

    parsed = urlparse(url)
    target = f"{parsed.scheme}://{parsed.netloc}" if same_host else url.split("?")[0].split("#")[0].rstrip("/")
    deadline = time.time() + MAX_WAIT_SECONDS
    next_click = time.time() + TURNSTILE_CLICK_INTERVAL
    last_title = None
    while time.time() < deadline:
        try:
            state = await with_timeout(
                tab.evaluate("document.readyState + ' ' + location.origin + location.pathname"),
                "tab.evaluate",
            )
            ready, _, href = str(state).partition(" ")
            href = href.rstrip("/")
            # The site lowercases nothing consistently, so compare paths
            # case-insensitively.
            on_page = href.lower().startswith(target.lower()) if same_host else href.lower() == target.lower()
            html = (
                await with_timeout(tab.get_content(), "tab.get_content")
                if on_page and ready == "complete"
                else None
            )
        except Exception as e:
            print(f"  page check failed: {e}", flush=True)
            await tab.sleep(POLL_SECONDS)
            continue

        if html is not None:
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            title = m.group(1).strip() if m else ""
            challenge = looks_like_challenge(html)
            if not challenge or title != last_title:
                print(f"  {len(html)} bytes, title={title!r}", flush=True)
                last_title = title
            if not challenge:
                return html
            if time.time() >= next_click:
                await try_click_turnstile(tab)
                next_click = time.time() + TURNSTILE_CLICK_INTERVAL

        await tab.sleep(POLL_SECONDS)

    print(f"WARNING: {url} did not load or its challenge did not clear", flush=True)
    return None


async def browser_fetch_once(tab, url: str) -> tuple[bytes | None, str]:
    """Fetch url with the page's own fetch(). Returns (bytes, "") or
    (None, what went wrong)."""
    js = (
        "(async () => {"
        "  try {"
        f"    const r = await fetch({json.dumps(url)}, {{credentials: 'include'}});"
        "    if (!r.ok) {"
        "      const m = r.headers.get('cf-mitigated');"
        "      return 'ERR:HTTP ' + r.status + (m ? ' (cf-mitigated: ' + m + ')' : '');"
        "    }"
        "    const b = new Uint8Array(await r.arrayBuffer());"
        "    let s = '';"
        "    for (let i = 0; i < b.length; i += 0x8000)"
        "      s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));"
        "    return 'OK:' + btoa(s);"
        "  } catch (e) {"
        "    return 'ERR:' + String(e);"
        "  }"
        "})()"
    )
    try:
        result = await with_timeout(
            tab.evaluate(js, await_promise=True, return_by_value=True), "tab.evaluate (fetch)", REQUEST_TIMEOUT
        )
    except Exception as e:
        return None, f"evaluate raised {e}"
    if not isinstance(result, str) or not result.startswith("OK:"):
        if isinstance(result, str) and result.startswith("ERR:"):
            return None, result[4:]
        return None, f"unexpected result {type(result).__name__}"
    try:
        data = base64.b64decode(result[3:], validate=True)
    except (ValueError, binascii.Error) as e:
        return None, f"undecodable payload: {e}"
    if not data:
        return None, "no bytes returned"
    if is_challenge_payload(data):
        return None, "Cloudflare challenge instead of the file"
    return data, ""


def retryable_fetch_failure(detail: str) -> bool:
    status = re.match(r"HTTP (\d{3})", detail)
    return int(status.group(1)) in RETRYABLE_FETCH_STATUSES if status else True


async def browser_fetch(tab, url: str) -> bytes | None:
    for attempt in range(1, BROWSER_FETCH_ATTEMPTS + 1):
        data, detail = await browser_fetch_once(tab, url)
        if data is not None:
            return data
        print(f"  browser fetch of {url} failed (attempt {attempt}/{BROWSER_FETCH_ATTEMPTS}): {detail}", flush=True)
        if attempt == BROWSER_FETCH_ATTEMPTS or not retryable_fetch_failure(detail):
            return None
        await asyncio.sleep(BROWSER_FETCH_RETRY_SECONDS)
    return None


async def wait_for_file_link(tab, page_url: str, html: str) -> str | None:
    deadline = time.time() + JUDGMENT_LINK_WAIT_SECONDS
    while True:
        link = judgment_file_link(html, page_url)
        if link or time.time() >= deadline:
            return link
        await tab.sleep(1)
        try:
            html = await with_timeout(tab.get_content(), "tab.get_content")
        except Exception:
            pass


async def resolve_redirect(tab, link: str) -> str | None:
    """Where link redirects to, read from the browser's network events: a
    fetch() is refused once the redirect leaves the host (CORS), but only
    after Chrome has issued the redirected request, which DevTools reports."""
    hops: dict[str, str] = {}

    def on_request(event):
        if event.redirect_response is not None:
            hops[event.redirect_response.url] = event.request.url

    tab.add_handler(uc.cdp.network.RequestWillBeSent, on_request)
    await with_timeout(tab.send(uc.cdp.network.enable()), "network.enable")
    await browser_fetch_once(tab, link)
    deadline = time.time() + JUDGMENT_REDIRECT_WAIT_SECONDS
    while link not in hops and time.time() < deadline:
        await tab.sleep(0.5)
    # nodriver can't parse some of newer Chrome's network events and logs a
    # traceback for each, so stop listening as soon as the redirect is known.
    try:
        await with_timeout(tab.send(uc.cdp.network.disable()), "network.disable")
    except Exception:
        pass
    final_url = follow_redirects(link, hops)
    return final_url if final_url != link else None


async def fetch_by_visiting(tab, url: str) -> bytes | None:
    """Load url so Cloudflare can show (and clear) its challenge, then fetch
    it. Once cleared, the navigation becomes a download and the tab stays
    where it was, still on url's origin, so keep retrying the fetch."""
    print(f"  visiting {url} to clear its challenge", flush=True)
    try:
        await with_timeout(tab.get(url), "tab.get")
    except Exception as e:
        print(f"  navigation to {url} ended: {e}", flush=True)
    deadline = time.time() + MAX_WAIT_SECONDS
    next_click = time.time() + TURNSTILE_CLICK_INTERVAL
    detail = ""
    while time.time() < deadline:
        await tab.sleep(2)
        data, detail = await browser_fetch_once(tab, url)
        if data is not None:
            return data
        if time.time() >= next_click:
            await try_click_turnstile(tab)
            next_click = time.time() + TURNSTILE_CLICK_INTERVAL
    print(f"  WARNING: could not fetch {url}: {detail}", flush=True)
    return None


async def download_judgment(browser: Browser, page_url: str) -> tuple[str, bytes] | None:
    """Fetch the file behind a judgment page, in a tab of its own.

    Returns (filename, bytes), or None if it couldn't be fetched (including
    a judgment that isn't published yet, which the next run retries)."""
    if not is_judgment_host_url(page_url):
        print(f"  WARNING: {page_url} is not a Federal Court https URL", flush=True)
        return None
    print(f"Following judgment {page_url}", flush=True)
    tab = None
    try:
        b = await browser.get()
        tab = await with_timeout(b.get("about:blank", new_tab=True), "browser.get")
        html = await fetch_page(tab, page_url)
        if html is None:
            return None
        if page_not_found(html):
            print(f"  {page_url} is not published yet", flush=True)
            return None

        link = await wait_for_file_link(tab, page_url, html)
        if link is None:
            print(f"  WARNING: no file link on {page_url}", flush=True)
            return None

        final_url = link
        data, _ = await browser_fetch_once(tab, link)
        if data is None or sniff_extension(data) is None:
            final_url = await resolve_redirect(tab, link)
            if final_url is None:
                print(f"  WARNING: {link} is neither a file nor a redirect to one", flush=True)
                return None
            if not is_judgment_host_url(final_url):
                print(f"  WARNING: {link} redirects to {final_url}, not a Federal Court host", flush=True)
                return None
            origin = f"https://{urlparse(final_url).hostname}/"
            if await fetch_page(tab, origin, same_host=True) is None:
                return None
            data = await browser_fetch(tab, final_url)
            if data is None:
                data = await fetch_by_visiting(tab, final_url)
            if data is None:
                return None
            if sniff_extension(data) is None:
                print(f"  WARNING: {final_url} is not a document", flush=True)
                return None

        filename = judgment_filename(final_url, data)
        if filename is None:
            print(f"  WARNING: no safe filename for {final_url}", flush=True)
            return None
        return filename, data
    except Exception as e:
        print(f"  WARNING: following {page_url} failed: {e!r}", flush=True)
        return None
    finally:
        if tab is not None:
            try:
                await with_timeout(tab.close(), "tab.close")
            except Exception:
                pass


async def _main(url: str, out_dir: str) -> int:
    browser = Browser()
    try:
        result = await download_judgment(browser, url)
    finally:
        browser.close()
    if result is None:
        return 2
    filename, data = result
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, filename)
    with open(dest, "wb") as f:
        f.write(data)
    print(f"Saved {dest} ({len(data)} bytes)", flush=True)
    return 0


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("Usage: judgments.py JUDGMENT_URL [OUT_DIR]", file=sys.stderr)
        return 1
    return uc.loop().run_until_complete(_main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "."))


if __name__ == "__main__":
    sys.exit(main())
