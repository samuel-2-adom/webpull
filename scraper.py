"""
WebPull - Scraper Engine
========================
The core scraping logic. Handles site detection,
authentication, extraction, and output formatting.

This module is intentionally kept separate from the
web layer so it can be tested and used independently.
"""

import csv
import getpass
import hashlib
import io
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONSTANTS
# ============================================================

VERSION = "1.0.0"

DEFAULT_TIMEOUT = 20
DEFAULT_DELAY   = 0.5
DEFAULT_RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Site types WebPull can detect
SITE_TYPES = {
    "html"       : "Plain HTML",
    "nextauth"   : "Next.js / NextAuth",
    "wordpress"  : "WordPress",
    "django"     : "Django",
    "laravel"    : "Laravel / PHP",
    "shopify"    : "Shopify",
    "rest_api"   : "REST API backed",
    "js_rendered": "JavaScript rendered",
    "cloudflare" : "Cloudflare protected",
    "unknown"    : "Unknown",
}


# ============================================================
# LOGGER
# A simple log collector so the web UI can stream progress
# ============================================================

class JobLogger:
    """
    Collects log messages during a scrape job.
    The web layer reads these to show live progress to the user.
    """

    def __init__(self):
        self.entries = []

    def log(self, level, message):
        entry = {
            "level"  : level,    # info | ok | warn | error
            "message": message,
        }
        self.entries.append(entry)
        # Also print to server console for debugging
        prefix = {"info": "  ", "ok": "✓ ", "warn": "⚠ ", "error": "✗ "}
        print(f"{prefix.get(level, '')} {message}")

    def info(self, msg):  self.log("info",  msg)
    def ok(self, msg):    self.log("ok",    msg)
    def warn(self, msg):  self.log("warn",  msg)
    def error(self, msg): self.log("error", msg)


# ============================================================
# URL HELPERS
# ============================================================

def normalize_url(url):
    url = url.strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def get_origin(url):
    """https://example.com/products  →  https://example.com"""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def remove_fragment(url):
    return urldefrag(url)[0]


# ============================================================
# ROBOTS.TXT
# ============================================================

def check_robots(url, logger):
    p = urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        if not rp.can_fetch(HEADERS["User-Agent"], url):
            logger.warn(
                "robots.txt disallows scraping this URL. "
                "Proceeding is against the site's wishes."
            )
            return False
        logger.ok("robots.txt check passed.")
    except Exception:
        logger.warn("Could not read robots.txt — proceeding anyway.")
    return True


# ============================================================
# SESSION
# ============================================================

def create_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


# ============================================================
# SITE DETECTION
# ============================================================

