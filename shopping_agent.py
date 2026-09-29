import json
import math
import time

import requests
from playwright.sync_api import sync_playwright

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gpt-oss:20b"

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


def interpret_shopping_prompt(user_prompt):
    """Decide whether a shopping request names a specific product, and build
    a good search query either way.

    Args:
        user_prompt: The shopper's natural-language request.

    Returns:
        A dict with is_specific (bool) and search_query (str).
    """
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": INTERPRET_PROMPT_TEMPLATE},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",
            "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    return json.loads(response.json()["message"]["content"])


def search_products(query, max_results=10):
    """Search Target for a product and return raw scraped listings. No
    login, no stealth/anti-detection techniques -- plain browsing.

    Args:
        query: What to search for, e.g. "usb-c cable".
        max_results: Maximum number of listings to return.

    Returns:
        A list of dicts, each with raw_text (the listing's full scraped
        text, for the LLM to parse) and url.
    """
    results = []
    seen_hrefs = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"https://www.target.com/s?searchTerm={query.replace(' ', '+')}", timeout=15000)
        page.wait_for_timeout(2000)

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
            results.append({"raw_text": text, "url": f"https://www.target.com{href}"})

        browser.close()
    return results


def filter_products(products, user_prompt):
    """Use the LLM to extract structured info from raw scraped listings and
    filter down to the ones matching what the user actually asked for.

    Args:
        products: The list returned by search_products.
        user_prompt: The shopper's original natural-language request.

    Returns:
        A list of dicts, each with name, price, rating, review_count, and
        url, for the matches.
    """
    listing_text = "\n\n".join(f"[{i}] {p['raw_text']}" for i, p in enumerate(products))
    user_content = f"Shopper's request: {user_prompt}\n\nListings:\n{listing_text}"

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": FILTER_PROMPT_TEMPLATE},
                {"role": "user", "content": user_content},
            ],
            "format": "json",
            "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    result = json.loads(response.json()["message"]["content"])

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


def shop_for_item(user_prompt, max_results=10):
    """End-to-end: interpret the request, search, filter to real matches,
    and rank by value -- unless the request named a specific product, in
    which case ranking is skipped (there's no "better value" tradeoff to
    make when the user already knows exactly what they want).

    Args:
        user_prompt: The shopper's natural-language request.
        max_results: Maximum number of raw listings to scrape and consider.

    Returns:
        A list of matching products (name, price, rating, review_count,
        url) -- ranked best-value-first for general requests, or as-found
        for specific-product requests.
    """
    interpretation = interpret_shopping_prompt(user_prompt)
    raw_products = search_products(interpretation["search_query"], max_results=max_results)
    matches = filter_products(raw_products, user_prompt)

    if interpretation["is_specific"]:
        return matches
    return rank_products(matches)


def find_product_link(user_prompt, max_results=10):
    """The simple, no-checkout-automation path: search for a product and
    hand back a link for the user to buy it themselves. No login, no cart,
    no checkout -- the actual purchase click is always the human's.

    Args:
        user_prompt: The shopper's natural-language request.
        max_results: Maximum number of raw listings to scrape and consider.

    Returns:
        A message string with the top match (or a message saying nothing
        was found).
    """
    matches = shop_for_item(user_prompt, max_results=max_results)
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

    return f"Found: {product['name']} -- ${product['price']}{rating_bit}\n{product['url']}"


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

    return (
        f"Found: {product['name']} -- ${product['price']}{rating_bit}\n{product['url']}\n\n"
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


def open_browser_for_manual_login(timeout_seconds=180):
    """Open a REAL, visible browser window and let a human manually complete
    Target's login -- including any human-verification challenge (e.g. the
    "Press & hold to confirm you're a human" check). No automation attempts
    the login or the challenge itself; this only navigates to the login page
    and then waits, polling for a sign that a human has finished logging in.

    Args:
        timeout_seconds: How long to wait for manual login before giving up.

    Returns:
        (playwright, browser, page) -- the live, authenticated session, left
        open so the caller can continue with add-to-cart/checkout. The
        caller is responsible for eventually closing it (playwright.stop()).

    Raises:
        TimeoutError: if login wasn't completed within timeout_seconds.
    """
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto("https://www.target.com/login")

    print(
        f"Browser window opened -- please log in manually (including any "
        f"verification challenge). Waiting up to {timeout_seconds}s..."
    )

    start = time.time()
    while time.time() - start < timeout_seconds:
        if "/login" not in page.url:
            print("Login detected -- resuming automated control.")
            return playwright, browser, page
        time.sleep(2)

    browser.close()
    playwright.stop()
    raise TimeoutError("Manual login was not completed within the timeout.")


FIND_PRODUCT_LINK_TOOL = {
    "type": "function",
    "function": {
        "name": "find_product_link",
        "description": (
            "Search for a product the user wants to buy and return the best "
            "match with a link -- the user clicks the link to actually buy it "
            "themselves; this tool never logs in, adds to cart, or completes "
            "any purchase. Use this whenever the user asks to find/get/buy "
            "something online (e.g. 'find me a cheap usb-c cable', 'I need a "
            "new phone case')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_prompt": {
                    "type": "string",
                    "description": "The shopper's request, in their own words.",
                },
            },
            "required": ["user_prompt"],
        },
    },
}


if __name__ == "__main__":
    results = shop_for_item("cheap usb-c cable under $10")
    print(json.dumps(results, indent=2))

    if results:
        print()
        print(format_confirmation_prompt(results[0]))
