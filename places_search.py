"""Place search backed by Mapbox's Search Box API, replacing the old
Google Places integration (which never actually had credentials deployed --
credentials/backend/google_maps_api_key.json never existed, so find_places
was non-functional in production until now).

Mapbox's API is lower-level than Google's Text Search -- it has no boolean
query syntax and `q` is just matched against place names/text, so a few
things are handled deliberately here rather than left to the API:

- A compound request ("cafe or steakhouse") is split into separate terms
  and searched individually, then merged -- passing it as one string gets
  matched as literal text against place NAMES (confirmed live: "cafes OR
  steak house" returned real places literally named things like "OZT Cafe
  and Steak House", worldwide, not local cafes AND local steakhouses).
- Each term is matched against Mapbox's own real canonical category list
  (fetched once, cached to disk) and searched via the dedicated /category
  endpoint when it maps cleanly -- that endpoint only returns places
  actually tagged with that category, unlike free-text matching.
- A real bbox is always computed and passed -- `proximity` is only a
  ranking bias, not a distance filter (confirmed live: an ambiguous query
  returned a match 14,000km away in Indonesia because its name matched the
  query text well, despite being nowhere near the requested location).
"""

import json
import math
import os
import re

import requests

ACCESS_TOKEN_FILE = "credentials/backend/mapbox_access_token.json"
CATEGORY_CACHE_FILE = "state/mapbox_categories.json"

GEOCODE_URL = "https://api.mapbox.com/search/geocode/v6/forward"
FORWARD_URL = "https://api.mapbox.com/search/searchbox/v1/forward"
CATEGORY_URL = "https://api.mapbox.com/search/searchbox/v1/category/{category_id}"
CATEGORY_LIST_URL = "https://api.mapbox.com/search/searchbox/v1/list/category"

DEFAULT_RADIUS_KM = 8
MAX_RESULTS = 10


def _access_token():
    with open(ACCESS_TOKEN_FILE) as f:
        return json.load(f)["access_token"]


def _load_category_list():
    """Mapbox's ~500-entry canonical POI category list -- reference data
    that essentially never changes, so it's fetched once and cached to disk
    rather than re-fetched on every search."""
    if os.path.exists(CATEGORY_CACHE_FILE) and os.path.getsize(CATEGORY_CACHE_FILE) > 0:
        with open(CATEGORY_CACHE_FILE) as f:
            return json.load(f)

    response = requests.get(CATEGORY_LIST_URL, params={"access_token": _access_token()}, timeout=15)
    response.raise_for_status()
    items = response.json().get("listItems", [])
    os.makedirs(os.path.dirname(CATEGORY_CACHE_FILE), exist_ok=True)
    with open(CATEGORY_CACHE_FILE, "w") as f:
        json.dump(items, f, indent=2)
    return items


def _normalize(term):
    return re.sub(r"[\s_-]+", "_", term.strip().lower()).rstrip("s")


def _match_category(term):
    """Best-effort match of a single term (e.g. "steak house", "sushi")
    against a real Mapbox canonical category id, or None if nothing
    reasonable matches -- callers fall back to free-text search in that
    case rather than guessing at a made-up category id."""
    normalized = _normalize(term)
    categories = _load_category_list()

    for cat in categories:
        if _normalize(cat["canonical_id"]) == normalized or _normalize(cat["name"]) == normalized:
            return cat["canonical_id"]

    # Loose containment (e.g. "coffee" inside "coffee_shop") -- prefer the
    # shortest matching id, which tends to be the most general/likely
    # category rather than an overly narrow one (e.g. "sports_bar").
    candidates = [
        cat["canonical_id"]
        for cat in categories
        if normalized in _normalize(cat["canonical_id"]) or normalized in _normalize(cat["name"])
    ]
    return min(candidates, key=len) if candidates else None


