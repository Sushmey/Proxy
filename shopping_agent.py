import argparse
import datetime
import json
import math
import os
import re
import time
from pathlib import Path

from llm_client import chat_json, wrap_untrusted

# --- Playwright stealth ------------------------------------------------------
# patchright is a hardened, drop-in fork of Playwright that removes the most
# common CDP / automation-detection leaks (navigator.webdriver, Runtime.enable,
# etc.). It exposes the same sync API, so the rest of this file is unchanged.
# If patchright isn't installed we fall back to stock Playwright.
try:
    from patchright.sync_api import sync_playwright  # type: ignore

    STEALTH_BACKEND = "patchright"
except ImportError:  # pragma: no cover - plain Playwright fallback
    from playwright.sync_api import sync_playwright  # type: ignore

    STEALTH_BACKEND = "playwright"

# A normal (non-"HeadlessChrome") user agent so headless runs don't advertise
# themselves. Override with SHOP_USER_AGENT if needed.
USER_AGENT = os.getenv(
    "SHOP_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
)
# HEADLESS = os.getenv("SHOP_HEADLESS", "true").strip().lower() in (
#     "1",
#     "true",
#     "yes",
#     "on",
# )

HEADLESS = False

# --- Store definitions -------------------------------------------------------
# Each store maps to a search URL template, a login URL, a base for relative
# links, and an extractor that turns the results page into raw listings.
STORES = {
    "amazon": {
        "search_url": "https://www.amazon.com/s?k={query}",
        "login_url": "https://www.amazon.com/ap/signin",
        "base": "https://www.amazon.com",
    },
    "target": {
        "search_url": "https://www.target.com/s?searchTerm={query}",
        "login_url": "https://www.target.com/login",
        "base": "https://www.target.com",
    },
}
DEFAULT_STORE = os.getenv("SHOP_STORE", "amazon").strip().lower()

INTERPRET_PROMPT_TEMPLATE = """You are helping a shopper search an online store. Decide whether \
their request names a SPECIFIC product (a particular brand/model they already know they want, \
e.g. "Anker 6ft USB-C cable", "iPhone 15 Pro Max 256GB") versus a GENERAL request describing what \
they need without naming an exact product (e.g. "a cheap usb-c cable", "something to charge my \
phone fast", "a good pair of running shoes").

Also build the best search query to type into the store's search box -- like searching an inbox, a \
single literal phrase can miss how products are actually listed, so build a query using realistic \
store terminology, combining a few likely phrasings if the request is general.

Respond with ONLY a JSON object with exactly these keys:
- is_specific: true if a specific product/brand/model was named, false otherwise
- search_query: the search terms to use in the store's search box
"""

FILTER_PROMPT_TEMPLATE = """You are given a list of raw scraped product listings (messy text -- \
title, price, rating, shipping info all run together) and a shopper's request.

Read each listing, extract its actual product name, price, star rating (out of 5), and number of \
reviews yourself from the noisy text, and decide which ones genuinely match what the shopper asked \
for.

IMPORTANT: an ACCESSORY for a product (a case, cover, charger, cable, screen protector, stand, \
etc.) is NOT a match when the shopper asked for the product itself -- e.g. if they asked for \
"airpods", an "AirPods case" or "AirPods charging cable" does not satisfy that, even though the \
listing's title contains the word "AirPods". Only count it as a match if the listing IS the thing \
they asked for, not something designed to attach to, protect, charge, or otherwise accessorize it.

Respond with ONLY a JSON object with exactly this key:
- matches: a list of objects, each with "index" (the listing's 0-based index in the input list), \
"name" (string), "price" (a number, or null if none was found), "rating" (a number out of 5, or \
null if none was found), and "review_count" (an integer, or null if none was found). Return an \
empty list if nothing matches.
"""

# --- Stealth browser helpers -------------------------------------------------

def _launch_browser(playwright, headless):
    """Launch a stealth browser and return (browser, context, page).

    Uses patchright (or Playwright) with the arguments/context options that
    avoid the usual automation tells: no --enable-automation flag, a realistic
    viewport/locale/timezone, and a normal user agent.
    """
    browser = playwright.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent=USER_AGENT,
    )
    page = context.new_page()
    return browser, context, page

def _extract_amazon(page, max_results):
    """Extract raw listings + canonical Amazon product links from a results page."""
    results = []
    seen = set()
    for card in page.query_selector_all('[data-component-type="s-search-result"]'):
        if len(results) >= max_results:
            break
        asin = (card.get_attribute("data-asin") or "").strip()
        link = card.query_selector('h2 a[href]') or card.query_selector('a[href*="/dp/"]')
        if not link:
            continue
        href = link.get_attribute("href")
        if not href:
            continue
        # Prefer a clean canonical /dp/<ASIN> link over the tracking-laden href.
        if asin:
            url = f"https://www.amazon.com/dp/{asin}"
            key = asin
        else:
            url = href if href.startswith("http") else f"https://www.amazon.com{href}"
            key = url
        if key in seen:
            continue
        seen.add(key)
        text = card.inner_text().strip()
        if not text:
            continue
        results.append({"raw_text": text, "url": url, "store": "amazon"})
    return results

def _extract_target(page, max_results):
    """Extract raw listings + Target product links from a results page."""
    results = []
    seen_hrefs = set()
    links = page.locator('a[href*="/p/"]')
    for i in range(links.count()):
        if len(results) >= max_results:
            break
        el = links.nth(i)
        href = el.get_attribute("href")
        text = el.inner_text().strip()
        if not href or href in seen_hrefs or not text:
            continue
        seen_hrefs.add(href)
        results.append(
            {"raw_text": text, "url": f"https://www.target.com{href}", "store": "target"}
        )
    return results

_EXTRACTORS = {"amazon": _extract_amazon, "target": _extract_target}

def interpret_shopping_prompt(user_prompt):
    """Decide whether a shopping request names a specific product, and build
    a good search query either way.

    Args:
        user_prompt: The shopper's natural-language request.

    Returns:
        A dict with is_specific (bool) and search_query (str).
    """
    return chat_json(INTERPRET_PROMPT_TEMPLATE, user_prompt)

def _search_one_store(query, max_results, store):
    """Scrape a single store's search page with a stealth browser."""
    store = store.lower()
    if store not in STORES:
        raise ValueError(f"Unknown store {store!r}; expected one of {list(STORES)}")

    cfg = STORES[store]
    url = cfg["search_url"].format(query=query.replace(" ", "+"))

    with sync_playwright() as p:
        _log(f"launching browser for search (store={store!r}, headless={HEADLESS})")
        browser, context, page = _launch_browser(p, HEADLESS)
        try:
            _goto(page, url, f"{store} search results")
            _wait(page, 2000, "search results to render")
            results = _EXTRACTORS[store](page, max_results)
            if not results:
                # An interstitial can stand in for the results page (Continue
                # screen, consent prompt, etc.) -- clear it and re-extract.
                _log("no listings found -- checking for an interstitial to clear")
                if _recover_stuck_page(page, "loading search results"):
                    results = _EXTRACTORS[store](page, max_results)
            _log(f"found {len(results)} raw listing(s) on {store}")
            return results
        finally:
            _log("closing search browser")
            context.close()
            browser.close()

def search_products(query, max_results=10, store=DEFAULT_STORE):
    """Search an online store for a product and return raw scraped listings,
    using a stealth browser (patchright) to avoid automation detection. No
    login.

    Args:
        query: What to search for, e.g. "usb-c cable".
        max_results: Maximum number of listings to return (per store).
        store: "amazon", "target", or "all" to search both.

    Returns:
        A list of dicts, each with raw_text (the listing's full scraped
        text, for the LLM to parse), url, and store.
    """
    stores = list(STORES) if store.lower() == "all" else [store]
    results = []
    for s in stores:
        results.extend(_search_one_store(query, max_results, s))
    return results

def filter_products(products, user_prompt):
    """Use the LLM to extract structured info from raw scraped listings and
    filter down to the ones matching what the user actually asked for.

    Args:
        products: The list returned by search_products.
        user_prompt: The shopper's original natural-language request.

    Returns:
        A list of dicts, each with name, price, rating, review_count, url,
        and store, for the matches.
    """
    listing_text = "\n\n".join(f"[{i}] {p['raw_text']}" for i, p in enumerate(products))
    listing_block = wrap_untrusted("scraped product listings", listing_text)
    user_content = f"Shopper's request: {user_prompt}\n\nListings:\n{listing_block}"

    result = chat_json(FILTER_PROMPT_TEMPLATE, user_content)

    matches = []
    for match in result.get("matches", []):
        index = match.get("index")
        if index is None or not (0 <= index < len(products)):
            continue
        matches.append(
            {
                "name": match.get("name"),
                "price": match.get("price"),
                "rating": match.get("rating"),
                "review_count": match.get("review_count"),
                "url": products[index]["url"],
                "store": products[index].get("store"),
            }
        )
    return matches

def rank_products(products):
    """Deterministically rank products by value: maximize trustworthy
    quality (rating weighted by review volume, so a high rating with few
    reviews doesn't outrank a slightly lower rating backed by thousands) per
    dollar spent. Never an LLM judgment call -- once rating/review_count/
    price are extracted, ranking them is pure arithmetic.

    Products missing rating, review_count, or price can't be scored and are
    sorted to the end, in the order they were found.

    Args:
        products: The list returned by filter_products.

    Returns:
        The same list, sorted best-value-first.
    """

    def value_score(p):
        if p.get("rating") is None or p.get("review_count") is None or not p.get("price"):
            return None
        trust = p["rating"] * math.log(1 + p["review_count"])
        return trust / p["price"]

    scored = [(value_score(p), p) for p in products]
    scored.sort(key=lambda pair: (pair[0] is None, -(pair[0] or 0)))
    return [p for _, p in scored]

def shop_for_item(user_prompt, max_results=10, store=DEFAULT_STORE):
    """End-to-end: interpret the request, search, filter to real matches,
    and rank by value -- unless the request named a specific product, in
    which case ranking is skipped (there's no "better value" tradeoff to
    make when the user already knows exactly what they want).

    Args:
        user_prompt: The shopper's natural-language request.
        max_results: Maximum number of raw listings to scrape and consider
            (per store).
        store: "amazon", "target", or "all".

    Returns:
        A list of matching products (name, price, rating, review_count,
        url, store) -- ranked best-value-first for general requests, or
        as-found for specific-product requests.
    """
    interpretation = interpret_shopping_prompt(user_prompt)
    raw_products = search_products(
        interpretation["search_query"], max_results=max_results, store=store
    )
    matches = filter_products(raw_products, user_prompt)

    if interpretation["is_specific"]:
        return matches
    return rank_products(matches)

