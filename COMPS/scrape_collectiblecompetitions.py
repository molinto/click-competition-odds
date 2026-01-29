#!/usr/bin/env python3
"""
CollectibleCompetitions scraper with odds calculation

Requirements:
  pip install requests beautifulsoup4 playwright
  playwright install chromium

Usage:
  python scrape_collectiblecompetitions.py \
    --root https://collectiblecompetitions.co.uk/ \
    --out collectible_comps.csv \
    --out-json collectible_comps.json \
    --html collectible_comps.html
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ------------------------------------------------------------
# Data model
# ------------------------------------------------------------

@dataclass
class Competition:
    url: str
    title: Optional[str] = None
    ticket_price_gbp: Optional[float] = None
    tickets_sold: Optional[int] = None
    tickets_total: Optional[int] = None
    user_ticket_limit: Optional[int] = None
    cash_alternative_gbp: Optional[float] = None
    competition_end: Optional[str] = None


# ------------------------------------------------------------
# HTTP helpers
# ------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CompScraper/1.0)"
}


def polite_sleep():
    time.sleep(random.uniform(0.6, 1.4))


def get_soup(url: str, session: requests.Session) -> BeautifulSoup:
    r = session.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


# ------------------------------------------------------------
# Link extraction
# ------------------------------------------------------------

def extract_product_links(root_url: str, soup: BeautifulSoup) -> List[str]:
    links: Set[str] = set()
    host = urlparse(root_url).netloc

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/product/" not in href:
            continue

        full = urljoin(root_url, href)
        if urlparse(full).netloc != host:
            continue

        full = full.split("?")[0].split("#")[0].rstrip("/") + "/"
        links.add(full)

    return sorted(links)


# ------------------------------------------------------------
# DOM extraction: tickets sold / total
# ------------------------------------------------------------

def extract_ticket_counts(soup: BeautifulSoup) -> tuple[Optional[int], Optional[int]]:
    """
    Extract tickets sold / total from:
    <div class="wc-comps-tickets-progress-labels">
    """
    sold = total = None

    container = soup.select_one("div.wc-comps-tickets-progress-labels")
    if not container:
        return sold, total

    sold_el = container.select_one(".wc-comps-tickets-sold")
    if sold_el:
        sold_span = sold_el.find("span")
        if sold_span:
            try:
                sold = int(sold_span.get_text(strip=True).replace(",", ""))
            except ValueError:
                sold = None

        text = sold_el.get_text(" ", strip=True)
        m_total = re.search(r"/\s*([0-9,]+)", text)
        if m_total:
            try:
                total = int(m_total.group(1).replace(",", ""))
            except ValueError:
                total = None

    return sold, total


# ------------------------------------------------------------
# Static page extraction
# ------------------------------------------------------------

def extract_competition_static(url: str, soup: BeautifulSoup) -> Competition:
    comp = Competition(url=url)

    # Title
    h1 = soup.select_one("h1")
    if h1:
        comp.title = h1.get_text(strip=True)

    text = soup.get_text("\n", strip=True).replace("\u00a0", " ")

    # User ticket limit
    m_user_limit = re.search(r"User\s*Ticket\s*Limit\s*([0-9,]+)", text, re.I)
    if m_user_limit:
        comp.user_ticket_limit = int(m_user_limit.group(1).replace(",", ""))

    # Cash alternative
    m_cash = re.search(r"£\s*([0-9][0-9,]*)\s*cash\s*alt", text, re.I)
    if m_cash:
        comp.cash_alternative_gbp = float(m_cash.group(1).replace(",", ""))

    # Competition end
    m_end = re.search(r"Competition\s*Ends\s*([^\n]+)", text, re.I)
    if m_end:
        comp.competition_end = m_end.group(1).strip()

    # Tickets sold / total from DOM
    sold, total = extract_ticket_counts(soup)
    comp.tickets_sold = sold
    comp.tickets_total = total

    # Fallback: Ticket Limit (total only)
    if comp.tickets_total is None:
        m_limit = re.search(r"Ticket\s*Limit\s*([0-9,]+)", text, re.I)
        if m_limit:
            comp.tickets_total = int(m_limit.group(1).replace(",", ""))

    return comp


# ------------------------------------------------------------
# Playwright JS extraction (ticket price)
# ------------------------------------------------------------

async def extract_price_from_js(url: str) -> Optional[float]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60000)

        price = await page.evaluate(
            "() => window.item && window.item.price ? parseFloat(window.item.price) : null"
        )

        await browser.close()

    return price


# ------------------------------------------------------------
# Odds calculations
# ------------------------------------------------------------

def odds_per_ticket(comp: Competition) -> Optional[float]:
    if comp.tickets_total and comp.tickets_total > 0:
        return 1.0 / comp.tickets_total
    return None


def win_probability(comp: Competition, spend: float) -> Optional[float]:
    if (
        not comp.ticket_price_gbp
        or not comp.tickets_total
        or comp.tickets_total <= 0
    ):
        return None

    tickets_bought = int(spend / comp.ticket_price_gbp)
    return min(tickets_bought, comp.tickets_total) / comp.tickets_total


# ------------------------------------------------------------
# HTML output
# ------------------------------------------------------------

def write_html(path: str, comps: List[Competition]) -> None:
    comps = sorted(
        comps,
        key=lambda c: (-(win_probability(c, 10) or 0)),
    )

    rows = ""
    for c in comps:
        rows += f"""
        <tr>
          <td><a href="{c.url}" target="_blank">{c.title or c.url}</a></td>
          <td style="text-align:right">£{c.ticket_price_gbp or ""}</td>
          <td style="text-align:right">{c.tickets_sold or ""}</td>
          <td style="text-align:right">{c.tickets_total or ""}</td>
          <td style="text-align:right">
            {((win_probability(c,10) or 0) * 100):.4f}%
          </td>
          <td>{c.competition_end or ""}</td>
        </tr>
        """

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Collectible Competitions – Odds</title>
<style>
body {{ background:#0b0f17; color:#e8eefc; font-family:system-ui }}
table {{ width:100%; border-collapse:collapse; font-size:13px }}
th,td {{ padding:10px; border-bottom:1px solid #243145 }}
a {{ color:#7aa2ff; text-decoration:none }}
</style>
</head>
<body>
<h2>Collectible Competitions – Odds</h2>
<table>
<thead>
<tr>
<th>Competition</th>
<th>Ticket £</th>
<th>Sold</th>
<th>Total</th>
<th>Win % (£10)</th>
<th>Ends</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="collectible_comps.csv")
    ap.add_argument("--out-json", default="collectible_comps.json")
    ap.add_argument("--html", default="collectible_comps.html")
    args = ap.parse_args()

    session = requests.Session()

    root_soup = get_soup(args.root, session)
    links = extract_product_links(args.root, root_soup)

    comps: List[Competition] = []

    for i, url in enumerate(links, 1):
        print(f"[{i}/{len(links)}] {url}")

        try:
            soup = get_soup(url, session)
            comp = extract_competition_static(url, soup)

            if comp.ticket_price_gbp is None:
                comp.ticket_price_gbp = asyncio.run(
                    extract_price_from_js(url)
                )

            comps.append(comp)

        except Exception as e:
            print("  !! Failed:", e)
            comps.append(Competition(url=url))

        polite_sleep()

    # CSV
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=asdict(comps[0]).keys())
        writer.writeheader()
        for c in comps:
            writer.writerow(asdict(c))

    # JSON
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump([asdict(c) for c in comps], f, indent=2)

    # HTML
    write_html(args.html, comps)

    print("Done.")


if __name__ == "__main__":
    main()
