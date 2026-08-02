#!/usr/bin/env python3
"""
Bring a Trailer scanner: finds live auctions ending within N hours
that appear to be located/registered/titled in California.

USAGE:
    pip install requests beautifulsoup4
    python bat_ca_scanner.py

NOTE: I (Claude) could not test this against the live site — bringatrailer.com
is blocked from my tools. Run it and if results look wrong (empty, or the
location/time isn't being found), copy me a snippet of "View Page Source" from
one listing page and I'll fix the selectors.
"""

import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

HOURS_WINDOW = 48  # "closing within 2 days"
BASE = "https://bringatrailer.com"
AUCTIONS_URL = f"{BASE}/auctions/"  # live auctions listing page

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


def find_listing_links(soup):
    """Pull every /listing/ link off the auctions page."""
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/listing/" in href:
            links.add(href.split("?")[0])
    return sorted(links)


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
    print(f"Fetching auctions list from {AUCTIONS_URL} ...")
    try:
        soup = get_soup(AUCTIONS_URL)
    except requests.RequestException as e:
        print(f"Could not reach BaT: {e}")
        sys.exit(1)

    listing_links = find_listing_links(soup)
    print(f"Found {len(listing_links)} candidate listing links.\n")

    cutoff = datetime.now(timezone.utc) + timedelta(hours=HOURS_WINDOW)
    matches = []

    for i, link in enumerate(listing_links, 1):
        url = link if link.startswith("http") else BASE + link
        print(f"[{i}/{len(listing_links)}] Checking {url}")
        try:
            lsoup = get_soup(url)
        except requests.RequestException:
            continue

        end_time = extract_end_time(lsoup)
        if end_time is None:
            continue
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        if end_time > cutoff:
            continue  # not ending soon enough

        title_tag = lsoup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else url

        location, full_context = extract_location_text(lsoup)
        if looks_california(location, full_context):
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

    # Also write a markdown file so a GitHub Actions run has something
    # to commit back to the repo for you to view.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open("results.md", "w") as f:
        f.write(f"# BaT California scan — last run {now}\n\n")
        f.write(f"Window: closing within {HOURS_WINDOW} hours\n\n")
        if not matches:
            f.write("No matches found.\n")
        for m in matches:
            f.write(f"- **{m['title']}**\n")
            f.write(f"  - Ends: {m['ends']}\n")
            f.write(f"  - Location: {m['location']}\n")
            f.write(f"  - [{m['url']}]({m['url']})\n\n")


if __name__ == "__main__":
    main()