def find_product_link(user_prompt, max_results=10, store=DEFAULT_STORE):
    """The simple, no-checkout-automation path: search for a product and
    hand back a link for the user to buy it themselves. No login, no cart,
    no checkout -- the actual purchase click is always the human's.

    Args:
        user_prompt: The shopper's natural-language request.
        max_results: Maximum number of raw listings to scrape and consider
            (per store).
        store: "amazon", "target", or "all".

    Returns:
        A message string with the top match (or a message saying nothing
        was found).
    """
    try:
        matches = shop_for_item(user_prompt, max_results=max_results, store=store)
    except Exception as exc:  # noqa: BLE001
        # This tool's result isn't relayed verbatim (the model summarizes it),
        # but a raw exception string in its place is still worth avoiding --
        # it's confusing context for the model and could leak internal detail.
        _log(f"find_product_link failed unexpectedly: {exc!r}")
        return "Something went wrong while searching for that -- want to try again?"
    if not matches:
        return "I couldn't find anything matching that -- want to try a different search?"
    return format_product_link_message(matches[0])

def format_product_link_message(product):
    """Build the human-readable message presenting a found product with a
    link for the user to buy it themselves -- no purchase confirmation
    needed here, since the agent never touches checkout in this flow.

    Args:
        product: A single product dict (name, price, rating, review_count, url).

    Returns:
        A message string to send the user.
    """
    rating_bit = ""
    if product.get("rating") is not None and product.get("review_count") is not None:
        rating_bit = f" ({product['rating']}★, {product['review_count']} reviews)"

    store_bit = f" [{product['store']}]" if product.get("store") else ""
    return (
        f"Found{store_bit}: {product['name']} -- ${product['price']}{rating_bit}\n"
        f"{product['url']}"
    )

def format_confirmation_prompt(product):
    """Build the human-readable message asking the user to confirm a
    purchase. Never proceed to any cart/checkout step without a reply to
    this passing is_unequivocal_confirmation.

    Args:
        product: A single product dict (name, price, rating, review_count, url).

    Returns:
        A message string to send the user.
    """
    rating_bit = ""
    if product.get("rating") is not None and product.get("review_count") is not None:
        rating_bit = f" ({product['rating']}★, {product['review_count']} reviews)"

    store_bit = f" [{product['store']}]" if product.get("store") else ""
    return (
        f"Found{store_bit}: {product['name']} -- ${product['price']}{rating_bit}\n"
        f"{product['url']}\n\n"
        "Do you want to buy this? Reply with an unequivocal \"yes\" to confirm -- "
        "anything else (including a hedged answer) will be treated as no."
    )

# Deliberately a small, exact set -- NOT an LLM judgment call. This gates an
# actual purchase, and a false positive here means real money gets spent, so
# only unambiguous affirmatives pass. A hedged, conditional, or unclear
# reply ("yeah I guess", "sure, but...", "maybe") is always treated as NOT
# confirmed rather than risk misreading it as a yes.
_CONFIRMATION_PHRASES = {
    "yes",
    "yes buy it",
    "yes, buy it",
    "confirm",
    "confirmed",
    "buy it",
    "purchase it",
    "yes purchase it",
    "yes please buy it",
    "do it",
    "i confirm",
    "yes i confirm",
}

def is_unequivocal_confirmation(reply_text):
    """Deterministically check whether a reply is an unambiguous purchase
    confirmation. See _CONFIRMATION_PHRASES for why this isn't an LLM call.

    Args:
        reply_text: The user's raw reply to a format_confirmation_prompt message.

    Returns:
        True only if the reply unambiguously confirms the purchase.
    """
    normalized = reply_text.strip().lower().rstrip(".!")
    return normalized in _CONFIRMATION_PHRASES

def open_browser_for_manual_login(timeout_seconds=180, store=DEFAULT_STORE):
    """Open a REAL, visible stealth browser window and let a human manually
    complete the store's login -- including any human-verification challenge
    (e.g. the "Press & hold to confirm you're a human" check). No automation
    attempts the login or the challenge itself; this only navigates to the
    login page and then waits, polling for a sign that a human has finished
    logging in.

    Args:
        timeout_seconds: How long to wait for manual login before giving up.
        store: Which store to log into ("amazon" or "target").

    Returns:
        (playwright, browser, page) -- the live, authenticated session, left
        open so the caller can continue with add-to-cart/checkout. The
        caller is responsible for eventually closing it (playwright.stop()).

    Raises:
        TimeoutError: if login wasn't completed within timeout_seconds.
    """
    cfg = STORES[store.lower()]
    playwright = sync_playwright().start()
    _log(f"launching visible browser for manual {store} login")
    browser, context, page = _launch_browser(playwright, headless=False)
    _goto(page, cfg["login_url"], "manual login page")

    login_path = cfg["login_url"].split("/", 3)[-1]
    print(
        f"Browser window opened -- please log in manually (including any "
        f"verification challenge). Waiting up to {timeout_seconds}s..."
    )

    start = time.time()
    while time.time() - start < timeout_seconds:
        if login_path not in page.url:
            _log(f"login detected after {int(time.time() - start)}s -- resuming automation")
            return playwright, browser, page
        time.sleep(2)

    _log(f"manual login timed out after {timeout_seconds}s -- closing browser")
    context.close()
    browser.close()
    playwright.stop()
    raise TimeoutError("Manual login was not completed within the timeout.")

# --- Amazon login / cart / checkout ------------------------------------------
#
# These steps operate on the shopper's OWN Amazon account using credentials
# read from a local amazon_credentials.json file. Nothing here is auto-run:
# you invoke them explicitly (see the __main__ block). The order is never
# submitted -- checkout_summary only reads the review page.

DEFAULT_CREDENTIALS_PATH = os.getenv(
    "AMAZON_CREDENTIALS", "credentials/users/owner/amazon_credentials.json"
)
SIGNIN_URL = "https://www.amazon.com/ap/signin"
_CAPTCHA_MARKERS = (
    "enter the characters you see below",
    "type the characters you see",
    "sorry, we just need to make sure you're not a robot",
    "api-services-support@amazon.com",
)
_OTP_MARKERS = (
    "two-step verification",
    "enter otp",
    "one time password",
    "authentication code",
    "verify your identity",
)

def load_credentials(path=DEFAULT_CREDENTIALS_PATH):
    """Read Amazon login credentials from a local JSON file.

    Expected shape (extra keys are ignored)::

        {"email": "you@example.com", "password": "..."}

    Args:
        path: Path to the credentials file.

    Returns:
        A dict with "email" and "password" keys.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if email or password is missing.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(
            f"Credentials file not found: {file_path}. Create it with "
            '{"email": "...", "password": "..."}.'
        )
    data = json.loads(file_path.read_text())
    email = data.get("email") or data.get("username")
    password = data.get("password")
    if not email or not password:
        raise ValueError(
            f"{file_path} must contain non-empty 'email' and 'password' keys."
        )
    return {"email": email, "password": password}

def is_logged_in(page):
    """Best-effort check of whether the current page shows a signed-in session."""
    try:
        if "/ap/signin" in (page.url or ""):
            return False
        nav = page.query_selector("#nav-link-accountList")
        if nav:
            text = nav.inner_text().lower()
            if "sign in" in text or "hello, sign in" in text:
                return False
            return True
        return "/ap/signin" not in (page.url or "")
    except Exception:  # noqa: BLE001
        return False

def _page_block_reason(page):
    """Return 'captcha', 'otp', or None based on what the page shows."""
    try:
        body = page.inner_text("body")[:5000].lower()
        title = (page.title() or "").lower()
    except Exception:  # noqa: BLE001
        return None
    if "captcha" in title or "robot check" in title:
        return "captcha"
    for marker in _CAPTCHA_MARKERS:
        if marker in body:
            return "captcha"
    for marker in _OTP_MARKERS:
        if marker in body:
            return "otp"
    return None

# --- Flow logging -------------------------------------------------------
#
# Every wait/navigation below goes through these so a run's console output
# shows exactly what step it's on and how long each pause was actually for
# -- e.g. "waiting 3500ms (checkout page to load)" -- instead of an
# unexplained gap that just looks like the browser is asleep or stuck.

def _log(msg):
    """Timestamped print for the browser-automation flow."""
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}")

def _wait(page, ms, reason):
    """page.wait_for_timeout, logged with what the wait is actually for."""
    _log(f"waiting {ms}ms ({reason})")
    page.wait_for_timeout(ms)

def _goto(page, url, reason, timeout=30000):
    """page.goto, logged with why we're navigating there."""
    _log(f"navigating to {url} ({reason})")
    page.goto(url, timeout=timeout)

def _click_first(page, selectors):
    """Click the first matching selector that exists; return True on success."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:  # noqa: BLE001
            el = None
        if el:
            try:
                el.click()
                return True
            except Exception:  # noqa: BLE001
                continue
    return False

def _first_visible(page, selectors):
    """Return the first element that exists *and* is visible, else None."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:  # noqa: BLE001
            el = None
        if el:
            try:
                if el.is_visible():
                    return el
            except Exception:  # noqa: BLE001
                continue
    return None

def _safe_fill(element, value):
    """Fill an input, returning False instead of raising if it isn't fillable."""
    try:
        element.fill(value)
        return True
    except Exception:  # noqa: BLE001
        return False

# Amazon serves a bare, standalone "Continue" page at several points in the
# sign-in flow -- e.g. after the email step, or right after the credentials are
# accepted. It's just an <a role="button"> that goes back to the homepage, and
# without clicking it the automation sits there forever because none of the
# sign-in selectors match. The markup (from a live page) is:
#
#   <span class="a-button a-button-primary">
#     <span class="a-button-inner">
#       <a href="https://www.amazon.com/" class="a-button-text" role="button">Continue</a>
#     </span>
#   </span>
#
# Matched on exact text "continue" (never class alone): the same a-button
# markup is used for consequential buttons like "Place your order", so a
# class-only match could click the wrong thing.
_CONTINUE_SELECTORS = (
    "span.a-button-primary a.a-button-text[role='button']",
    "span.a-button a.a-button-text[role='button']",
    "a.a-button-text[role='button']",
    "a[role='button']",
    "input[name='continue']",
)