def detect_site(url, logger):
    """
    Probe the URL and figure out what kind of site it is.

    Detection order matters — we check the most specific
    signals first, fall back to general heuristics last.

    Returns a dict:
        {
            "type"       : "nextauth",
            "label"      : "Next.js / NextAuth",
            "confidence" : "high",
            "notes"      : "NextAuth providers endpoint found.",
            "signals"    : [...list of what we found...]
        }
    """

    logger.info(f"Detecting site type for: {url}")

    origin  = get_origin(url)
    signals = []
    session = create_session()

    # --------------------------------------------------------
    # 1. Cloudflare check — do this first because Cloudflare
    #    will interfere with all other detection signals
    # --------------------------------------------------------

    try:
        r = session.get(url, timeout=DEFAULT_TIMEOUT)
        server  = r.headers.get("Server", "").lower()
        cf_ray  = r.headers.get("CF-RAY", "")
        cf_cache = r.headers.get("CF-Cache-Status", "")

        if cf_ray or "cloudflare" in server or cf_cache:
            signals.append("Cloudflare headers detected")

            # Check if it's actually blocking us (challenge page)
            if r.status_code in {403, 503} or "challenge" in r.text.lower():
                logger.warn("Cloudflare bot protection detected.")
                return _result(
                    "cloudflare",
                    "high",
                    "Cloudflare is actively blocking automated requests.",
                    signals,
                )

            signals.append("Cloudflare present but not blocking")

    except requests.exceptions.RequestException as exc:
        logger.warn(f"Initial probe failed: {exc}")
        return _result("unknown", "low", str(exc), signals)

    # --------------------------------------------------------
    # 2. NextAuth detection
    # --------------------------------------------------------

    try:
        r = session.get(
            origin + "/api/auth/providers",
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, dict) and data:
                    signals.append("NextAuth /api/auth/providers endpoint found")
                    logger.ok("NextAuth detected.")
                    return _result(
                        "nextauth",
                        "high",
                        "NextAuth Credentials provider found.",
                        signals,
                    )
            except ValueError:
                pass
    except requests.exceptions.RequestException:
        pass

    # --------------------------------------------------------
    # 3. WordPress detection
    # --------------------------------------------------------

    try:
        r = session.get(
            origin + "/wp-login.php",
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200 and "wordpress" in r.text.lower():
            signals.append("WordPress login page found at /wp-login.php")
            logger.ok("WordPress detected.")
            return _result(
                "wordpress",
                "high",
                "WordPress login page found.",
                signals,
            )
    except requests.exceptions.RequestException:
        pass

    # Also check the generator meta tag
    try:
        r = session.get(url, timeout=DEFAULT_TIMEOUT)
        if "wordpress" in r.text.lower() and (
            'content="WordPress' in r.text
            or "wp-content" in r.text
        ):
            signals.append("WordPress meta/content signatures found")
            logger.ok("WordPress detected via page content.")
            return _result(
                "wordpress",
                "high",
                "WordPress signatures found in page content.",
                signals,
            )
    except requests.exceptions.RequestException:
        pass

    # --------------------------------------------------------
    # 4. Shopify detection
    # --------------------------------------------------------

    try:
        r = session.get(url, timeout=DEFAULT_TIMEOUT)
        if (
            "myshopify.com" in r.url
            or "Shopify.theme" in r.text
            or "shopify.com/s/files" in r.text
            or r.headers.get("X-ShopId")
        ):
            signals.append("Shopify signatures detected")
            logger.warn("Shopify site detected.")
            return _result(
                "shopify",
                "high",
                "Shopify platform detected. Consider using the Shopify API instead.",
                signals,
            )
    except requests.exceptions.RequestException:
        pass

    # --------------------------------------------------------
    # 5. Django detection
    # --------------------------------------------------------

    try:
        r = session.get(url, timeout=DEFAULT_TIMEOUT)
        cookies = {c.name for c in session.cookies}
        if "csrftoken" in cookies and "sessionid" in cookies:
            signals.append("Django csrftoken + sessionid cookies found")
            logger.ok("Django detected.")
            return _result(
                "django",
                "medium",
                "Django CSRF and session cookies detected.",
                signals,
            )
        if "csrftoken" in cookies:
            signals.append("Django csrftoken cookie found")
    except requests.exceptions.RequestException:
        pass

    # --------------------------------------------------------
    # 6. Laravel detection
    # --------------------------------------------------------

    try:
        r = session.get(url, timeout=DEFAULT_TIMEOUT)
        powered_by = r.headers.get("X-Powered-By", "").lower()
        cookies = {c.name for c in session.cookies}
        if "laravel" in powered_by or "XSRF-TOKEN" in cookies:
            signals.append("Laravel XSRF-TOKEN or X-Powered-By header found")
            logger.ok("Laravel detected.")
            return _result(
                "laravel",
                "medium",
                "Laravel framework signatures detected.",
                signals,
            )
    except requests.exceptions.RequestException:
        pass

    # --------------------------------------------------------
    # 7. REST API backed detection
    #    Heuristic: page HTML has very little content but
    #    there are fetch/XHR calls in the script tags
    # --------------------------------------------------------

    try:
        r = session.get(url, timeout=DEFAULT_TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")

        # Look for API call patterns in inline scripts
        scripts = " ".join(
            s.get_text() for s in soup.find_all("script")
            if not s.get("src")
        )

        api_patterns = [
            r"fetch\(['\"]\/api\/",
            r"axios\.",
            r"\.get\(['\"]\/api",
            r"XMLHttpRequest",
        ]

        api_hits = sum(
            1 for p in api_patterns
            if re.search(p, scripts)
        )

        if api_hits >= 2:
            signals.append(f"API call patterns found in scripts ({api_hits} hits)")

        # Count visible text vs script weight
        body = soup.body or soup
        for tag in body(["script", "style", "noscript"]):
            tag.decompose()

        visible_text = body.get_text(" ", strip=True)
        word_count   = len(visible_text.split())
        script_count = len(
            BeautifulSoup(r.text, "html.parser").find_all("script")
        )

        signals.append(f"Visible words: {word_count}")
        signals.append(f"Script tags: {script_count}")

        if api_hits >= 2 and word_count < 100:
            logger.warn("REST API-backed site detected.")
            return _result(
                "rest_api",
                "medium",
                (
                    "Site appears to load data from a REST API. "
                    "Check browser DevTools → Network → Fetch/XHR "
                    "to find the real data endpoint."
                ),
                signals,
            )

        # --------------------------------------------------------
        # 8. JS-rendered detection
        # --------------------------------------------------------

        if word_count < 50 and script_count > 3:
            logger.warn("JavaScript-rendered site detected.")
            return _result(
                "js_rendered",
                "medium",
                (
                    "Page content is likely rendered by JavaScript. "
                    "BeautifulSoup will only see the empty HTML shell."
                ),
                signals,
            )

        # --------------------------------------------------------
        # 9. Plain HTML — everything else
        # --------------------------------------------------------

        logger.ok("Plain HTML site detected.")
        return _result(
            "html",
            "high",
            "Standard HTML page. BeautifulSoup will work well.",
            signals,
        )

    except requests.exceptions.RequestException as exc:
        logger.error(f"Detection failed: {exc}")
        return _result("unknown", "low", str(exc), signals)


def _result(site_type, confidence, notes, signals):
    return {
        "type"      : site_type,
        "label"     : SITE_TYPES.get(site_type, "Unknown"),
        "confidence": confidence,
        "notes"     : notes,
        "signals"   : signals,
    }


# ============================================================
# HTTP FETCH
# ============================================================

def fetch(session, url, logger, timeout=DEFAULT_TIMEOUT,
          retries=DEFAULT_RETRIES, delay=0):

    if delay:
        time.sleep(delay)

    for attempt in range(1, retries + 1):
        try:
            logger.info(f"GET {url}  (attempt {attempt}/{retries})")

            r = session.get(
                url,
                timeout=timeout,
                allow_redirects=True,
            )

            logger.info(
                f"Status: {r.status_code}  |  "
                f"{len(r.text):,} chars"
            )

            r.raise_for_status()
            return r

        except requests.exceptions.Timeout:
            logger.warn("Request timed out.")

        except requests.exceptions.ConnectionError:
            logger.warn("Connection failed.")

        except requests.exceptions.HTTPError as exc:
            status = (
                exc.response.status_code
                if exc.response else None
            )
            logger.error(f"HTTP {status}")
            if status in {400, 401, 403, 404}:
                return None

        except requests.exceptions.RequestException as exc:
            logger.warn(f"Request error: {exc}")

        if attempt < retries:
            logger.info("Retrying in 2s...")
            time.sleep(2)

    logger.error("All attempts failed.")
    return None


# ============================================================
# CACHING
# ============================================================

def _cache_path(url, cache_dir):
    key = hashlib.sha1(url.encode()).hexdigest()
    return Path(cache_dir) / f"{key}.html"


def get_html(session, url, logger, cache_dir=None,
             timeout=DEFAULT_TIMEOUT, delay=0,
             retries=DEFAULT_RETRIES):

    if cache_dir:
        path = _cache_path(url, cache_dir)
        if path.exists():
            logger.ok(f"Loaded from cache.")
            return path.read_text(encoding="utf-8")

    r = fetch(session, url, logger,
              timeout=timeout, retries=retries, delay=delay)

    if r is None:
        return None

    html = r.text

    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        _cache_path(url, cache_dir).write_text(html, encoding="utf-8")

    return html


# ============================================================
# AUTHENTICATION
# ============================================================

def auth_nextauth(session, url, credentials, logger,
                  timeout=DEFAULT_TIMEOUT):
    """
    Authenticate against a NextAuth Credentials site.

    credentials = {
        "username_field": "email",
        "username"      : "user@example.com",
        "password"      : "secret",
    }

    Key fixes vs earlier universal scrapers:
        1. Explicit Content-Type header (the critical fix)
        2. callbackUrl → /dashboard not the target page
        3. Provider ID auto-detected not hardcoded
        4. Username field name is configurable
    """

    logger.info("Starting NextAuth login...")

    origin      = get_origin(url)
    provider_id = "credentials"

    # Step 0: Warm up the session by visiting the origin first.
    # Some NextAuth sites require session cookies before CSRF works.
    try:
        session.get(origin, timeout=timeout)
        logger.info("Session warmed up.")
    except Exception:
        pass

    # Auto-detect the credentials provider ID
    try:
        r = session.get(
            origin + "/api/auth/providers",
            timeout=timeout,
        )
        if r.status_code == 200:
            for pid, pdata in r.json().items():
                if pdata.get("type") == "credentials":
                    provider_id = pid
                    logger.ok(f"Provider detected: {provider_id}")
                    break
    except Exception:
        logger.warn("Could not detect provider — using 'credentials'.")

    # Step 1: Get CSRF token
    try:
        r = session.get(
            origin + "/api/auth/csrf",
            timeout=timeout,
        )
        r.raise_for_status()
        csrf_token = r.json().get("csrfToken")

        if not csrf_token:
            logger.error("No csrfToken in response.")
            return False

        logger.ok("CSRF token obtained.")

    except Exception as exc:
        logger.error(f"Could not get CSRF token: {exc}")
        return False

    # Step 2: POST credentials
    callback_url   = f"{origin}/api/auth/callback/{provider_id}"
    username_field = credentials.get("username_field", "email")

    payload = {
        username_field: credentials["username"],
        "password"    : credentials["password"],
        "csrfToken"   : csrf_token,
        "callbackUrl" : origin + "/dashboard",
        "json"        : "true",
    }

    logger.info(f"Signing in via: {callback_url}")

    try:
        r = session.post(
            callback_url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer"     : origin + "/login",
                "Origin"      : origin,
            },
            timeout=timeout,
            allow_redirects=False,
        )

        logger.info(f"Login response: {r.status_code}")

        # Fix 3 — explicit 401 handling
        if r.status_code == 401:
            logger.error("Server returned 401 — credentials rejected.")
            return False

        try:
            resp_data = r.json()
            if resp_data.get("error"):
                logger.error(f"Server error: {resp_data['error']}")
                return False
        except ValueError:
            pass  # 302 redirects have no JSON body — that's fine

    except requests.exceptions.RequestException as exc:
        logger.error(f"Login request failed: {exc}")
        return False

    # Step 3: Verify session
    try:
        r = session.get(
            origin + "/api/auth/session",
            timeout=timeout,
        )
        r.raise_for_status()
        session_data = r.json()

    except Exception as exc:
        logger.error(f"Could not verify session: {exc}")
        return False

    user = session_data.get("user") if session_data else None

    if not user:
        logger.error(
            "Login failed — session returned no user. "
            "Check credentials and username field name."
        )
        return False

    logger.ok(f"Logged in as: {user.get('name') or user.get('email', '—')}")
    return True


def auth_wordpress(session, url, credentials, logger,
                   timeout=DEFAULT_TIMEOUT):
    """WordPress login via /wp-login.php"""

    logger.info("Starting WordPress login...")

    origin    = get_origin(url)
    login_url = origin + "/wp-login.php"

    try:
        r = session.get(login_url, timeout=timeout)
        r.raise_for_status()
    except Exception as exc:
        logger.error(f"Could not load WordPress login page: {exc}")
        return False

    soup    = BeautifulSoup(r.text, "html.parser")
    payload = {}

    # Collect hidden fields (nonces etc.)
    form = soup.find("form", id="loginform")
    if form:
        for hidden in form.find_all("input", type="hidden"):
            name  = hidden.get("name")
            value = hidden.get("value", "")
            if name:
                payload[name] = value

    payload["log"]         = credentials["username"]
    payload["pwd"]         = credentials["password"]
    payload["wp-submit"]   = "Log In"
    payload["redirect_to"] = origin + "/wp-admin/"
    payload["testcookie"]  = "1"

    try:
        r = session.post(
            login_url,
            data=payload,
            timeout=timeout,
            allow_redirects=True,
        )

        # WordPress redirects to /wp-admin/ on success
        if "wp-admin" in r.url or "dashboard" in r.url:
            logger.ok("WordPress login successful.")
            return True

        if "incorrect" in r.text.lower() or "error" in r.text.lower():
            logger.error("WordPress login failed — check credentials.")
            return False

        logger.ok("WordPress login appears successful.")
        return True

    except Exception as exc:
        logger.error(f"WordPress login failed: {exc}")
        return False


def auth_html_form(session, url, credentials, logger,
                   timeout=DEFAULT_TIMEOUT):
    """
    Generic HTML form login.
    Loads the login page, finds the form, submits it.
    Works for Django, Laravel, and most traditional sites.
    """

    logger.info("Starting HTML form login...")

    login_url = credentials.get("login_url") or url

    try:
        r = session.get(login_url, timeout=timeout)
        r.raise_for_status()
    except Exception as exc:
        logger.error(f"Could not load login page: {exc}")
        return False

    soup    = BeautifulSoup(r.text, "html.parser")
    form    = soup.find("form")
    payload = {}

    if form:
        for hidden in form.find_all("input", type="hidden"):
            name  = hidden.get("name")
            value = hidden.get("value", "")
            if name:
                payload[name] = value

        action   = form.get("action") or login_url
        post_url = urljoin(login_url, action)
    else:
        logger.warn("No <form> found — posting directly to login URL.")
        post_url = login_url

    username_field = credentials.get("username_field", "email")
    password_field = credentials.get("password_field", "password")

    payload[username_field] = credentials["username"]
    payload[password_field] = credentials["password"]

    try:
        r = session.post(
            post_url,
            data=payload,
            timeout=timeout,
            allow_redirects=True,
        )

        logger.info(f"Response: {r.status_code}  |  Final URL: {r.url}")

        if "login" in r.url.lower() or "signin" in r.url.lower():
            logger.warn(
                "Ended up back on login page — "
                "login may have failed."
            )
        else:
            logger.ok("Login appears successful.")

        return True

    except Exception as exc:
        logger.error(f"Login failed: {exc}")
        return False


def authenticate(session, url, auth_config, logger,
                 timeout=DEFAULT_TIMEOUT):
    """
    Route to the correct auth handler based on auth_config["method"].

    auth_config = {
        "method"        : "nextauth" | "wordpress" | "html_form" | "none",
        "username"      : "...",
        "password"      : "...",
        "username_field": "email",     # optional
        "password_field": "password",  # optional
        "login_url"     : "...",       # optional, for html_form
    }
    """

    method = auth_config.get("method", "none")

    if method == "none":
        logger.ok("No authentication required.")
        return True

    elif method == "nextauth":
        return auth_nextauth(
            session, url, auth_config, logger, timeout
        )

    elif method == "wordpress":
        return auth_wordpress(
            session, url, auth_config, logger, timeout
        )

    elif method == "html_form":
        return auth_html_form(
            session, url, auth_config, logger, timeout
        )

    else:
        logger.warn(f"Unknown auth method: {method}")
        return False


# ============================================================
# TEXT CLEANING
# ============================================================

def clean(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


# ============================================================
# EXTRACTORS
# ============================================================

def extract_title(soup):
    return clean(soup.title.get_text()) if soup.title else ""


def extract_all_text(soup):
    s = BeautifulSoup(str(soup), "html.parser")
    for tag in s(["script", "style", "noscript", "template"]):
        tag.decompose()
    return clean(s.get_text(" ", strip=True))


def extract_links(soup, base_url):
    results = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        results.append({
            "text": clean(a.get_text(" ", strip=True)),
            "url" : href,
        })
    return results


def extract_images(soup, base_url):
    results = []
    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
        )
        if not src:
            continue
        results.append({
            "alt": clean(img.get("alt", "")),
            "url": urljoin(base_url, src),
        })
    return results


def extract_headings(soup):
    results = []
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        results.append({
            "level": int(tag.name[1]),
            "text" : clean(tag.get_text(" ", strip=True)),
        })
    return results


def extract_tables(soup):
    all_tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if cells:
                rows.append([
                    clean(c.get_text(" ", strip=True))
                    for c in cells
                ])
        if rows:
            all_tables.append(rows)
    return all_tables


def extract_selector(soup, selector):
    try:
        return [
            clean(el.get_text(" ", strip=True))
            for el in soup.select(selector)
        ]
    except Exception:
        return []


def extract_structured(soup, container_selector, fields, base_url):
    """
    Pull named fields from each repeating container.

    Example:
        container : .product
        fields    : {"name": ".product-name", "price": ".price"}

    Returns a list of dicts, one per container match.
    """
    records = []

    try:
        containers = soup.select(container_selector)
    except Exception:
        return []

    for container in containers:
        record = {}
        for field_name, selector in fields.items():
            try:
                el = container.select_one(selector)
            except Exception:
                el = None

            if el is None:
                record[field_name] = None
            elif el.name == "a":
                record[field_name] = urljoin(
                    base_url, el.get("href", "")
                )
            else:
                record[field_name] = clean(
                    el.get_text(" ", strip=True)
                )

        records.append(record)

    return records


def find_next_page(soup, current_url, selector):
    if not selector:
        return None
    try:
        el = soup.select_one(selector)
        if el and el.get("href"):
            return urljoin(current_url, el["href"])
    except Exception:
        pass
    return None


# ============================================================
# EXTRACT ONE PAGE
# ============================================================

def extract_page(html, base_url, config):
    """
    config = {
        "extract"    : ["title","text","links","images",
                        "headings","tables","selector","structured"],
        "selector"   : ".price",           # for "selector" mode
        "container"  : ".product",         # for "structured" mode
        "fields"     : {"name": ".name"},  # for "structured" mode
    }
    """

    soup   = BeautifulSoup(html, "html.parser")
    result = {"_url": base_url}
    modes  = config.get("extract", [])

    if "title" in modes:
        result["title"] = extract_title(soup)

    if "text" in modes:
        result["text"] = extract_all_text(soup)

    if "links" in modes:
        result["links"] = extract_links(soup, base_url)

    if "images" in modes:
        result["images"] = extract_images(soup, base_url)

    if "headings" in modes:
        result["headings"] = extract_headings(soup)

    if "tables" in modes:
        result["tables"] = extract_tables(soup)

    if "selector" in modes and config.get("selector"):
        result["selector_results"] = extract_selector(
            soup, config["selector"]
        )

    if "structured" in modes and config.get("container"):
        result["structured"] = extract_structured(
            soup,
            config["container"],
            config.get("fields", {}),
            base_url,
        )

    result["_soup"] = soup
    return result


# ============================================================
# OUTPUT FORMATTERS
# ============================================================

def to_json(all_results):
    """Return JSON string of results (no internal fields)."""
    clean_data = [
        {k: v for k, v in r.items() if k != "_soup"}
        for r in all_results
    ]
    return json.dumps(clean_data, indent=2, ensure_ascii=False)


def to_csv(all_results):
    """Return CSV string of results."""
    rows = _flatten_for_csv(all_results)
    if not rows:
        return ""

    fields = list(dict.fromkeys(
        k for row in rows for k in row.keys()
    ))

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=fields,
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def to_txt(all_results):
    """
    Human-readable text format.
    Inspired by the SNHS scraper's clean pipe-delimited output.
    """
    lines = []

    for page in all_results:
        url = page.get("_url", "")
        lines.append(f"URL: {url}")
        lines.append("=" * 60)

        if "title" in page:
            lines.append(f"Title: {page['title']}")

        if "text" in page:
            # Trim long text for readability
            text = page["text"][:2000]
            lines.append(f"\nText:\n{text}")

        if "headings" in page:
            lines.append("\nHeadings:")
            for h in page["headings"]:
                indent = "  " * (h["level"] - 1)
                lines.append(f"{indent}H{h['level']}: {h['text']}")

        if "links" in page:
            lines.append("\nLinks:")
            for link in page["links"]:
                lines.append(f"  {link['text']} → {link['url']}")

        if "images" in page:
            lines.append("\nImages:")
            for img in page["images"]:
                lines.append(f"  [{img['alt']}] {img['url']}")

        if "tables" in page:
            lines.append("\nTables:")
            for i, table in enumerate(page["tables"], 1):
                lines.append(f"  Table {i}:")
                for row in table:
                    lines.append("  " + " | ".join(row))

        if "selector_results" in page:
            lines.append("\nSelector Results:")
            for item in page["selector_results"]:
                lines.append(f"  • {item}")

        if "structured" in page:
            lines.append("\nStructured Data:")
            for record in page["structured"]:
                parts = [
                    f"{k}: {v}"
                    for k, v in record.items()
                    if v is not None
                ]
                lines.append("  " + " | ".join(parts))

        lines.append("")

    return "\n".join(lines)


def _flatten_for_csv(all_results):
    """Flatten nested results into flat rows for CSV."""
    rows = []

    for page in all_results:
        base = {"url": page.get("_url", "")}

        if "title" in page:
            base["title"] = page["title"]

        # Structured data → one row per record
        if "structured" in page:
            for record in page["structured"]:
                row = dict(base)
                row.update(record)
                rows.append(row)
            continue

        # Links → one row per link
        if "links" in page:
            for link in page["links"]:
                row = dict(base)
                row["type"]      = "link"
                row["link_text"] = link["text"]
                row["link_url"]  = link["url"]
                rows.append(row)
            continue

        # Images → one row per image
        if "images" in page:
            for img in page["images"]:
                row = dict(base)
                row["type"]      = "image"
                row["image_alt"] = img["alt"]
                row["image_url"] = img["url"]
                rows.append(row)
            continue

        # Selector results → one row per result
        if "selector_results" in page:
            for item in page["selector_results"]:
                row = dict(base)
                row["result"] = item
                rows.append(row)
            continue

        # Text, headings, etc → single row
        if "text" in page:
            base["text"] = page["text"][:500]

        if "headings" in page:
            base["headings"] = " | ".join(
                h["text"] for h in page["headings"]
            )

        rows.append(base)

    return rows


# ============================================================
# MAIN SCRAPE FUNCTION
# ============================================================

def run_scrape(job_config, logger):
    """
    The main entry point called by the web layer.

    job_config = {
        "url"      : "https://example.com",
        "auth"     : {
            "method"        : "none" | "nextauth" | "wordpress" | "html_form",
            "username"      : "...",
            "password"      : "...",
            "username_field": "email",
            "password_field": "password",
            "login_url"     : "...",
        },
        "extract"  : {
            "modes"    : ["title", "links", ...],
            "selector" : ".price",
            "container": ".product",
            "fields"   : {"name": ".name", "price": ".price"},
        },
        "settings" : {
            "pages"        : 1,
            "next_selector": "a.next",
            "delay"        : 0.5,
            "timeout"      : 20,
            "cache_dir"    : None,
        },
    }

    Returns:
        {
            "success": True/False,
            "results": [...],
            "json"   : "...",
            "csv"    : "...",
            "txt"    : "...",
        }
    """

    url = normalize_url(job_config.get("url", ""))

    if not url:
        logger.error("No valid URL provided.")
        return {"success": False, "results": []}

    logger.info(f"Starting WebPull job for: {url}")

    settings = job_config.get("settings", {})
    pages          = int(settings.get("pages", 1))
    next_selector  = settings.get("next_selector", "")
    delay          = float(settings.get("delay", DEFAULT_DELAY))
    timeout        = int(settings.get("timeout", DEFAULT_TIMEOUT))
    cache_dir      = settings.get("cache_dir") or None

    extract_config = job_config.get("extract", {})
    extraction     = {
        "extract"  : extract_config.get("modes", ["title"]),
        "selector" : extract_config.get("selector", ""),
        "container": extract_config.get("container", ""),
        "fields"   : extract_config.get("fields", {}),
    }

    # --------------------------------------------------------
    # Check robots.txt
    # --------------------------------------------------------

    check_robots(url, logger)

    # --------------------------------------------------------
    # Create session and authenticate
    # --------------------------------------------------------

    session    = create_session()
    auth_config = job_config.get("auth", {"method": "none"})

    ok = authenticate(session, url, auth_config, logger, timeout)

    if not ok:
        logger.error("Authentication failed — aborting job.")
        return {"success": False, "results": []}

    # --------------------------------------------------------
    # Scrape pages
    # --------------------------------------------------------

    all_results = []
    current_url = url
    visited     = set()

    for page_num in range(1, pages + 1):

        current_url = remove_fragment(current_url)

        if current_url in visited:
            logger.warn("Pagination loop detected — stopping.")
            break

        visited.add(current_url)

        logger.info(f"Page {page_num}/{pages}: {current_url}")

        html = get_html(
            session,
            current_url,
            logger,
            cache_dir=cache_dir,
            timeout=timeout,
            delay=delay if page_num > 1 else 0,
        )

        if html is None:
            logger.error("Could not retrieve page.")
            break

        result = extract_page(html, current_url, extraction)
        all_results.append(result)

        logger.ok(f"Page {page_num} extracted successfully.")

        # Pagination
        if page_num >= pages:
            break

        soup = result.get("_soup")
        if soup and next_selector:
            next_url = find_next_page(soup, current_url, next_selector)
            if next_url:
                logger.info(f"Next page: {next_url}")
                current_url = next_url
            else:
                logger.info("No next page found — done.")
                break

    # --------------------------------------------------------
    # Format output
    # --------------------------------------------------------

    logger.info("Formatting output...")

    json_out = to_json(all_results)
    csv_out  = to_csv(all_results)
    txt_out  = to_txt(all_results)

    logger.ok(
        f"Done. {len(all_results)} page(s) scraped."
    )

    return {
        "success": True,
        "results": [
            {k: v for k, v in r.items() if k != "_soup"}
            for r in all_results
        ],
        "json": json_out,
        "csv" : csv_out,
        "txt" : txt_out,
    }