def _split_terms(query):
    """Split a compound request into separate single-category search
    terms -- see this module's docstring for why."""
    parts = re.split(r"\s*(?:,|/|\bor\b|\band\b)\s*", query, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _geocode(address):
    """Resolve a free-text location to (longitude, latitude), or detect
    genuine ambiguity instead of silently trusting Mapbox's top result.

    A bare city name like "Boulder" or "Springfield" really does match
    several distinct real places (confirmed live) -- but a result like
    "San Diego County" or "San Diego, Madrid" showing up alongside "San
    Diego, California" is NOT real ambiguity, it's just a broader
    (district) or far less specific (neighborhood) wrapper around/near the
    same top match. The real signal is whether there's more than one
    GEOGRAPHICALLY DISTINCT result at the SAME feature_type tier as the top
    result -- comparing against a fixed tier like "place" isn't enough
    either: "Little Italy" matches five real, distinct neighborhoods (NYC,
    Chicago, Toronto, San Diego, Niagara Falls) and none of them are
    "place"-tier at all.

    Returns:
        (coordinates, None) if resolved confidently, or
        (None, [full_address, ...]) if genuinely ambiguous -- the caller
        must ask the user to pick one rather than guess.
    """
    response = requests.get(
        GEOCODE_URL, params={"q": address, "limit": 5, "access_token": _access_token()}, timeout=15
    )
    response.raise_for_status()
    features = response.json().get("features", [])
    if not features:
        return None, None

    top_props = features[0]["properties"]
    top_tier = top_props["feature_type"]

    distinct_matches = {}  # (lng, lat) -> full_address, same tier as the top result only
    for feature in features:
        props = feature["properties"]
        if props["feature_type"] != top_tier:
            continue
        coords = (props["coordinates"]["longitude"], props["coordinates"]["latitude"])
        distinct_matches.setdefault(coords, props.get("full_address") or props.get("name"))

    if len(distinct_matches) > 1:
        return None, list(distinct_matches.values())

    lng, lat = top_props["coordinates"]["longitude"], top_props["coordinates"]["latitude"]
    return (lng, lat), None


def _bbox_around(lng, lat, radius_km=DEFAULT_RADIUS_KM):
    lat_delta = radius_km / 111.0  # ~111km per degree of latitude, everywhere
    lng_delta = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return f"{lng - lng_delta},{lat - lat_delta},{lng + lng_delta},{lat + lat_delta}"


def _search_category(category_id, lng, lat, bbox):
    response = requests.get(
        CATEGORY_URL.format(category_id=category_id),
        params={
            "proximity": f"{lng},{lat}",
            "bbox": bbox,
            "limit": MAX_RESULTS,
            "access_token": _access_token(),
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("features", [])


def _search_free_text(term, lng, lat, bbox):
    response = requests.get(
        FORWARD_URL,
        params={
            "q": term,
            "proximity": f"{lng},{lat}",
            "bbox": bbox,
            "limit": MAX_RESULTS,
            "access_token": _access_token(),
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("features", [])


def find_places(query, location):
    """Search for restaurants, cafes, or other places matching a query,
    scoped to a location.

    Args:
        query: What to search for, e.g. "cafes", "sushi", "cafe or
            steakhouse" -- a compound request is split and searched as
            separate terms, then merged (see module docstring).
        location: City, neighborhood, or address to scope the search to --
            required; this data source needs a real point to search around,
            unlike a text-search engine that can infer a place from the
            query alone.

    Returns:
        A list of dicts, each with name, address, distance_km, phone, and
        website for the top matching places (merged and deduplicated
        across every term in query) -- NOT ratings or price level, which
        this data source doesn't provide. Or a message string -- asking the
        user to pick a specific match, or to clarify -- if the location
        couldn't be resolved confidently; relay that verbatim rather than
        guessing which place was meant.
    """
    try:
        coords, ambiguous_matches = _geocode(location)
        if ambiguous_matches:
            options = "; ".join(ambiguous_matches[:5])
            return (
                f"'{location}' matches more than one real place: {options}. "
                "Ask the user which one they meant before searching."
            )
        if coords is None:
            return f"Couldn't find a location matching {location!r} -- ask the user to clarify."
        lng, lat = coords
        bbox = _bbox_around(lng, lat)

        seen_ids = set()
        all_features = []
        for term in _split_terms(query):
            category_id = _match_category(term)
            features = (
                _search_category(category_id, lng, lat, bbox)
                if category_id
                else _search_free_text(term, lng, lat, bbox)
            )
            for feature in features:
                mapbox_id = feature.get("properties", {}).get("mapbox_id")
                if mapbox_id in seen_ids:
                    continue
                seen_ids.add(mapbox_id)
                all_features.append(feature)

        all_features.sort(key=lambda f: f["properties"].get("distance") or float("inf"))

        results = []
        for feature in all_features[:MAX_RESULTS]:
            props = feature.get("properties", {})
            distance_m = props.get("distance")
            metadata = props.get("metadata", {})
            results.append(
                {
                    "name": props.get("name"),
                    "address": props.get("full_address") or props.get("place_formatted"),
                    "distance_km": round(distance_m / 1000, 2)
                    if isinstance(distance_m, (int, float))
                    else None,
                    "phone": metadata.get("phone"),
                    "website": metadata.get("website"),
                }
            )
        return results
    except Exception as exc:  # noqa: BLE001
        return f"Place search failed unexpectedly ({exc}) -- try again or rephrase."


FIND_PLACES_TOOL = {
    "type": "function",
    "function": {
        "name": "find_places",
        "description": (
            "Search for restaurants, cafes, or other places matching a query, "
            "scoped to a location (city, neighborhood, or address -- required). "
            "Use this when the user asks to find or get recommendations for a "
            "place to eat, get coffee, etc. For multiple kinds of place (e.g. "
            "'cafe or steakhouse'), just list them separated by 'or'/commas -- "
            "each is searched separately and the results merged. If you don't "
            "know what area/city to search in and the user hasn't said, ask "
            "them first rather than guessing. If the location name itself is "
            "ambiguous (e.g. 'Springfield', 'Boulder' -- multiple real places "
            "share that name), this returns a message asking you to have the "
            "user pick one -- relay that question verbatim rather than "
            "guessing which one they meant. Results include name, address, "
            "distance, phone, and website -- NOT star ratings or price level."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for, e.g. 'cafes', 'sushi', 'cafe or steakhouse'.",
                },
                "location": {
                    "type": "string",
                    "description": "City, neighborhood, or address to scope the search to.",
                },
            },
            "required": ["query", "location"],
        },
    },
}