def _click_continue_deterministic(page, timeout_ms=8000):
    """Tier-1 Continue handling: fixed selectors + exact-text match, polling
    for up to timeout_ms. No LLM. Returns True if a Continue was clicked.

    timeout_ms=0 does a single quick pass (no polling) -- cheap enough to call
    opportunistically whenever a step looks stuck.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    while True:
        for sel in _CONTINUE_SELECTORS:
            try:
                candidates = page.query_selector_all(sel) or []
            except Exception:  # noqa: BLE001
                candidates = []
            for el in candidates:
                try:
                    if not el.is_visible():
                        continue
                    label = " ".join(el.inner_text().split()).strip().lower()
                    if label != "continue":
                        continue
                    el.click()
                    _log(f"clicked standalone 'Continue' button (matched {sel!r})")
                    _wait(page, 2000, "page after Continue to settle")
                    return True
                except Exception:  # noqa: BLE001
                    continue

        # Fallback for markup drift: match by accessible role + exact name.
        try:
            loc = page.get_by_role(
                "button", name=re.compile(r"^\s*continue\s*$", re.IGNORECASE)
            )
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                _log("clicked 'Continue' (role=button text match)")
                _wait(page, 2000, "page after Continue to settle")
                return True
        except Exception:  # noqa: BLE001
            pass

        if time.time() >= deadline:
            return False
        page.wait_for_timeout(500)


def _click_continue_button(page, timeout_ms=8000, allow_llm=True):
    """Click a standalone 'Continue' button/link if one is showing.

    Polls for up to timeout_ms because the interstitial can render a moment
    after the page that triggered it. Returns True if a Continue control was
    clicked, False if none appeared.

    When the fixed selectors/text don't match -- the screen shows up at random
    with wording we've never seen -- it falls back to `_click_continue_with_llm`,
    which asks the model which single element advances past the screen. That
    fallback will only ever click a label on a fixed safe allow-list, so an
    unseen interstitial can be cleared autonomously without risking a purchase.

    Args:
        page: A live Playwright/Patchright page.
        timeout_ms: How long to keep looking for the button.
        allow_llm: Whether to fall back to the agent when selectors miss.

    Returns:
        True if a 'Continue' control was clicked.
    """
    if _click_continue_deterministic(page, timeout_ms):
        return True

    if allow_llm:
        _log("no deterministic Continue found -- asking the agent to identify it")
        if _click_continue_with_llm(page):
            return True

    _log(f"no 'Continue' button found within {timeout_ms}ms")
    return False


# The Continue screen appears at random points in Amazon's flow with wording
# that drifts, so a fixed selector list will always eventually miss. This is
# the autonomous fallback: ask the model which single visible element advances
# past the screen. It is deliberately NOT routed through improvise_navigate,
# which clicks whatever the model picks without validating the label -- during
# sign-in that could mean clicking "Place your order". Here the model's choice
# is only honored if its label is on a fixed safe allow-list.
_CONTINUE_LLM_GOAL = (
    "Advance past this informational Amazon interstitial by clicking its single "
    "obvious acknowledgement button -- typically labeled 'Continue' (also "
    "acceptable: 'Next', 'Go back', 'Back', 'Return to homepage', 'Home'). "
    "Never click anything that buys, orders, subscribes, starts a trial, or "
    "changes the cart."
)

# Exact labels the agent is allowed to click. Deliberately excludes anything
# ambiguous like "Proceed" (which could be "Proceed to checkout").
_CONTINUE_LLM_SAFE_LABELS = {
    "continue",
    "continue to amazon.com",
    "next",
    "go back",
    "back",
    "home",
    "return to homepage",
    "return to home page",
    "go to homepage",
    "ok",
    "okay",
    "got it",
    "dismiss",
    "close",
    "accept",
    "accept & continue",
    "accept and continue",
    "i understand",
    "no thanks",
    "not now",
}

# Hard refusal list -- if any of these appear in the candidate's label, it is
# never clicked, no matter what the model said.
_CONTINUE_LLM_FORBIDDEN_MARKERS = (
    "place your order",
    "place order",
    "buy now",
    "add to cart",
    "add to basket",
    "proceed to checkout",
    "checkout",
    "try prime",
    "start membership",
    "free trial",
    "subscribe",
    "sign up",
    "pay",
    "purchase",
    "confirm order",
    "cancel order",
    "delete",
)


def _is_safe_continue_label(label):
    """Whether a candidate element's label is safe to click as a Continue.

    Requires an exact match against _CONTINUE_LLM_SAFE_LABELS and no match
    against _CONTINUE_LLM_FORBIDDEN_MARKERS. An unseen screen can therefore be
    cleared autonomously, but nothing that could spend money ever is.
    """
    normalized = " ".join(label.split()).strip().lower()
    if not normalized:
        return False
    if any(marker in normalized for marker in _CONTINUE_LLM_FORBIDDEN_MARKERS):
        return False
    return normalized in _CONTINUE_LLM_SAFE_LABELS


def _click_continue_with_llm(page):
    """Agentic last resort for an unrecognized Continue screen: list the page's
    visible interactive elements, ask the model which single one advances past
    the interstitial, and click it -- but only if its label is on the fixed
    safe allow-list (see _is_safe_continue_label).

    Returns True if a safe element was clicked, False otherwise.
    """
    elements = _visible_interactive_elements(page, max_elements=25)
    if not elements:
        _log("continue(llm): no visible interactive elements to consider")
        return False

    _log(
        f"continue(llm): asking the model which of {len(elements)} element(s) "
        "advances past this screen"
    )
    done, index = _choose_interactive_element(_CONTINUE_LLM_GOAL, elements)
    if done:
        _log("continue(llm): model reports the screen is already passed")
        return True
    if index is None:
        _log("continue(llm): model found no element that safely advances")
        return False

    label, el = elements[index]
    if not _is_safe_continue_label(label):
        _log(f"continue(llm): refusing to click {label!r} -- not on the safe allow-list")
        return False

    try:
        el.click()
    except Exception as exc:  # noqa: BLE001
        _log(f"continue(llm): click on {label!r} failed ({exc})")
        return False
    _log(f"continue(llm): clicked {label!r} to advance past the screen")
    _wait(page, 2000, "page after agent-chosen Continue to settle")
    return True

DEBUG_DIR = "state/shopping/debug"

def _dump_debug(page, label):
    """Best-effort: save a full-page screenshot + the page's HTML whenever a
    scraping step hits an unrecognized screen (login, address book, etc.), so
    a failure is debuggable afterward instead of just "not found". Never
    raises -- this is diagnostics, not core logic, and must never be what
    breaks the caller.

    Args:
        page: The live Playwright/Patchright page to capture.
        label: Short slug describing where the failure happened, e.g.
            "no-password-field".
    """
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.join(DEBUG_DIR, f"{stamp}-{label}")
        page.screenshot(path=f"{base}.png", full_page=True)
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        print(
            f"[debug] saved {base}.png / .html "
            f"(url={page.url!r}, title={page.title()!r})"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[login debug] could not capture debug info: {exc}")

def _page_fingerprint(page):
    """A cheap signature of the current page: url + a short hash of the body
    text. Used to tell whether an action (a Continue click, a retry) actually
    changed anything, or we're stuck on the same screen."""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        url = ""
    try:
        body = page.inner_text("body")[:3000]
    except Exception:  # noqa: BLE001
        body = ""
    return f"{url}::{hash(body)}"

def _recover_stuck_page(page, reason, allow_llm=True):
    """Generic 'we're stuck on an unexpected screen' recovery. Tries a
    standalone Continue (deterministic first, then the agentic fallback), then
    a Prime-upsell decline, then improvise_navigate. Returns True if the page
    looks different afterwards (i.e. something moved it forward).

    Called after any step that didn't reach its expected end state, so a
    random interstitial appearing anywhere in the flow gets cleared instead of
    silently stalling. Nothing here can place an order: the Continue path is
    allow-listed, and improvise_navigate is only ever handed navigation goals.

    Args:
        page: The live page that looks stuck.
        reason: Short description of what we were waiting for, for the log.
        allow_llm: Whether the Continue fallback may consult the model.

    Returns:
        True if the page changed after recovery attempts.
    """
    before = _page_fingerprint(page)
    _log(f"recovering from an unexpected screen while {reason}")

    acted = False
    if _click_continue_button(page, timeout_ms=0, allow_llm=allow_llm):
        acted = True
    elif _dismiss_prime_upsell(page, attempts=1):
        acted = True

    if _page_fingerprint(page) != before:
        return True

    # Nothing deterministic moved the page -- let the improviser try, then
    # treat any click it made as recovery so the caller re-checks its
    # expectation (the page may have advanced without a visible url change).
    if improvise_navigate(
        page,
        "Move past this unexpected Amazon screen toward continuing the current "
        "shopping flow. Never click anything that buys, orders, subscribes, "
        "starts a trial, or changes the cart.",
        max_steps=2,
    ):
        acted = True
    return acted or _page_fingerprint(page) != before

