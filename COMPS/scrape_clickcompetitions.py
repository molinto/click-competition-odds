#!/usr/bin/env python3
"""
ClickCompetitions scraper (category -> comp pages -> details)

Usage:
  python scrape_clickcompetitions.py --category "https://www.clickcompetitions.co.uk/competition-category/car-competitions/" \
    --out click_car_comps.csv

Optional:
  --max 50            limit number of competitions processed
  --use-playwright    use JS rendering (recommended if fields missing)
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from typing import Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ----------------------------
# Data model
# ----------------------------

@dataclass
class Competition:
    url: str
    title: Optional[str] = None
    ticket_price_gbp: Optional[float] = None
    draw_datetime: Optional[str] = None
    sales_end_datetime: Optional[str] = None
    tickets_available: Optional[int] = None
    tickets_sold: Optional[int] = None
    cash_alternative_gbp: Optional[float] = None


# ----------------------------
# Helpers
# ----------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CompScraper/1.0; +https://example.com/bot)"
}

MONEY_RE = re.compile(r"£\s*([0-9][0-9,]*\.?[0-9]*)")
INT_RE = re.compile(r"([0-9][0-9,]*)")

def polite_sleep(min_s: float = 0.8, max_s: float = 1.8) -> None:
    time.sleep(random.uniform(min_s, max_s))

def to_float_gbp(text: str) -> Optional[float]:
    if not text:
        return None
    m = MONEY_RE.search(text.replace("\u00a0", " "))
    if not m:
        return None
    return float(m.group(1).replace(",", ""))

def to_int(text: str) -> Optional[int]:
    if not text:
        return None
    m = INT_RE.search(text.replace("\u00a0", " "))
    if not m:
        return None
    return int(m.group(1).replace(",", ""))

def normalise_url(base: str, href: str) -> str:
    return urljoin(base, href)

def get_soup(url: str, session: requests.Session, timeout: int = 25) -> BeautifulSoup:
    r = session.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


# ----------------------------
# Static HTML extraction
# ----------------------------

def extract_competition_links_from_category(category_url: str, soup: BeautifulSoup) -> List[str]:
    """
    Attempts to find competition links from the category listing.
    Strategy:
      - Grab all <a href> containing '/competition/' (excluding category links).
      - De-dupe, keep same host.
    """
    links: Set[str] = set()
    category_host = urlparse(category_url).netloc

    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if "/competition/" not in href:
            continue
        # Skip category pages etc.
        if "/competition-category/" in href:
            continue
        full = normalise_url(category_url, href)
        if urlparse(full).netloc != category_host:
            continue
        # Normalise: strip query/fragment
        full = full.split("#")[0].split("?")[0].rstrip("/") + "/"
        links.add(full)

    return sorted(links)


def extract_comp_details_static(url: str, soup: BeautifulSoup) -> Competition:
    """
    Extract fields from a competition page using multiple heuristics.
    This is intentionally defensive because sites tweak HTML a lot.
    """
    comp = Competition(url=url)

    # Title: usually H1 somewhere near top
    h1 = soup.select_one("h1")
    if h1 and h1.get_text(strip=True):
        comp.title = h1.get_text(" ", strip=True)

    # Ticket price often appears like "£0.33 PER ENTRY"
    # Search for a text node containing "PER ENTRY"
    text = soup.get_text("\n", strip=True)
    # Ticket price
    m_price = re.search(r"£\s*([0-9]+\.[0-9]+)\s*PER\s*ENTRY", text, flags=re.I)
    if m_price:
        comp.ticket_price_gbp = float(m_price.group(1))

    # Tickets available often like "289995 tickets available"
    m_avail = re.search(r"([0-9][0-9,]*)\s*tickets\s*available", text, flags=re.I)
    if m_avail:
        comp.tickets_available = int(m_avail.group(1).replace(",", ""))

    # Tickets sold often like "Tickets Sold: 3033 of 289995"
    m_sold = re.search(r"Tickets\s*Sold:\s*([0-9][0-9,]*)\s*of\s*([0-9][0-9,]*)", text, flags=re.I)
    if m_sold:
        comp.tickets_sold = int(m_sold.group(1).replace(",", ""))
        # If available wasn't found, take the denominator
        if comp.tickets_available is None:
            comp.tickets_available = int(m_sold.group(2).replace(",", ""))

    # Cash alternative often like "Cash Alternative: £47,500"
    m_cash = re.search(r"Cash\s*Alternative:\s*£\s*([0-9][0-9,]*\.?[0-9]*)", text, flags=re.I)
    if m_cash:
        comp.cash_alternative_gbp = float(m_cash.group(1).replace(",", ""))

    # Live draw datetime often like "Live Draw 24th January 2026 @ 8:30 PM"
    m_draw = re.search(r"Live\s*Draw\s*([^\n]+)", text, flags=re.I)
    if m_draw:
        comp.draw_datetime = m_draw.group(1).strip()

    # Ticket sales end often like "Ticket sales end 24th Jan 2026 @ 8:15 PM"
    m_end = re.search(r"Ticket\s*sales\s*end\s*([^\n]+)", text, flags=re.I)
    if m_end:
        comp.sales_end_datetime = m_end.group(1).strip()

    return comp


def is_comp_complete(comp: Competition) -> bool:
    """
    Decide whether static extraction found enough.
    """
    return (
        comp.title is not None and
        comp.ticket_price_gbp is not None and
        (comp.tickets_available is not None or comp.tickets_sold is not None)
    )


# ----------------------------
# Playwright (JS rendered) fallback
# ----------------------------

async def extract_comp_details_playwright(url: str) -> Competition:
    """
    JS-rendered extraction using Playwright.
    Only used if --use-playwright specified.
    """
    from playwright.async_api import async_playwright

    comp = Competition(url=url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60000)

        # Title: h1
        try:
            h1 = await page.text_content("h1")
            if h1:
                comp.title = " ".join(h1.split())
        except Exception:
            pass

        full_text = await page.inner_text("body")
        full_text = full_text.replace("\u00a0", " ")

        m_price = re.search(r"£\s*([0-9]+\.[0-9]+)\s*PER\s*ENTRY", full_text, flags=re.I)
        if m_price:
            comp.ticket_price_gbp = float(m_price.group(1))

        m_avail = re.search(r"([0-9][0-9,]*)\s*tickets\s*available", full_text, flags=re.I)
        if m_avail:
            comp.tickets_available = int(m_avail.group(1).replace(",", ""))

        m_sold = re.search(r"Tickets\s*Sold:\s*([0-9][0-9,]*)\s*of\s*([0-9][0-9,]*)", full_text, flags=re.I)
        if m_sold:
            comp.tickets_sold = int(m_sold.group(1).replace(",", ""))
            if comp.tickets_available is None:
                comp.tickets_available = int(m_sold.group(2).replace(",", ""))

        m_cash = re.search(r"Cash\s*Alternative:\s*£\s*([0-9][0-9,]*\.?[0-9]*)", full_text, flags=re.I)
        if m_cash:
            comp.cash_alternative_gbp = float(m_cash.group(1).replace(",", ""))

        m_draw = re.search(r"Live\s*Draw\s*([^\n]+)", full_text, flags=re.I)
        if m_draw:
            comp.draw_datetime = m_draw.group(1).strip()

        m_end = re.search(r"Ticket\s*sales\s*end\s*([^\n]+)", full_text, flags=re.I)
        if m_end:
            comp.sales_end_datetime = m_end.group(1).strip()

        await browser.close()

    return comp


# ----------------------------
# Value / odds calculations
# ----------------------------

def odds_per_ticket(comp: Competition) -> Optional[float]:
    """Odds per single ticket = 1 / tickets_available (if known)"""
    if comp.tickets_available and comp.tickets_available > 0:
        return 1.0 / comp.tickets_available
    return None

def tickets_per_pound(comp: Competition) -> Optional[float]:
    if comp.ticket_price_gbp and comp.ticket_price_gbp > 0:
        return 1.0 / comp.ticket_price_gbp
    return None

def win_probability_for_spend(comp: Competition, spend_gbp: float) -> Optional[float]:
    """
    Approx chance of winning if you spend £X, assuming:
      - 1 prize
      - You buy floor(spend/price) tickets
      - tickets_available is total pool
    Probability = n / N (without replacement, small n)
    """
    if not (comp.ticket_price_gbp and comp.tickets_available):
        return None
    n = int(spend_gbp / comp.ticket_price_gbp)
    if n <= 0 or comp.tickets_available <= 0:
        return None
    if n > comp.tickets_available:
        n = comp.tickets_available
    return n / comp.tickets_available


# ----------------------------
# Main
# ----------------------------

def write_csv(path: str, comps: List[Competition]) -> None:
    fields = list(asdict(comps[0]).keys()) + [
        "odds_per_ticket",
        "tickets_per_pound",
        "win_prob_for_10gbp",
        "win_prob_for_50gbp",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in comps:
            row = asdict(c)
            row["odds_per_ticket"] = odds_per_ticket(c)
            row["tickets_per_pound"] = tickets_per_pound(c)
            row["win_prob_for_10gbp"] = win_probability_for_spend(c, 10.0)
            row["win_prob_for_50gbp"] = win_probability_for_spend(c, 50.0)
            w.writerow(row)

def write_json(path: str, comps: List[Competition]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(c) for c in comps], f, indent=2, ensure_ascii=False)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", required=True, help="Category URL (e.g., car competitions listing)")
    ap.add_argument("--out", default="competitions.csv", help="Output CSV path")
    ap.add_argument("--out-json", default="competitions.json", help="Output JSON path")
    ap.add_argument("--max", type=int, default=0, help="Max competitions to process (0 = all found)")
    ap.add_argument("--use-playwright", action="store_true", help="Use Playwright (JS) for comp pages if static parse is incomplete")
    ap.add_argument("--delay-min", type=float, default=0.8, help="Min delay between requests (seconds)")
    ap.add_argument("--delay-max", type=float, default=1.8, help="Max delay between requests (seconds)")
    args = ap.parse_args()

    session = requests.Session()

    # 1) Fetch category
    cat_soup = get_soup(args.category, session)
    comp_links = extract_competition_links_from_category(args.category, cat_soup)

    if args.max and args.max > 0:
        comp_links = comp_links[: args.max]

    if not comp_links:
        raise SystemExit("No competition links found on category page. The HTML structure may have changed (or content is JS-rendered). Try --use-playwright and/or share the HTML.")

    comps: List[Competition] = []

    # 2) Visit each comp page
    for i, url in enumerate(comp_links, start=1):
        print(f"[{i}/{len(comp_links)}] {url}")
        try:
            soup = get_soup(url, session)
            comp = extract_comp_details_static(url, soup)

            # Optional JS fallback
            if args.use_playwright and not is_comp_complete(comp):
                import asyncio
                comp = asyncio.run(extract_comp_details_playwright(url))

            comps.append(comp)

        except Exception as e:
            print(f"  !! Failed: {e}")
            comps.append(Competition(url=url))

        time.sleep(random.uniform(args.delay_min, args.delay_max))

    # 3) Output
    if comps:
        write_csv(args.out, comps)
        write_json(args.out_json, comps)
        print(f"Saved: {args.out}")
        print(f"Saved: {args.out_json}")

if __name__ == "__main__":
    main()
