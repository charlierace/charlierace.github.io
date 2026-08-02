#!/usr/bin/env python3
"""
Bring a Trailer scanner: finds live auctions ending within N hours
that appear to be located/registered in California.

USAGE:
    pip install requests
    python bat_ca_scanner.py

HOW THIS WORKS: bringatrailer.com/auctions/ embeds a JSON search index
directly in its raw HTML (confirmed via browser DevTools) covering every
live auction — with title, url, timestamp_end (real close time, epoch
seconds), lat/lon (seller location), and a full-text "searchable" field.
This script fetches that one page, extracts every embedded listing object,
and filters for closing soon + California.

CALIFORNIA MATCH: a listing counts as California if EITHER:
  - its lat/lon falls inside a rough California bounding box, OR
  - its searchable/title text mentions "California" or a CA-style location
The bounding box is approximate and may have minor overlap with border
areas of Nevada/Arizona/Oregon — check the "location" field if precision
near the border matters to you.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

HOURS_WINDOW = 48  # "closing within 2 days"
BASE = "https://bringatrailer.com"
AUCTIONS_URL = f"{BASE}/auctions/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

CA_PATTERN = re.compile(r"\bcalifornia\b|,\s*ca\b", re.IGNORECASE)

# Rough California bounding box (lat/lon)
CA_LAT_MIN, CA_LAT_MAX = 32.4, 42.1
CA_LON_MIN, CA_LON_MAX = -124.6, -114.0


def fetch_embedded_listings():
    """
    Fetch the auctions page and pull out every embedded listing object.
    Each object's keys appear in alphabetical order in BaT's markup
    ("excerpt" first), so we anchor on that to find each object's start,
    then let Python's real JSON parser (raw_decode) read the exact object
    — far more reliable than a hand-rolled brace-matching regex.
    """
    resp = requests.get(AUCTIONS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html_text = resp.text

    decoder = json.JSONDecoder()
    objects = []
    seen_urls = set()

    excerpt_matches = list(re.finditer(r'"excerpt"\s*:', html_text))

    for m in excerpt_matches:
        key_idx = m.start()
        brace_idx = html_text.rfind("{", 0, key_idx)
        if brace_idx == -1:
            continue
        try:
            obj, _ = decoder.raw_decode(html_text, brace_idx)
        except json.JSONDecodeError:
            continue

        if not isinstance(obj, dict) or "timestamp_end" not in obj:
            continue

        url = obj.get("url", "")
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        objects.append(obj)

    return objects, len(excerpt_matches)


def looks_california(listing):
    lat, lon = listing.get("lat"), listing.get("lon")
    try:
        if lat is not None and lon is not None:
            lat_f, lon_f = float(lat), float(lon)
            if CA_LAT_MIN <= lat_f <= CA_LAT_MAX and CA_LON_MIN <= lon_f <= CA_LON_MAX:
                return True
    except (TypeError, ValueError):
        pass

    text = f"{listing.get('searchable', '')} {listing.get('title', '')}"
    return bool(CA_PATTERN.search(text))


def main():
    print(f"Fetching {AUCTIONS_URL} ...")
    diagnostics = {
        "excerpt_matches_seen": 0,
        "objects_parsed": 0,
        "missing_timestamp": 0,
        "ending_outside_window": 0,
        "checked_within_window": 0,
        "ca_matches": 0,
    }

    try:
        listings, excerpt_count = fetch_embedded_listings()
    except requests.RequestException as e:
        write_results([], diagnostics, error=f"Could not reach BaT: {e}")
        sys.exit(1)

    diagnostics["excerpt_matches_seen"] = excerpt_count
    diagnostics["objects_parsed"] = len(listings)
    print(f"Parsed {len(listings)} listing objects out of {excerpt_count} candidates.\n")

    if not listings:
        write_results(
            [],
            diagnostics,
            error="No embedded listing objects were parsed — page structure may have changed.",
        )
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) + timedelta(hours=HOURS_WINDOW)
    now = datetime.now(timezone.utc)
    matches = []

    for listing in listings:
        ts_end = listing.get("timestamp_end")
        if ts_end is None:
            diagnostics["missing_timestamp"] += 1
            continue

        try:
            end_time = datetime.fromtimestamp(int(ts_end), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            diagnostics["missing_timestamp"] += 1
            continue

        if end_time > cutoff or end_time < now:
            diagnostics["ending_outside_window"] += 1
            continue

        diagnostics["checked_within_window"] += 1

        if looks_california(listing):
            diagnostics["ca_matches"] += 1
            matches.append({
                "title": listing.get("title", listing.get("url", "Unknown")),
                "url": listing.get("url", ""),
                "ends": end_time.isoformat(),
                "location": f"lat={listing.get('lat')}, lon={listing.get('lon')}",
            })

    print("\n=== California cars ending within", HOURS_WINDOW, "hours ===")
    if not matches:
        print("No matches found.")
    for m in matches:
        print(f"- {m['title']}")
        print(f"    Ends: {m['ends']}  |  {m['location']}")
        print(f"    {m['url']}")

    print("\nDiagnostics:", diagnostics)
    write_results(matches, diagnostics)


def write_results(matches, diagnostics, error=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open("results.md", "w") as f:
        f.write(f"# BaT California scan — last run {now}\n\n")
        f.write(f"Window: closing within {HOURS_WINDOW} hours\n\n")

        if error:
            f.write(f"**Note:** {error}\n\n")

        if not matches:
            f.write("No matches found.\n\n")
        for m in matches:
            f.write(f"- **{m['title']}**\n")
            f.write(f"  - Ends: {m['ends']}\n")
            f.write(f"  - Location: {m['location']}\n")
            f.write(f"  - [{m['url']}]({m['url']})\n\n")

        f.write("---\n\n")
        f.write("### Diagnostics\n\n")
        f.write(f"- '\"excerpt\":' occurrences seen on page: {diagnostics.get('excerpt_matches_seen', 0)}\n")
        f.write(f"- Listing objects successfully parsed: {diagnostics.get('objects_parsed', 0)}\n")
        f.write(f"- Listings missing/invalid timestamp_end: {diagnostics.get('missing_timestamp', 0)}\n")
        f.write(f"- Listings ending outside the {HOURS_WINDOW}h window: {diagnostics.get('ending_outside_window', 0)}\n")
        f.write(f"- Listings checked within window: {diagnostics.get('checked_within_window', 0)}\n")
        f.write(f"- California matches: {diagnostics.get('ca_matches', 0)}\n")

        if diagnostics.get("objects_parsed", 0) == 0:
            f.write(
                "\n**Likely cause:** 0 listing objects parsed. BaT may have "
                "changed the embedded data structure, or added bot "
                "protection that returns different HTML to a script than to "
                "a browser. Next step: re-check via browser DevTools that "
                "the '\"excerpt\":' pattern still appears in the raw page "
                "response (not just the rendered DOM).\n"
            )


if __name__ == "__main__":
    main()