def login_amazon(page, credentials, interactive_timeout=180):
    """Log into Amazon using stored credentials if not already signed in.

    Handles both sign-in variants Amazon serves: the two-step email-then-
    password flow and the combined form where both fields appear at once. If
    Amazon interposes a CAPTCHA or a two-step verification (OTP) challenge,
    control is handed to the human -- the browser should be visible (run with
    SHOP_HEADLESS=false) -- and we poll until the challenge clears, since
    neither can be solved automatically.

    Args:
        page: A live Playwright/Patchright page.
        credentials: Dict with "email" and "password" (see load_credentials).
        interactive_timeout: Seconds to wait for a human to clear a challenge.

    Returns:
        True if the session ends up logged in, else False.
    """
    _goto(page, "https://www.amazon.com/", "check existing session")
    _wait(page, 1500, "homepage to render")

    if is_logged_in(page):
        print("Already signed in to Amazon.")
        return True

    link = page.query_selector("a[data-nav-role='signin']") or page.query_selector(
        "#nav-link-accountList a"
    )
    href = link.get_attribute("href") if link else SIGNIN_URL
    _goto(page, href, "sign-in page")
    _wait(page, 2000, "sign-in page to render")

    print(f"Signing in as {credentials['email']} ...")
    email_field = _first_visible(
        page, ["#ap_email_login", "input[name='email']", "input[type='email']"]
    )
    if not email_field:
        # An interstitial can stand in for the sign-in page -- clear it once
        # and look again before giving up.
        _log("email field not found -- checking for an interstitial to clear")
        if _recover_stuck_page(page, "finding the sign-in email field"):
            email_field = _first_visible(
                page, ["#ap_email_login", "input[name='email']", "input[type='email']"]
            )
    if not email_field:
        print(f"Could not find the email field (page block: {_page_block_reason(page)}).")
        _dump_debug(page, "no-email-field")
        return False

    # Amazon's password field ID/name has drifted across A/B tests before, so
    # the generic input[type='password'] fallback matters -- it's what still
    # matches after Amazon renames #ap_password out from under us.
    _PASSWORD_SELECTORS = ["#ap_password", "input[name='password']", "input[type='password']"]

    # Combined form: email + password on one page.
    password_field = _first_visible(page, _PASSWORD_SELECTORS)
    if password_field:
        _log("combined email+password form detected")
        if not _safe_fill(email_field, credentials["email"]):
            print("Could not fill the email field.")
            _dump_debug(page, "email-fill-failed")
            return False
        _safe_fill(password_field, credentials["password"])
        _click_first(page, ["#signInSubmit", "input#signInSubmit"])
    else:
        # Two-step form: email first, then password.
        _log("two-step email-then-password form detected")
        if not _safe_fill(email_field, credentials["email"]):
            print("Could not fill the email field.")
            _dump_debug(page, "email-fill-failed")
            return False
        _click_first(page, ["#continue", "input#continue", "button[type='submit']"])
        _log("waiting up to 15000ms for the password field to appear")
        try:
            page.wait_for_selector(
                "#ap_password, input[name='password'], input[type='password'], "
                "#auth-error-message-box, #auth-warning-message-box",
                timeout=15000,
            )
        except Exception:  # noqa: BLE001
            _log("timed out waiting for the password field selector")
        _wait(page, 1000, "email-step response to settle")

        if _page_block_reason(page):
            _log("captcha/otp block detected after email step")
            return _wait_for_human(page, interactive_timeout)

        error = _auth_error(page)
        if error:
            print(f"Amazon rejected the email step: {error}")
            _dump_debug(page, "email-step-rejected")
            return False

        password_field = _first_visible(page, _PASSWORD_SELECTORS)
        if not password_field:
            # One retry after a longer wait -- covers slow-rendering pages
            # that weren't ready when wait_for_selector above gave up.
            _wait(page, 2000, "retry: slow-rendering password field")
            password_field = _first_visible(page, _PASSWORD_SELECTORS)
        if not password_field:
            # Amazon sometimes shows a bare "Continue" interstitial instead of
            # a password field here. Click through it, then re-check: it may
            # lead to the password page, or straight to a signed-in session.
            _log("no password field -- checking for a standalone 'Continue' page")
            if _click_continue_button(page):
                if _page_block_reason(page):
                    return _wait_for_human(page, interactive_timeout)
                if is_logged_in(page):
                    print("Signed in to Amazon.")
                    return True
                password_field = _first_visible(page, _PASSWORD_SELECTORS)
        if not password_field:
            print("Password field not found; sign-in may require another step.")
            _dump_debug(page, "no-password-field")
            return False
        _safe_fill(password_field, credentials["password"])
        _click_first(page, ["#signInSubmit", "input#signInSubmit"])

    _wait(page, 3500, "sign-in submission to complete")

    if _page_block_reason(page):
        _log("captcha/otp block detected after sign-in submission")
        return _wait_for_human(page, interactive_timeout)

    error = _auth_error(page)
    if error:
        print(f"Sign-in failed: {error}")
        _dump_debug(page, "sign-in-failed")
        return False

    if is_logged_in(page):
        print("Signed in to Amazon.")
        return True

    # A bare "Continue" page can also appear right after the credentials are
    # accepted (Amazon shows it before landing on the signed-in homepage).
    # Click through it, then re-check.
    _log("not signed in yet -- checking for a standalone 'Continue' page")
    if _click_continue_button(page):
        if _page_block_reason(page):
            return _wait_for_human(page, interactive_timeout)
        if is_logged_in(page):
            print("Signed in to Amazon.")
            return True

    print("Sign-in did not complete (check credentials, or solve the challenge).")
    _dump_debug(page, "sign-in-incomplete")
    return False

def _auth_error(page):
    """Return Amazon's sign-in error text if the page shows one, else None."""
    for sel in ("#auth-error-message-box", "#auth-warning-message-box", ".a-alert-error"):
        el = page.query_selector(sel)
        if el:
            text = " ".join(el.inner_text().split())
            if text:
                return text[:200]
    return None

def _wait_for_human(page, interactive_timeout):
    """Poll until a CAPTCHA/OTP challenge is cleared by a human."""
    print("Amazon presented a CAPTCHA / verification challenge.")
    print(f"Solve it manually in the browser window; waiting up to {interactive_timeout}s ...")
    start = time.time()
    while time.time() - start < interactive_timeout:
        if is_logged_in(page) and not _page_block_reason(page):
            _log(f"challenge cleared after {int(time.time() - start)}s -- signed in")
            return True
        time.sleep(2)
    _log(f"challenge not cleared within {interactive_timeout}s -- giving up")
    return is_logged_in(page)

# Amazon sometimes interrupts the flow (product page, cart, checkout) with a
# full Prime-membership upsell interstitial. Deterministic, not routed
# through improvise_navigate: a wrong click here could start a paid trial,
# not just pick the wrong price option, so this only ever clicks a known
# decline control -- the real id (found on a live page) first, a fixed
# allow-list of unambiguous "no" text as a fallback, and NEVER anything
# matching "try prime" / "free trial" / "start membership".
_PRIME_DECLINE_SELECTORS = ("#prime-decline-button",)
_PRIME_DECLINE_TEXT_PATTERNS = (
    "no thanks",
    "not now",
    "skip",
    "continue without prime",
)

_DISMISS_TEXT_MATCH_JS = """(patterns) => {
    const els = document.querySelectorAll('a, button, [role="button"], span');
    for (const el of els) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        const text = (el.innerText || el.textContent || '').trim().toLowerCase();
        if (!text || text.length > 40) continue;
        for (const p of patterns) {
            if (text === p || text.startsWith(p)) {
                el.click();
                return text;
            }
        }
    }
    return null;
}"""

def _dismiss_prime_upsell(page, attempts=3):
    """Decline a Prime membership upsell interstitial if one is showing.

    #prime-decline-button is a real <a href> link -- clicking it triggers a
    full page navigation, not an in-place DOM change -- and Amazon can also
    render/redirect to this interstitial a little after the page that
    triggered it settles. So this polls a few times with short waits between
    (in case the button isn't there yet) and waits for the page to finish
    loading after a click (since a fixed short wait isn't enough for a real
    navigation), rather than checking exactly once.

    The text-pattern fallback runs as a single page.evaluate() -- one round
    trip into the browser that scans the DOM in JS and clicks a match
    in-place -- rather than one page.get_by_text(regex).count() call per
    pattern. The old per-pattern version meant walking Amazon's entire DOM
    via the accessibility tree up to 4 times per attempt, which on a real
    (large, carousel-heavy) Amazon page measured ~30s per attempt -- turning
    a 3-attempt poll meant to cost ~2.4s into 90+ seconds of dead time.

    Returns True if something was dismissed, False if there was nothing to
    dismiss across all attempts (the common case).
    """
    for attempt in range(1, attempts + 1):
        clicked = _click_first(page, _PRIME_DECLINE_SELECTORS)
        matched_via = "id" if clicked else None
        if not clicked:
            try:
                matched_text = page.evaluate(_DISMISS_TEXT_MATCH_JS, list(_PRIME_DECLINE_TEXT_PATTERNS))
            except Exception:  # noqa: BLE001
                matched_text = None
            if matched_text:
                clicked = True
                matched_via = f"text {matched_text!r}"

        if clicked:
            _log(f"dismissed Prime upsell via {matched_via} (attempt {attempt}/{attempts})")
            try:
                page.wait_for_load_state("load", timeout=8000)
            except Exception:  # noqa: BLE001
                _log("wait_for_load_state timed out after dismissing Prime upsell")
                page.wait_for_timeout(1500)
            return True

        _log(f"no Prime upsell found (attempt {attempt}/{attempts})")
        page.wait_for_timeout(800)
    return False

_ADD_TO_CART_SELECTORS = (
    "#add-to-cart-button",
    "input[name='submit.add-to-cart']",
    "button[name='submit.add-to-cart']",
)

# Some listings gate Add to Cart behind a buy-box price/offer picker (radio
# swatches like "Regular price" vs "Deal price") that must be selected first.
# Prefer a plain regular price deterministically -- a "deal" can carry
# conditions (coupon clip, subscription, minimum quantity) the shopper never
# asked for -- and only fall back to whatever's offered if there's no plain
# option.
_BUYING_OPTION_PREFERENCE = ("regular price", "one-time purchase", "buy new", "deal price")

def _element_label(page, el):
    """Best-effort human-readable label for any interactive element: its own
    visible text first (buttons, links), then aria attributes, then a
    value/placeholder, then -- for bare inputs with no text of their own,
    like radio swatches -- the nearest label-ish ancestor.
    """
    try:
        text = el.inner_text().strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass
    try:
        label_id = el.get_attribute("aria-labelledby")
        if label_id:
            texts = [
                lab.inner_text()
                for lid in label_id.split()
                if (lab := page.query_selector(f"#{lid}"))
            ]
            if texts:
                return " ".join(texts)
        aria = el.get_attribute("aria-label")
        if aria:
            return aria
        value = el.get_attribute("value") or el.get_attribute("placeholder")
        if value:
            return value
        return el.evaluate(
            "el => (el.closest('label') || el.closest('li') || el.parentElement)?.innerText || ''"
        )
    except Exception:  # noqa: BLE001
        return ""

def _select_buying_option(page):
    """Pick a buy-box price/offer option per _BUYING_OPTION_PREFERENCE, if the
    page is showing one. Returns True if an option was clicked.
    """
    container = (
        page.query_selector("#buybox")
        or page.query_selector("#desktop_buybox")
        or page.query_selector("#dp-container")
    )
    if not container:
        _log("no buy-box container found -- nothing to pick an option from")
        return False

    options = []
    for el in container.query_selector_all("input[type='radio'], [role='radio']"):
        try:
            if not el.is_visible():
                continue
        except Exception:  # noqa: BLE001
            continue
        options.append((_element_label(page, el).strip().lower(), el))
    if not options:
        _log("buy-box container found but no visible radio/swatch options in it")
        return False
    _log(f"buy-box options found: {[label for label, _el in options]}")

    # Never fall back into a "used" condition (e.g. "Used - Like New") -- the
    # shopper asked for the product, not whatever used listing happens to be
    # first. Only consider used options if that's genuinely all there is.
    new_condition_options = [(label, el) for label, el in options if "used" not in label]
    candidates = new_condition_options or options

    for preferred in _BUYING_OPTION_PREFERENCE:
        for label, el in candidates:
            if preferred in label:
                try:
                    el.click()
                    _log(f"selected buy-box option {label!r} (matched preference {preferred!r})")
                    page.wait_for_timeout(800)
                    return True
                except Exception:  # noqa: BLE001
                    continue

    # No recognized label -- fall back to the first (non-used) option rather
    # than leaving Add to Cart permanently hidden.
    try:
        candidates[0][1].click()
        _log(f"selected buy-box option {candidates[0][0]!r} (no preferred label matched)")
        page.wait_for_timeout(800)
        return True
    except Exception:  # noqa: BLE001
        return False

