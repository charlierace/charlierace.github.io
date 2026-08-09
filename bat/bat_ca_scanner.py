#!/usr/bin/env python3
"""
Bring a Trailer scanner: pulls ALL live auctions (with a valid year+make
parsed from the title — parts listings like wheels/engines are skipped)
into results.json with year, make, model, current bid, end time, and
title/registration place. Filtering by days-left, make/model, or
title/reg place happens client-side in the dashboard, not here — so this
writes the full data set rather than pre-filtering by time window.

USAGE:
    pip install requests
    python bat_ca_scanner.py

STAGE 1 (cheap, 1 request): bringatrailer.com/auctions/ embeds a JSON
search index directly in its raw HTML covering every live auction, with
title, url, and timestamp_end (real close time). Used to find everything
still live.

STAGE 2 (1 request per listing that has a parseable year+make): visit
each listing's own page to pull current bid and any title/registration
mention. This is now close to a full crawl of live auctions rather than
a narrowed subset, so expect a run of several minutes.

RELIABILITY NOTE: current bid and title/registration place are the least
certain fields. BaT uses a live-bidding service (Pusher) so the bid shown
in the raw HTML may be a stale starting value rather than the true live
figure, and "title/registration place" isn't a standard structured field
on BaT listings — it's guessed from description text and will often come
back empty for listings that don't mention it. If either looks
consistently wrong in results.json, send me a listing URL and I'll tune
the extraction.
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# Effectively "all" live auctions — BaT auctions rarely run past ~14 days,
# so this comfortably covers everything currently live without needing to
# remove time filtering altogether. Actual day-level filtering happens
# client-side in the dashboard.
HOURS_WINDOW = 24 * 14
BASE = "https://bringatrailer.com"
AUCTIONS_URL = f"{BASE}/auctions/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]
# Longest names first so "New York" matches before a stray "York"-like partial
_STATE_ALTERNATION = "|".join(re.escape(s) for s in sorted(US_STATES, key=len, reverse=True))

TITLE_PLACE_PATTERNS = [
    # "clean Florida title", "Florida title", "California registration"
    re.compile(rf"\b({_STATE_ALTERNATION})\s+(?:title|registration)\b", re.IGNORECASE),
    # "titled in Florida", "registered in California"
    re.compile(rf"\b(?:titled|registered)\s+in\s+({_STATE_ALTERNATION})\b", re.IGNORECASE),
    # "title: Florida" / "title - Florida"
    re.compile(rf"\btitle\s*[:\-]\s*({_STATE_ALTERNATION})\b", re.IGNORECASE),
    # "title from Florida"
    re.compile(rf"\btitle\s+from\s+({_STATE_ALTERNATION})\b", re.IGNORECASE),
]

# Known multi-word makes, checked before falling back to "first word = make"
MULTI_WORD_MAKES = [
    "Aston Martin", "Land Rover", "Mercedes-Benz", "Alfa Romeo", "Rolls-Royce",
    "Austin-Healey", "De Tomaso", "Shelby American", "Harley-Davidson",
    "American Motors", "Great Wall", "Big Dog", "Toyota Land",
]


# ---------- Stage 1: cheap index fetch ----------

def fetch_embedded_listings():
    """Pull every embedded listing object out of the auctions page's raw
    HTML. Keys appear alphabetically in BaT's markup ("excerpt" first), so
    we anchor there, then let Python's real JSON parser (raw_decode) read
    the exact object — far more reliable than hand-rolled brace matching.
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


def parse_year_make_model(title):
    """Best-effort split of a BaT title into (year, make, model).
    Titles often have mileage/condition prefixes ('300-Mile 2019 Porsche
    911 GT3...'), so we anchor on the first 4-digit year token rather than
    assuming the year is at position 0. Parts listings (wheels, engines)
    often have no year at all — returns (None, None, title) in that case.
    """
    m = re.search(r"\b(19[0-9]{2}|20[0-9]{2})\b", title)
    if not m:
        return None, None, title.strip()

    year = m.group(1)
    remainder = title[m.end():].strip(" ,-")
    if not remainder:
        return year, None, None

    for make in MULTI_WORD_MAKES:
        if remainder.lower().startswith(make.lower() + " "):
            return year, make, remainder[len(make):].strip()

    parts = remainder.split(" ", 1)
    make = parts[0]
    model = parts[1] if len(parts) > 1 else ""
    return year, make, model


# ---------- Stage 2: per-match detail enrichment ----------

