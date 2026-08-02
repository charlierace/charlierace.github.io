#!/usr/bin/env python3
"""
Bring a Trailer scanner: finds live auctions ending within N hours
that appear to be located/registered/titled in California.

USAGE:
    pip install requests beautifulsoup4
    python bat_ca_scanner.py

NOTE: I (Claude) could not test this against the live site — bringatrailer.com
is blocked from my tools. Diagnostics get written to results.md on every run;
if results look wrong, paste me that diagnostics block and I'll fix things.

HOW IT FINDS LISTINGS: BaT's /auctions/ grid page renders listings via
JavaScript, which a plain HTTP request can't execute (confirmed: page loads
fine, HTTP 200, but zero listing links appear in the raw HTML). Instead, this
pulls candidate listing URLs from BaT's RSS feed (a plain XML feed, no JS
required), then visits each individual listing page normally to get the real
end time and location. Trade-off: RSS only surfaces recently-published
listings, not the full closing-soon queue, so this may miss auctions that
were listed a while ago but happen to be closing soon.
"""

import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

HOURS_WINDOW = 48  # "closing within 2 days"
BASE = "https://bringatrailer.com"
RSS_FEED_URL = f"{BASE}/feed/"
RSS_PAGES_TO_FETCH = 5  # each feed page is roughly 10-20 items

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

CA_PATTERN = re.compile(r"\b(california|,\s*ca\b|\bca\.?\s*\d{5})\b", re.IGNORECASE)


def get_soup(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def fetch_rss_items():
    """Pull listing links + titles from BaT's RSS feed, paging through
    several pages and de-duping by link."""
    items = []
    seen_links = set()

    for page in range(1, RSS_PAGES_TO_FETCH + 1):
        url = RSS_FEED_URL if page == 1 else f"{RSS_FEED_URL}?paged={page}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"RSS page {page} failed: {e}")
            break

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            print(f"RSS page {page} did not parse as XML: {e}")
            break

        channel = root.find("channel")
        if channel is None:
            break

        page_items = channel.findall("item")
        if not page_items:
            break  # ran out of pages

        for item in page_items:
            link = (item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            if link and link not in seen_links:
                seen_links.add(link)
                items.append({"link": link, "title": title})

        time.sleep(1)

    return items


def extract_end_time(soup):
    """
    BaT listing pages render a live countdown timer. The timer element
    typically carries the auction end time as a data attribute (e.g.
    data-until epoch seconds) or it's embedded in a <script> block as JSON.
    We try a few strategies and fall back to None if none match.
    """
    # Strategy 1: data-until / data-time attributes on any tag
    for tag in soup.find_all(attrs={"data-until": True}):
        try:
            ts = int(tag["data-until"])
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, KeyError):
            pass

    # Strategy 2: look for an epoch timestamp near "auctions_ending" in scripts
    for script in soup.find_all("script"):
        if script.string and "ending" in script.string.lower():
            m = re.search(r'"timestamp_end"\s*:\s*(\d{9,10})', script.string)
            if m:
                return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)

    # Strategy 3: plain-text "Ends in" / date string near top of page
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Ends?:?\s+([A-Za-z]+ \d{1,2},? \d{4}(?: at)? \d{1,2}:\d{2} ?[APMapm]{2})", text)
    if m:
        try:
            return datetime.strptime(m.group(1).replace(" at", ""), "%B %d, %Y %I:%M %p")
        except ValueError:
            pass

    return None