# --- Improvised navigation ---------------------------------------------------
#
# Everything above (login, buy-box option picking, address extraction) is
# deterministic: fixed selectors, fixed regexes, fixed preference orders. That
# breaks the moment Amazon's markup drifts, and we can't hand-write a
# selector for every popup/interstitial/unfamiliar picker in advance.
#
# improvise_navigate is a fallback of last resort for exactly that case: it
# lists whatever's actually clickable on the page right now, asks the LLM
# which one (if any) moves toward a short natural-language goal, clicks it,
# and repeats up to a few times. It is ONLY for getting unstuck while
# browsing/navigating -- nothing here ever touches place_order or any
# purchase confirmation, which stay fully deterministic and gated by
# is_unequivocal_confirmation / PLACE_ORDERS_ENABLED regardless.

_INTERACTIVE_SELECTOR = (
    "button, a[href], input[type='radio'], input[type='checkbox'], "
    "input[type='submit'], input[type='button'], [role='button'], "
    "[role='radio'], [role='link'], select"
)

_IMPROVISE_SYSTEM_PROMPT = """You are helping navigate a live webpage toward a goal, one click at \
a time. You'll be given the goal and a numbered list of the page's currently visible \
clickable/selectable elements (their text or role).

Respond with ONLY a JSON object with exactly these keys:
- done: true if the goal already looks satisfied given the context (most turns this is false)
- index: the number of the single best element to click next to make progress toward the goal, \
or null if none of the listed elements would help (e.g. this looks like the wrong page entirely, \
or a genuine dead end)
- reason: one short sentence explaining the choice
"""

def _visible_interactive_elements(page, container_selector=None, max_elements=25):
    """Collect visible clickable/selectable elements (optionally scoped to
    container_selector) as (label, element_handle) pairs, for presenting to
    the LLM as a numbered menu of possible next actions. Deduplicated by
    label and capped so the prompt stays small.
    """
    root = page
    if container_selector:
        root = page.query_selector(container_selector) or page

    elements = []
    seen_labels = set()
    for el in root.query_selector_all(_INTERACTIVE_SELECTOR):
        if len(elements) >= max_elements:
            break
        try:
            if not el.is_visible():
                continue
        except Exception:  # noqa: BLE001
            continue
        label = " ".join(_element_label(page, el).split())[:120]
        if not label or label.lower() in seen_labels:
            continue
        seen_labels.add(label.lower())
        elements.append((label, el))
    return elements

def _choose_interactive_element(goal, elements):
    """Ask the LLM to pick the best next element to click toward `goal` from
    the numbered `elements` list (see _visible_interactive_elements).

    Returns (done, index) -- index is None if nothing listed would help.
    """
    if not elements:
        return False, None

    listing = "\n".join(f"{i}: {label}" for i, (label, _el) in enumerate(elements))
    listing_block = wrap_untrusted("scraped page elements", listing)
    user_content = f"Goal: {goal}\n\nVisible elements:\n{listing_block}"

    try:
        result = chat_json(_IMPROVISE_SYSTEM_PROMPT, user_content, timeout=60)
    except Exception as exc:  # noqa: BLE001
        _log(f"improvise: Ollama call failed ({exc})")
        return False, None

    index = result.get("index")
    if not isinstance(index, int) or not (0 <= index < len(elements)):
        index = None
    _log(f"improvise: model reason={result.get('reason')!r} done={result.get('done')} index={index}")
    return bool(result.get("done")), index

def improvise_navigate(page, goal, container_selector=None, max_steps=5):
    """Fallback navigation for when deterministic selectors miss: at each
    step, list the page's visible interactive elements, ask the LLM which one
    (if any) moves toward `goal`, click it, and repeat. Never used for
    anything that spends money -- see the module note above.

    Args:
        page: A live, signed-in Playwright/Patchright page.
        goal: A short natural-language description of what we're trying to
            accomplish (e.g. "make the Add to Cart button appear or become
            clickable").
        container_selector: Optional CSS selector to scope the element search
            to (e.g. "#buybox") so the model isn't distracted by unrelated
            page chrome. Falls back to the whole page if not found.
        max_steps: How many click attempts to try before giving up.

    Returns:
        True if the model reported the goal satisfied, False if it ran out of
        steps or found nothing useful to click.
    """
    _log(f"improvise_navigate starting: goal={goal!r}, scope={container_selector!r}")
    for step in range(1, max_steps + 1):
        elements = _visible_interactive_elements(page, container_selector)
        if not elements:
            _log(f"improvise step {step}/{max_steps}: no visible interactive elements found")
            _dump_debug(page, "improvise-no-elements")
            return False
        _log(f"improvise step {step}/{max_steps}: {len(elements)} candidate element(s), asking LLM")

        done, index = _choose_interactive_element(goal, elements)
        if done:
            _log(f"improvise step {step}/{max_steps}: model reports goal already satisfied")
            return True
        if index is None:
            _log(f"improvise step {step}/{max_steps}: model found nothing useful to click")
            _dump_debug(page, "improvise-no-good-element")
            return False

        label, el = elements[index]
        try:
            el.click()
        except Exception as exc:  # noqa: BLE001
            _log(f"improvise step {step}/{max_steps}: click on {label!r} failed ({exc})")
            return False
        _log(f"improvise step {step}/{max_steps}: clicked {label!r} toward {goal!r}")
        _wait(page, 5000, "page to react to improvised click")

    _log(f"improvise_navigate giving up after {max_steps} step(s)")
    return False

def add_to_cart(page, product_url, quantity=1):
    """Add a product to the Amazon cart. Assumes the session is signed in.

    Args:
        page: A live, signed-in Playwright/Patchright page.
        product_url: The product page URL (or a bare ASIN).
        quantity: How many to add.

    Returns:
        True if the cart confirmation was detected.
    """
    url = product_url
    if not url.startswith("http"):
        url = f"https://www.amazon.com/dp/{url}"
    _goto(page, url, "product page")
    _wait(page, 2000, "product page to render")
    _dismiss_prime_upsell(page)

    if quantity and quantity > 1:
        try:
            page.select_option("#quantity", str(quantity))
            _wait(page, 500, "quantity selector to apply")
        except Exception:  # noqa: BLE001
            pass

    if not _first_visible(page, _ADD_TO_CART_SELECTORS):
        _log("Add to Cart not visible yet -- checking for a buy-box option picker")
        if _select_buying_option(page):
            _wait(page, 500, "buy-box option selection to apply")

    # Deterministic option-picking above covers known buy-box pickers. If Add
    # to Cart is still hidden, this is something we haven't seen before
    # (unfamiliar picker, interstitial, popup) -- let the improviser take a
    # few clicks at it before giving up.
    if not _first_visible(page, _ADD_TO_CART_SELECTORS):
        _log("Add to Cart still not visible -- handing off to improvise_navigate")
        improvise_navigate(
            page,
            "Make the 'Add to Cart' button appear or become clickable",
            container_selector="#buybox",
        )

    if not _click_first(page, _ADD_TO_CART_SELECTORS):
        # The button may be hidden behind an interstitial (Continue screen,
        # consent prompt) rather than actually missing -- try to clear it once.
        _log("Add to Cart not found -- checking for an interstitial to clear")
        if _recover_stuck_page(page, "adding to cart"):
            _dismiss_prime_upsell(page)
            if not _first_visible(page, _ADD_TO_CART_SELECTORS):
                _select_buying_option(page)
                _wait(page, 500, "buy-box option selection to apply")
        if not _click_first(page, _ADD_TO_CART_SELECTORS):
            print("Could not find an 'Add to Cart' button (options or sign-in needed).")
            _dump_debug(page, "add-to-cart-not-found")
            return False
    _log("clicked Add to Cart")
    _wait(page, 2500, "add-to-cart confirmation to render")
    _dismiss_prime_upsell(page)

    for sel in (
        "#NATC_SMART_WAGON_CONF_MSG_SUCCESS",
        "#huc-v2-order-row-confirm-text",
        "#sw-atc-confirmation",
    ):
        if page.query_selector(sel):
            print(f"Added to cart: {url}")
            return True
    try:
        if "added to cart" in page.inner_text("body").lower():
            print(f"Added to cart: {url}")
            return True
    except Exception:  # noqa: BLE001
        pass
    # No confirmation on screen -- an interstitial may be covering it.
    _log("no add-to-cart confirmation yet -- checking for an interstitial to clear")
    if _recover_stuck_page(page, "confirming the add-to-cart"):
        for sel in (
            "#NATC_SMART_WAGON_CONF_MSG_SUCCESS",
            "#huc-v2-order-row-confirm-text",
            "#sw-atc-confirmation",
        ):
            if page.query_selector(sel):
                print(f"Added to cart: {url}")
                return True
        try:
            if "added to cart" in page.inner_text("body").lower():
                print(f"Added to cart: {url}")
                return True
        except Exception:  # noqa: BLE001
            pass
    print("Clicked 'Add to Cart' but no confirmation was detected.")
    _dump_debug(page, "add-to-cart-no-confirmation")
    return False

ADDRESS_BOOK_URL = "https://www.amazon.com/a/addresses"
ADDRESS_CACHE_PATH = os.path.join("state", "shopping", "default_address.json")

_PROCEED_TO_CHECKOUT_SELECTORS = (
    "input[name='proceedToRetailCheckout']",
    "button[name='proceedToRetailCheckout']",
    "a[href*='/gp/buy/']",
)

def _load_cached_address():
    """Return the last address we successfully fetched, or None."""
    if not os.path.exists(ADDRESS_CACHE_PATH) or os.path.getsize(ADDRESS_CACHE_PATH) == 0:
        return None
    try:
        with open(ADDRESS_CACHE_PATH) as f:
            return json.load(f).get("address")
    except Exception:  # noqa: BLE001
        return None

def _save_cached_address(address):
    """Persist a successfully-fetched address as a fallback for future runs.
    Only ever written from a live fetch that actually found something -- this
    is a fallback for when scraping fails, never a substitute for it, so a
    changed real-world address always overwrites it on the next live read.
    """
    os.makedirs(os.path.dirname(ADDRESS_CACHE_PATH), exist_ok=True)
    with open(ADDRESS_CACHE_PATH, "w") as f:
        json.dump(
            {"address": address, "fetched_at": datetime.datetime.now().isoformat()},
            f,
            indent=2,
        )