def fetch_listing_details(url):
    """Best-effort extraction of current bid and any title/registration-
    place mention from an individual listing page."""
    details = {"current_bid": None, "title_place": None}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return details

    plain = re.sub(r"<[^>]+>", " ", resp.text)
    plain = re.sub(r"\s+", " ", plain)

    bid_patterns = [
        r'"current_bid"\s*:\s*"?([\d,]+)"?',
        r'"bid_amount"\s*:\s*"?([\d,]+)"?',
        r'"highest_bid"\s*:\s*"?([\d,]+)"?',
        r"Current Bid:?\s*\$([\d,]+)",
        r"USD\s*\$\s*([\d,]+)",
    ]
    for pat in bid_patterns:
        m = re.search(pat, plain, re.IGNORECASE)
        if m:
            details["current_bid"] = m.group(1)
            break

    for pat in TITLE_PLACE_PATTERNS:
        m = pat.search(plain)
        if m:
            details["title_place"] = m.group(1)
            break

    return details


def main():
    print(f"Fetching {AUCTIONS_URL} ...")
    diagnostics = {
        "excerpt_matches_seen": 0,
        "objects_parsed": 0,
        "missing_timestamp": 0,
        "ending_outside_window": 0,
        "checked_within_window": 0,
        "skipped_no_year_or_make": 0,
        "detail_fetch_failures": 0,
        "bid_found": 0,
        "title_place_found": 0,
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
            [], diagnostics,
            error="No embedded listing objects were parsed — page structure may have changed.",
        )
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) + timedelta(hours=HOURS_WINDOW)
    now = datetime.now(timezone.utc)
    candidates = []

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

        title = listing.get("title") or listing.get("url", "Unknown")
        year, make, model = parse_year_make_model(title)
        if not year or not make:
            diagnostics["skipped_no_year_or_make"] += 1
            continue

        candidates.append((listing, end_time, year, make, model, title))

    print(f"{len(candidates)} listings with a valid year+make — fetching details...\n")

    matches = []
    for i, (listing, end_time, year, make, model, title) in enumerate(candidates, 1):
        url = listing.get("url", "")
        print(f"[{i}/{len(candidates)}] {title}")

        details = fetch_listing_details(url) if url else {
            "current_bid": None, "title_place": None
        }

        if details["current_bid"]:
            diagnostics["bid_found"] += 1
        if details["title_place"]:
            diagnostics["title_place_found"] += 1

        matches.append({
            "full_title": title,
            "year": year,
            "make": make,
            "model": model,
            "current_bid": details["current_bid"],
            "ends": end_time.isoformat(),
            "title_place": details["title_place"],
            "url": url,
        })

        time.sleep(0.5)  # be polite — this now covers close to all live auctions

    print("\nDiagnostics:", diagnostics)
    write_results(matches, diagnostics)


def write_results(matches, diagnostics, error=None):
    now_dt = datetime.now(timezone.utc)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M UTC")

    # Machine-readable output for the dashboard
    with open("results.json", "w") as f:
        json.dump({
            "last_run": now_str,
            "last_run_iso": now_dt.isoformat(),
            "window_hours": HOURS_WINDOW,
            "error": error,
            "listings": matches,
        }, f, indent=2)

    # Human-readable output for quick viewing on GitHub / debugging
    with open("results.md", "w") as f:
        f.write(f"# BaT scan — last run {now_str}\n\n")
        f.write(f"Window: all live auctions closing within {HOURS_WINDOW // 24} days "
                f"(effectively all); filter by days-left/make/title-place in the dashboard\n\n")

        if error:
            f.write(f"**Note:** {error}\n\n")

        if not matches:
            f.write("No matches found.\n\n")
        else:
            f.write("| Year | Make | Model | Current Bid | Ends (UTC) | Title/Reg Place |\n")
            f.write("|---|---|---|---|---|---|\n")
            for m in matches:
                bid = f"${m['current_bid']}" if m["current_bid"] else "Unknown"
                tp = m["title_place"] or "Not found"
                f.write(
                    f"| {m['year']} | {m['make']} | {m['model'] or '?'} "
                    f"| {bid} | {m['ends']} | {tp} |\n"
                )
            f.write("\n")
            for m in matches:
                f.write(f"- [{m['full_title']}]({m['url']})\n")

        f.write("\n---\n\n### Diagnostics\n\n")
        for k, v in diagnostics.items():
            f.write(f"- {k.replace('_', ' ')}: {v}\n")

        if diagnostics.get("objects_parsed", 0) == 0:
            f.write(
                "\n**Likely cause:** 0 listing objects parsed from the auctions "
                "page. Structure may have changed — re-check via DevTools.\n"
            )
        elif diagnostics.get("checked_within_window", 0) > 0 and diagnostics.get("bid_found", 0) == 0:
            f.write(
                "\n**Note:** no current bid values were found on any listing "
                "page. The bid-extraction patterns likely need tuning — "
                "send me one listing URL from the table above and I'll fix it.\n"
            )


if __name__ == "__main__":
    main()