def extract_location_text(soup):
    """Grab the seller/vehicle location line and full essentials block text."""
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Location:\s*([A-Za-z .,'-]+?\d{0,5})(?:\s{2,}|\||$)", text)
    location = m.group(1).strip() if m else ""

    # also scan the whole essentials/description text for CA/California mentions
    # (covers cases where only the title/registration state is called out)
    full_context = text
    return location, full_context


def looks_california(location, full_context):
    if location and CA_PATTERN.search(location):
        return True
    # narrower check on full text to avoid false positives from unrelated "CA" mentions
    if re.search(r"\btitle[d]?\s*(in)?\s*california\b", full_context, re.IGNORECASE):
        return True
    if re.search(r"\bregistered\s*(in)?\s*california\b", full_context, re.IGNORECASE):
        return True
    return False


def main():
    print(f"Fetching listing links from RSS feed at {RSS_FEED_URL} ...")
    diagnostics = {
        "rss_items_found": 0,
        "links_found": 0,
        "listing_fetch_failures": 0,
        "end_time_not_found": 0,
        "end_time_too_far_out": 0,
        "checked_within_window": 0,
        "ca_matches": 0,
    }

    rss_items = fetch_rss_items()
    diagnostics["rss_items_found"] = len(rss_items)
    diagnostics["links_found"] = len(rss_items)
    print(f"Found {len(rss_items)} candidate listing links from RSS.\n")

    if not rss_items:
        write_results([], diagnostics, error="RSS feed returned zero items")
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) + timedelta(hours=HOURS_WINDOW)
    matches = []

    for i, rss_item in enumerate(rss_items, 1):
        url = rss_item["link"]
        print(f"[{i}/{len(rss_items)}] Checking {url}")
        try:
            lsoup = get_soup(url)
        except requests.RequestException:
            diagnostics["listing_fetch_failures"] += 1
            continue

        end_time = extract_end_time(lsoup)
        if end_time is None:
            diagnostics["end_time_not_found"] += 1
            continue
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        if end_time > cutoff:
            diagnostics["end_time_too_far_out"] += 1
            continue  # not ending soon enough

        diagnostics["checked_within_window"] += 1

        title_tag = lsoup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else url

        location, full_context = extract_location_text(lsoup)
        if looks_california(location, full_context):
            diagnostics["ca_matches"] += 1
            matches.append({
                "title": title,
                "url": url,
                "ends": end_time.isoformat(),
                "location": location,
            })

        time.sleep(1)  # be polite to BaT's servers

    print("\n=== California cars ending within", HOURS_WINDOW, "hours ===")
    if not matches:
        print("No matches found (or parsing needs adjustment — see note in script header).")
    for m in matches:
        print(f"- {m['title']}")
        print(f"    Ends: {m['ends']}  |  Location: {m['location']}")
        print(f"    {m['url']}")

    print("\nDiagnostics:", diagnostics)
    write_results(matches, diagnostics)


def write_results(matches, diagnostics, error=None):
    # Writes a markdown file so a GitHub Actions run has something to
    # commit back to the repo — including diagnostics, so a "no matches"
    # run tells you WHERE it stopped finding things without needing to
    # dig through the Actions log.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open("results.md", "w") as f:
        f.write(f"# BaT California scan — last run {now}\n\n")
        f.write(f"Window: closing within {HOURS_WINDOW} hours\n\n")

        if error:
            f.write(f"**Scan failed to reach BaT:** {error}\n\n")

        if not matches:
            f.write("No matches found.\n\n")
        for m in matches:
            f.write(f"- **{m['title']}**\n")
            f.write(f"  - Ends: {m['ends']}\n")
            f.write(f"  - Location: {m['location']}\n")
            f.write(f"  - [{m['url']}]({m['url']})\n\n")

        f.write("---\n\n")
        f.write("### Diagnostics\n\n")
        f.write(f"- RSS items found: {diagnostics.get('rss_items_found', 0)}\n")
        f.write(f"- Listing pages that failed to fetch: {diagnostics.get('listing_fetch_failures', 0)}\n")
        f.write(f"- Listings where end time could not be parsed: {diagnostics.get('end_time_not_found', 0)}\n")
        f.write(f"- Listings ending outside the {HOURS_WINDOW}h window: {diagnostics.get('end_time_too_far_out', 0)}\n")
        f.write(f"- Listings checked within window: {diagnostics.get('checked_within_window', 0)}\n")
        f.write(f"- California matches: {diagnostics.get('ca_matches', 0)}\n")

        if diagnostics.get("rss_items_found", 0) == 0:
            f.write(
                "\n**Likely cause:** the RSS feed itself returned zero items. "
                "This could mean BaT changed their feed URL/structure, or the "
                "feed request is being blocked.\n"
            )
        elif diagnostics.get("end_time_not_found", 0) == diagnostics.get("rss_items_found", 0):
            f.write(
                "\n**Likely cause:** listing links were found, but the end-time "
                "parser (`extract_end_time`) never found a usable end time on "
                "any listing page. The HTML structure BaT uses for the "
                "countdown/end-time likely differs from what the selectors "
                "expect and needs updating.\n"
            )


if __name__ == "__main__":
    main()