def get_default_address(page):
    """Fetch the account's default shipping address directly from Amazon's
    address book page. Unlike the checkout review page, this page exists
    only to list addresses, so its markup is far less likely to have shifted
    out from under known selectors -- a second, independent way to get the
    address when the checkout-page scrape misses.

    Args:
        page: A live, signed-in Playwright/Patchright page. Navigates away
            from wherever the page currently is.

    Returns:
        The address as a single cleaned string, or None if it couldn't be
        found (e.g. the account has no saved addresses).
    """
    try:
        _goto(page, ADDRESS_BOOK_URL, "address book page")
        _wait(page, 2500, "address book page to render")
    except Exception as exc:  # noqa: BLE001
        _log(f"navigation to address book page failed ({exc})")
        return None

    if _page_block_reason(page):
        print("Address book page blocked (captcha/otp) -- can't read the address this way.")
        return None

    # Amazon renders each field of the default address as its own id'd
    # widget -- verified against a live page dump (see
    # state/shopping/debug/*-address-book-no-entries.html) -- so read those
    # directly rather than guessing at a container element for "an entry".
    parts = []
    for sel in (
        "#address-ui-widgets-FullName",
        "#address-ui-widgets-AddressLineOne",
        "#address-ui-widgets-AddressLineTwo",
        "#address-ui-widgets-CityStatePostalCode",
        "#address-ui-widgets-Country",
    ):
        el = page.query_selector(sel)
        if el:
            text = " ".join(el.inner_text().split())
            if text:
                parts.append(text)
    if parts:
        _log(f"address book: found address via address-ui-widgets ids ({len(parts)} field(s))")
        return ", ".join(parts)[:400]
    _log("address book: address-ui-widgets ids didn't match, falling back to tile scan")

    # Fallback for markup that's drifted since: scan each address tile for
    # whichever is marked "Default", else take the first.
    entries = page.query_selector_all(
        "div[id^='ya-myab-display-address-block-'], .address-book-entry, "
        "[data-testid='address-book-entry']"
    )
    if not entries:
        # An interstitial (Continue screen, consent prompt) can stand in for
        # the address book page -- clear it and re-scan before giving up.
        _log("no address-book entries -- checking for an interstitial to clear")
        if _recover_stuck_page(page, "reading the address book"):
            entries = page.query_selector_all(
                "div[id^='ya-myab-display-address-block-'], .address-book-entry, "
                "[data-testid='address-book-entry']"
            )
    if not entries:
        print(f"No address-book entries matched known selectors (url={page.url!r}).")
        _dump_debug(page, "address-book-no-entries")
        return None

    chosen = None
    for el in entries:
        try:
            text = el.inner_text()
        except Exception:  # noqa: BLE001
            continue
        if re.search(r"\bdefault\b", text, re.IGNORECASE):
            chosen = el
            break
    chosen = chosen or entries[0]

    try:
        text = " ".join(chosen.inner_text().split())
    except Exception:  # noqa: BLE001
        return None
    return text[:400] or None

def _extract_checkout_address(page, body_text):
    """Try every known way of reading the checkout page's shipping address:
    the verified widget ids first, then older guessed selectors, then two
    regex fallbacks over the raw page text. Returns None if nothing matched.
    """
    # The real checkout page (verified against a live dump -- see
    # state/shopping/debug/*-checkout-no-address.html) shows the address as
    # "Delivering to <name>" / "<street, city, state zip, country>" in these
    # two ids, not any of the guessed selectors below.
    name_el = page.query_selector("#deliver-to-customer-text")
    addr_el = page.query_selector("#deliver-to-address-text")
    if addr_el:
        parts = []
        if name_el:
            name_text = re.sub(
                r"^\s*Delivering to\s*", "", name_el.inner_text(), flags=re.IGNORECASE
            ).strip()
            if name_text:
                parts.append(name_text)
        addr_text = " ".join(addr_el.inner_text().split())
        if addr_text:
            parts.append(addr_text)
        if parts:
            return ", ".join(parts)[:400]

    for sel in (
        "[id^='address-book-entry'] .a-box-inner",
        "[data-testid='shipping-address']",
        "#ship-to-this-address",
        "#address-book-entry-0",
        "#shipToInsertionNode",
        "[data-testid='address-book-entry']",
        ".checkout-address-widget",
        "#shipping-address-box",
    ):
        el = page.query_selector(sel)
        if el:
            text = " ".join(el.inner_text().split())
            if text:
                return text[:400]

    m = re.search(
        r"(?:Shipping Address|Ship(?:ping)? to|Deliver(?:ing)? to)\s*[:\n]\s*(.+?)"
        r"(?:\n\s*\n|\bChange\b|\bEdit\b|$)",
        body_text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        text = " ".join(m.group(1).split())
        if text:
            return text[:400]

    # Last resort: a raw "123 Main St, Anytown, CA 12345"-shaped line, found
    # without relying on any heading text at all.
    m = re.search(
        r"\d{1,6}\s+[A-Za-z0-9.\-' ]+,?\s*[A-Za-z .\-]+,\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?",
        body_text,
    )
    if m:
        return m.group(0).strip()[:400]

    return None

def checkout_summary(page):
    """Open the checkout review page and read the shipping address, shipping
    time, and order total including tax. The order is NEVER placed.

    Args:
        page: A live, signed-in Playwright/Patchright page with items in cart.

    Returns:
        A dict with address, shipping_time, estimated_tax, order_total, and
        order_placed (always False).
    """
    _goto(page, "https://www.amazon.com/gp/cart/view.html", "cart page")
    _wait(page, 2000, "cart page to render")
    _dismiss_prime_upsell(page)

    if not _click_first(page, _PROCEED_TO_CHECKOUT_SELECTORS):
        # A Continue screen / consent prompt on the cart page can hide the
        # Proceed button -- clear it once and look again.
        _log("'Proceed to checkout' not found -- checking for an interstitial to clear")
        if _recover_stuck_page(page, "finding 'Proceed to checkout'"):
            _dismiss_prime_upsell(page)
        if not _click_first(page, _PROCEED_TO_CHECKOUT_SELECTORS):
            print("Could not find 'Proceed to checkout' (cart empty or not signed in).")
            _dump_debug(page, "proceed-to-checkout-not-found")
            return None
    _log("clicked 'Proceed to checkout'")
    _wait(page, 3500, "checkout page to load")
    _dismiss_prime_upsell(page)

    summary_text = ""
    for sel in (
        "#subtotals-marketplace-table",
        "[data-testid='order-summary']",
        "#orderSummary",
        "#checkout-summary",
    ):
        el = page.query_selector(sel)
        if el:
            summary_text = el.inner_text()
            break
    if not summary_text:
        try:
            summary_text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            summary_text = ""

    # Amazon's checkout markup (ids/data-testids) drifts across A/B tests far
    # more often than the visible page text, so both the address and shipping
    # time fall back to regex over the full body text when no known selector
    # matches, rather than just giving up.
    try:
        body_text = page.inner_text("body")
    except Exception:  # noqa: BLE001
        body_text = summary_text

    address = _extract_checkout_address(page, body_text)
    if address:
        _log("address found via on-page checkout selectors/regex")

    if not address and _dismiss_prime_upsell(page):
        # A Prime upsell was apparently still showing (or appeared) after the
        # earlier checks -- now that it's dismissed, re-read whatever page we
        # land on before giving up.
        _wait(page, 1000, "page after late Prime-upsell dismissal")
        try:
            body_text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            pass
        address = _extract_checkout_address(page, body_text)
        if address:
            _log("address found after a late Prime-upsell dismissal")

    if address:
        _save_cached_address(address)
    else:
        # The checkout-page selectors/regex above are a guess too -- capture
        # what the page actually looked like before moving on.
        _log("on-page address extraction failed -- trying the address book page")
        _dump_debug(page, "checkout-no-address")
        # Try the address book page directly, then come back to checkout so
        # callers (e.g. place_order) still find the page sitting on the
        # review screen as expected.
        address = get_default_address(page)
        if address:
            _log("address found via the address book page")
            _save_cached_address(address)
        _log("returning to checkout after address book detour")
        _goto(page, "https://www.amazon.com/gp/cart/view.html", "cart page (returning)")
        _wait(page, 2000, "cart page to render")
        _click_first(page, _PROCEED_TO_CHECKOUT_SELECTORS)
        _wait(page, 3500, "checkout page to reload")

    if not address:
        address = _load_cached_address()
        if address:
            _log("address book also failed -- using cached address from a previous run")
        else:
            _log("no address found on-page, via address book, or in cache")

    shipping_time = None
    eta = page.query_selector("[data-testid='delivery-promise']") or page.query_selector(
        "#delivery-promise"
    )
    if eta:
        shipping_time = " ".join(eta.inner_text().split())[:200]
    if not shipping_time:
        m = re.search(r"(Arriving[^\n]*|Delivery[^\n]*|Get it[^\n]*)", body_text)
        shipping_time = m.group(1).strip() if m else None

    def _amount(labels):
        for label in labels:
            m = re.search(
                rf"{label}\s*:?\s*\$?\s*([\d,]+\.\d{{2}})",
                summary_text,
                re.IGNORECASE,
            )
            if m:
                return float(m.group(1).replace(",", ""))
        return None

    return {
        "address": address,
        "shipping_time": shipping_time,
        "estimated_tax": _amount(("estimated tax", "sales tax", "tax")),
        "order_total": _amount(("order total", "total")),
        "order_placed": False,
    }

def format_checkout_summary(summary):
    """Render a checkout_summary dict as a message for the user."""
    if not summary:
        return "Could not read the checkout summary."
    money = lambda v: f"${v:.2f}" if v is not None else "not found"  # noqa: E731
    return (
        f"Shipping address: {summary.get('address') or 'not found'}\n"
        f"Shipping time: {summary.get('shipping_time') or 'not found'}\n"
        f"Order total (incl. tax): {money(summary.get('order_total'))}\n"
        "Order NOT placed (read-only review page)."
    )

def format_purchase_confirmation(product, checkout=None):
    """Build the message asking the user to confirm an actual purchase --
    item, price after tax, shipping time, and shipping address, in that
    order, with the yes/no ask LAST (after the user has every number, not
    before it). Never proceed to place_order without a reply to this passing
    is_unequivocal_confirmation.

    Args:
        product: A single product dict (name, price, rating, review_count,
            url, store).
        checkout: Optional checkout-summary dict from find_and_prepare_purchase
            (address, shipping_time, estimated_tax, order_total). When
            missing (e.g. login/cart failed), falls back to the product's
            pre-tax listing price and omits shipping time/address.

    Returns:
        A message string to send the user.
    """
    store_bit = f" [{product['store']}]" if product.get("store") else ""
    fields = [f"Item: {product.get('name')}{store_bit}"]

    if checkout and checkout.get("order_total") is not None:
        fields.append(f"Price (incl. tax + shipping): ${checkout['order_total']:.2f}")
    elif product.get("price") is not None:
        fields.append(f"Price: ${product['price']} (tax/shipping not confirmed)")

    if checkout and checkout.get("shipping_time"):
        fields.append(f"Shipping time: {checkout['shipping_time']}")
    if checkout and checkout.get("address"):
        fields.append(f"Shipping to: {checkout['address']}")

    if product.get("url"):
        fields.append(product["url"])

    fields.append(
        "Buy this? Reply with an unequivocal \"yes\" to confirm -- anything else "
        "(including a hedged answer) will be treated as no."
    )
    # A blank line between every field, not just between groups -- a wall of
    # single-spaced lines still reads as one dense paragraph.
    lines = []
    for field in fields:
        if lines:
            lines.append("")
        lines.append(field)
    return "\n".join(lines)

def find_and_prepare_purchase(
    user_prompt,
    max_results=10,
    store="amazon",
    credentials_path=DEFAULT_CREDENTIALS_PATH,
    quantity=1,
    auto_login=True,
):
    """End-to-end: find the best match, log in if needed, add it to the cart,
    and return the checkout summary (address, shipping time, total incl. tax).

    The order is never placed. Login uses amazon_credentials.json; if Amazon
    shows a CAPTCHA/OTP, run non-headless so a human can clear it.

    Args:
        user_prompt: The shopper's natural-language request.
        max_results: Max raw listings to scrape per store.
        store: Store to search (defaults to amazon).
        credentials_path: Path to amazon_credentials.json.
        quantity: How many to add to the cart.
        auto_login: Attempt credential login when not already signed in.

    Returns:
        A dict with product, cart_added (bool), and checkout (dict or None).
    """
    matches = shop_for_item(user_prompt, max_results=max_results, store=store)
    if not matches:
        return {"product": None, "cart_added": False, "checkout": None}
    product = matches[0]
    _log(f"best match: {product.get('name')!r} ({product.get('url')})")

    result = {"product": product, "cart_added": False, "checkout": None}

    with sync_playwright() as p:
        _log(f"launching browser for cart-prep (headless={HEADLESS})")
        browser, context, page = _launch_browser(p, HEADLESS)
        try:
            if auto_login:
                try:
                    creds = load_credentials(credentials_path)
                    login_amazon(page, creds)
                except (FileNotFoundError, ValueError) as exc:
                    print(f"Skipping login: {exc}")

            result["cart_added"] = add_to_cart(page, product["url"], quantity=quantity)
            if result["cart_added"]:
                result["checkout"] = checkout_summary(page)
        finally:
            _log("closing cart-prep browser")
            context.close()
            browser.close()
    return result

def place_order(page):
    """Click Amazon's final 'Place your order' button and confirm the order
    was submitted.

    WARNING: this ACTUALLY SPENDS MONEY.

    NOTE: while PLACE_ORDERS_ENABLED is False this stays UNCONNECTED -- buy_flow
    never calls it and just returns a canned "order placed" (demo). It's the
    real implementation kept ready for later; flip PLACE_ORDERS_ENABLED to True
    to have buy_flow invoke it, always behind is_unequivocal_confirmation().

    Assumes `page` is signed in and sitting on the checkout / place-order
    review screen (i.e. right after checkout_summary navigated there).

    Args:
        page: A live, signed-in Playwright/Patchright page on the review page.

    Returns:
        A dict with order_placed (bool) and order_number (str or None).
    """
    place_order_selectors = [
        "#placeYourOrder input[name='placeYourOrder1']",
        "input[name='placeYourOrder1']",
        "#submitOrderButtonId input",
        "#bottomSubmitOrderButtonId input",
        "input[aria-labelledby='submitOrderButtonId-announce']",
        "#placeOrder",
    ]
    if not _click_first(page, place_order_selectors):
        # An interstitial can cover the Place-order button. _recover_stuck_page
        # is safe to use here: its Continue path only ever clicks an
        # allow-listed label, so it cannot itself submit the order -- the only
        # thing that places the order is the explicit click below.
        _log("'Place your order' not found -- checking for an interstitial to clear")
        if _recover_stuck_page(page, "finding the 'Place your order' button"):
            _dismiss_prime_upsell(page)
    if not _click_first(page, place_order_selectors):
        print("Could not find the 'Place your order' button.")
        _dump_debug(page, "place-order-button-not-found")
        return {"order_placed": False, "order_number": None}
    _log("clicked 'Place your order'")
    _wait(page, 4000, "order submission to complete")

    body = ""
    try:
        body = page.inner_text("body").lower()
    except Exception:  # noqa: BLE001
        pass

    placed = "/thankyou" in (page.url or "") or any(
        marker in body
        for marker in (
            "order placed",
            "thank you, your order",
            "your order has been placed",
            "placed your order",
            "order confirmation",
        )
    )

    order_number = None
    m = re.search(r"order\s*#\s*([0-9\-]{10,})", body)
    if m:
        order_number = m.group(1)

    if placed:
        suffix = f" (#{order_number})" if order_number else ""
        print(f"Order placed{suffix}.")
    else:
        print("Clicked 'Place your order' but could not confirm the order was placed.")
    return {"order_placed": placed, "order_number": order_number}

# Master switch for real purchases. While False, buy_flow and confirm_purchase
# NEVER call place_order() -- they return a canned "order placed" (demo). Flip
# to True (and make sure place_order is what you want) to actually spend money.
PLACE_ORDERS_ENABLED = False

def buy_flow(
    user_prompt,
    confirm_fn,
    max_results=10,
    store="amazon",
    credentials_path=DEFAULT_CREDENTIALS_PATH,
    quantity=1,
):
    """End-to-end buy flow in a SINGLE browser session: find -> log in -> add
    to cart -> read the checkout summary -> ask the user to confirm -> then
    either place the order on that same live session (when PLACE_ORDERS_ENABLED
    is True) or return a canned "order placed" without calling place_order.

    Because the session stays open across the confirmation, there's no second
    login and no re-adding to cart: the same signed-in page that produced the
    summary is the one the order would be placed on, so the price/total you
    confirmed is the price that gets charged.

    Args:
        user_prompt: The shopper's natural-language request.
        confirm_fn: Callable taking the confirmation-prompt string and returning
            the user's raw reply. It's invoked while the browser session is
            live -- e.g. input() for the CLI, or a function that sends the
            Telegram message and blocks until the user replies.
        max_results: Max raw listings to scrape per store.
        store: Store to search (defaults to amazon).
        credentials_path: Path to amazon_credentials.json. If missing, login and
            the cart summary are skipped and the demo still runs off the search
            result.
        quantity: How many to buy.

    Returns:
        A dict with product, cart_added, checkout, confirmed, order (the
        place_order result, or None), and message (to send back to the user).
    """
    result = {
        "product": None,
        "cart_added": False,
        "checkout": None,
        "confirmed": False,
        "order": None,
        "message": "",
    }

    matches = shop_for_item(user_prompt, max_results=max_results, store=store)
    if not matches:
        result["message"] = "I couldn't find anything matching that."
        return result
    product = matches[0]
    result["product"] = product
    _log(f"best match: {product.get('name')!r} ({product.get('url')})")

    with sync_playwright() as p:
        _log(f"launching browser for single-session buy flow (headless={HEADLESS})")
        browser, context, page = _launch_browser(p, HEADLESS)
        try:
            # Best-effort login + cart so we can show a real checkout summary.
            # Missing credentials just fall back to the search result.
            try:
                creds = load_credentials(credentials_path)
            except (FileNotFoundError, ValueError) as exc:
                print(f"Skipping login/cart summary: {exc}")
            else:
                if login_amazon(page, creds):
                    result["cart_added"] = add_to_cart(
                        page, product["url"], quantity=quantity
                    )
                    if result["cart_added"]:
                        result["checkout"] = checkout_summary(page)

            # Ask for confirmation, showing whatever summary we have.
            prompt = format_purchase_confirmation(product, result["checkout"])
            reply = confirm_fn(prompt)

            if not is_unequivocal_confirmation(reply):
                result["message"] = "Okay, I won't order it."
                return result
            result["confirmed"] = True

            if PLACE_ORDERS_ENABLED:
                # Real purchase -- same live session, item already in cart.
                result["order"] = place_order(page)
                placed = bool(result["order"] and result["order"]["order_placed"])
                result["message"] = (
                    f"Order placed ✅ -- {product.get('name')}."
                    if placed
                    else "Tried to place the order but couldn't confirm it."
                )
            else:
                # Demo: place_order() is intentionally NOT called.
                result["message"] = f"Order placed ✅ -- {product.get('name')}."
        finally:
            _log("closing single-session buy-flow browser")
            context.close()
            browser.close()
    return result

# --- Two-message confirmation flow (for chat bots that can't block on a
# reply) ----------------------------------------------------------------
#
# buy_flow above assumes one call can hold a browser session open while it
# blocks for the user's reply. That doesn't work for something like the
# Telegram bot: each incoming message is handled and replied to before the
# bot polls for the next one, so a tool call that blocks waiting for the next
# message would deadlock -- the reply that would unblock it can never be
# fetched.
#
# So here the confirmation is two separate tool calls, one per incoming
# message, with the pending purchase persisted to disk in between:
#   1. prepare_purchase: search + log in + add to cart + read the checkout
#      summary, stash the result as this conversation's pending purchase, and
#      return the confirmation prompt for the assistant to relay.
#   2. confirm_purchase: on the user's next message, check it against the
#      pending purchase saved for this conversation.
#
# The order-placing account is the same either way (the cart persists
# server-side on Amazon), so the only cost of the two-message shape is a
# second login and a small window where the price could have changed since
# the summary was shown -- confirm_purchase re-reads the checkout summary
# before place_order.

PENDING_PURCHASE_DIR = "state/shopping/pending"

def _pending_purchase_path(conversation_id):
    return os.path.join(PENDING_PURCHASE_DIR, f"{conversation_id}.json")

def save_pending_purchase(
    conversation_id, product, checkout=None, quantity=1, credentials_path=DEFAULT_CREDENTIALS_PATH
):
    """Stash a conversation's in-progress purchase to disk so a later, separate
    message (the confirmation reply) can act on it. credentials_path is stashed
    too -- confirm_purchase signs in and places the order on a FRESH session, so
    it must reuse whichever account prepare_purchase searched/added-to-cart on,
    not silently fall back to this machine's own default account."""
    os.makedirs(PENDING_PURCHASE_DIR, exist_ok=True)
    data = {
        "product": product,
        "checkout": checkout,
        "quantity": quantity,
        "credentials_path": credentials_path,
    }
    with open(_pending_purchase_path(conversation_id), "w") as f:
        json.dump(data, f, indent=2)

def load_pending_purchase(conversation_id):
    """Return the conversation's pending purchase dict, or None if there isn't one."""
    path = _pending_purchase_path(conversation_id)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    with open(path) as f:
        return json.load(f)

def clear_pending_purchase(conversation_id):
    path = _pending_purchase_path(conversation_id)
    if os.path.exists(path):
        os.remove(path)

def prepare_purchase(
    conversation_id,
    user_prompt,
    max_results=10,
    store="amazon",
    credentials_path=DEFAULT_CREDENTIALS_PATH,
    quantity=1,
):
    """Phase 1 of the two-message buy flow: find the best match, log in, add it
    to the cart, read the checkout summary, and stash it as this
    conversation's pending purchase. Places no order.

    Args:
        conversation_id: This conversation's key (email thread id, Telegram
            chat_id) -- bound by the caller, never supplied by the LLM.
        user_prompt: The shopper's natural-language request.
        max_results: Max raw listings to scrape per store.
        store: Store to search (defaults to amazon).
        credentials_path: Path to amazon_credentials.json.
        quantity: How many to buy.

    Returns:
        A message string: either the confirmation prompt (product + checkout
        summary) to relay to the user verbatim, or a not-found message.
    """
    try:
        outcome = find_and_prepare_purchase(
            user_prompt,
            max_results=max_results,
            store=store,
            credentials_path=credentials_path,
            quantity=quantity,
        )
        product = outcome["product"]
        if not product:
            clear_pending_purchase(conversation_id)
            return "I couldn't find anything matching that -- want to try a different search?"

        save_pending_purchase(
            conversation_id, product, outcome["checkout"], quantity, credentials_path
        )
        return format_purchase_confirmation(product, outcome["checkout"])
    except Exception as exc:  # noqa: BLE001
        # Anything unexpected here (a Playwright error, a network hiccup, an
        # unrecognized page) should never surface as a raw exception string --
        # this tool's return value goes straight to the user verbatim.
        _log(f"prepare_purchase failed unexpectedly: {exc!r}")
        clear_pending_purchase(conversation_id)
        return (
            "Something went wrong while searching for and preparing that purchase. "
            "No order was placed -- want to try again?"
        )

def confirm_purchase(conversation_id, reply_text):
    """Phase 2 of the two-message buy flow: check `reply_text` -- the user's
    raw, untouched next message -- against the pending purchase prepare_purchase
    stashed for this conversation.

    is_unequivocal_confirmation is the only thing that decides whether a
    purchase proceeds; the caller must bind reply_text to the user's actual
    message text, never to something the LLM composed as a tool argument,
    since a false positive here spends real money.

    The pending purchase is cleared either way (confirmed or not) so a stray
    later "yes" can't re-trigger an old, already-answered prompt.

    Args:
        conversation_id: This conversation's key -- bound by the caller.
        reply_text: The user's raw reply to the confirmation prompt.

    Returns:
        A message string to send back to the user.
    """
    pending = load_pending_purchase(conversation_id)
    if not pending:
        return "There's no pending order for me to confirm right now."
    clear_pending_purchase(conversation_id)

    if not is_unequivocal_confirmation(reply_text):
        return "Okay, I won't order it."

    product = pending["product"]
    quantity = pending.get("quantity", 1)
    # Older pending purchases saved before credentials_path was stashed here
    # won't have this key -- falling back to the default is the same
    # behavior they always had.
    credentials_path = pending.get("credentials_path", DEFAULT_CREDENTIALS_PATH)

    if not PLACE_ORDERS_ENABLED:
        # Demo: place_order() is intentionally NOT called.
        return f"Order placed ✅ -- {product.get('name')}."

    # Real purchase -- a fresh session (the one from prepare_purchase already
    # closed), re-signed-in on the SAME account prepare_purchase used, then
    # re-checking the cart/total before placing it.
    with sync_playwright() as p:
        _log(f"launching fresh browser for order confirmation (headless={HEADLESS})")
        browser, context, page = _launch_browser(p, HEADLESS)
        try:
            try:
                creds = load_credentials(credentials_path)
            except (FileNotFoundError, ValueError) as exc:
                return f"Couldn't sign in to place the order ({exc}) -- nothing was charged."
            if not login_amazon(page, creds):
                return "Couldn't sign in to place the order -- nothing was charged."

            summary = checkout_summary(page)
            if not summary:
                # Cart may have been cleared since prepare_purchase -- re-add once.
                if not add_to_cart(page, product["url"], quantity=quantity):
                    return "Couldn't find that item in the cart -- nothing was charged."
                summary = checkout_summary(page)
                if not summary:
                    return "Couldn't reach checkout -- nothing was charged."

            order = place_order(page)
            if order["order_placed"]:
                suffix = f" (#{order['order_number']})" if order["order_number"] else ""
                return f"Order placed ✅ -- {product.get('name')}{suffix}."
            return (
                "Tried to place the order but couldn't confirm it went through -- "
                "please check your Amazon orders page."
            )
        except Exception as exc:  # noqa: BLE001
            # Same reasoning as prepare_purchase's catch -- never let a raw
            # exception reach the user. Deliberately NOT claiming "nothing was
            # charged" here: place_order may have already been clicked before
            # whatever failed, so the honest answer is "go check," not a
            # reassurance that might be wrong.
            _log(f"confirm_purchase failed unexpectedly: {exc!r}")
            return (
                "Something went wrong while trying to confirm that order, and I "
                "can't be sure whether it went through -- please check your "
                "Amazon orders page directly."
            )
        finally:
            _log("closing order-confirmation browser")
            context.close()
            browser.close()

PREPARE_PURCHASE_TOOL = {
    "type": "function",
    "function": {
        "name": "prepare_purchase",
        "description": (
            "Search a store for a product, sign in on the user's own account, add "
            "the best match to the cart, and return a message showing the item and "
            "the checkout summary (shipping address, shipping time, order total "
            "incl. tax) -- ask the user to confirm before anything else happens. "
            "This does NOT place the order. Use it when the user has clearly asked "
            "to buy/order/purchase something (not just find or compare). After "
            "calling this, relay its returned message to the user essentially "
            "verbatim and wait for their reply -- do not call confirm_purchase "
            "yourself; it only fires on the user's own next message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_prompt": {
                    "type": "string",
                    "description": "The shopper's request, in their own words.",
                },
                "store": {
                    "type": "string",
                    "enum": ["amazon", "target", "all"],
                    "description": "Which store to search. Defaults to 'amazon'.",
                },
                "quantity": {
                    "type": "integer",
                    "description": "How many to buy. Defaults to 1.",
                },
            },
            "required": ["user_prompt"],
        },
    },
}

CONFIRM_PURCHASE_TOOL = {
    "type": "function",
    "function": {
        "name": "confirm_purchase",
        "description": (
            "Check the user's message against a pending purchase from an earlier "
            "prepare_purchase call in this conversation, and place the order only "
            "if it's an unequivocal 'yes' -- anything hedged or unclear is treated "
            "as a no. Call this whenever the immediately preceding assistant turn "
            "asked the user to confirm a purchase and this message is their reply "
            "to it. Takes no arguments -- it reads the user's raw message itself."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

FIND_PRODUCT_LINK_TOOL = {
    "type": "function",
    "function": {
        "name": "find_product_link",
        "description": (
            "Search a store for a product and return the single best match "
            "with a link the user can click to buy it themselves. This tool "
            "only searches and returns a link: it does NOT sign in, add items "
            "to a cart, or buy anything, so it is not a checkout tool. Use it "
            "when the user asks to find or get a product (e.g. 'find me a "
            "cheap usb-c cable', 'I need a new phone case'). Buying for the "
            "user is a separate flow the user must invoke explicitly and is "
            "outside this tool's scope. Searches Amazon by default; pass "
            "store='target' or store='all' to include Target."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_prompt": {
                    "type": "string",
                    "description": "The shopper's request, in their own words.",
                },
                "store": {
                    "type": "string",
                    "enum": ["amazon", "target", "all"],
                    "description": (
                        "Which store to search. Defaults to 'amazon'."
                    ),
                },
            },
            "required": ["user_prompt"],
        },
    },
}

def _build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Find products and optionally prepare an Amazon cart + checkout summary."
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default="cheap usb-c cable under $10",
        help="The shopper's request.",
    )
    parser.add_argument(
        "--cart",
        action="store_true",
        help="Log in, add the best match to the Amazon cart, and show the "
        "checkout summary (address, shipping time, total incl. tax). "
        "The order is never placed.",
    )
    parser.add_argument(
        "--buy",
        action="store_true",
        help="DEMO buy flow: find the best match, show the checkout summary as "
        "a confirmation prompt, read your reply, and (if it's an unequivocal "
        "yes) report 'order placed'. No real order is ever placed.",
    )
    parser.add_argument(
        "--store", default=DEFAULT_STORE, choices=["amazon", "target", "all"]
    )
    parser.add_argument(
        "--credentials", default=DEFAULT_CREDENTIALS_PATH,
        help="Path to amazon_credentials.json.",
    )
    parser.add_argument("--quantity", type=int, default=1)
    return parser

if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    print(f"Stealth backend: {STEALTH_BACKEND} (headless={HEADLESS})")

    if args.buy:
        # Single-session buy flow. input() is the confirm callback, invoked
        # while the browser session is still open. With PLACE_ORDERS_ENABLED
        # False (the default), this is a demo and never places a real order.
        def _cli_confirm(prompt):
            print("\n" + prompt)
            return input("\nYour reply: ")

        outcome = buy_flow(
            args.prompt,
            _cli_confirm,
            store=args.store,
            credentials_path=args.credentials,
            quantity=args.quantity,
        )
        print("\n" + outcome["message"])
    elif args.cart:
        outcome = find_and_prepare_purchase(
            args.prompt,
            store=args.store,
            credentials_path=args.credentials,
            quantity=args.quantity,
        )
        product = outcome["product"]
        if not product:
            print("No matching product found.")
        else:
            print("\nBest match:")
            print(format_product_link_message(product))
            print(f"\nAdded to cart: {outcome['cart_added']}")
            if outcome["checkout"]:
                print("\nCheckout summary:")
                print(format_checkout_summary(outcome["checkout"]))
    else:
        results = shop_for_item(args.prompt, store=args.store)
        print(json.dumps(results, indent=2))
        if results:
            print()
            print(format_confirmation_prompt(results[0]))