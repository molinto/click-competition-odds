#!/usr/bin/env python3
"""
ClickCompetitions scraper + combiner (multi-category -> combined CSV + HTML)

Examples:
  python COMPS/scrape_clickcompetitions_all.py \
    --category Cash="https://www.clickcompetitions.co.uk/competition-category/cash-competitions/" \
    --category Cars="https://www.clickcompetitions.co.uk/competition-category/car-competitions/" \
    --category Tech="https://www.clickcompetitions.co.uk/competition-category/tech-competitions/" \
    --category Daily="https://www.clickcompetitions.co.uk/competition-category/daily-deals/" \
    --out combined_click_comps.csv \
    --html out/index.html

Optional:
  --max-per-category 50
  --use-playwright
  --per-category-out-dir out/categories
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ----------------------------
# Data model
# ----------------------------

@dataclass
class Competition:
    category: str
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
    "User-Agent": "Mozilla/5.0 (compatible; CompScraper/1.2; +https://example.com/bot)"
}

MONEY_RE = re.compile(r"£\s*([0-9][0-9,]*\.?[0-9]*)", re.I)
INT_RE = re.compile(r"([0-9][0-9,]*)")

def polite_sleep(min_s: float, max_s: float) -> None:
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
# Link extraction
# ----------------------------

def extract_competition_links_from_category(category_url: str, soup: BeautifulSoup) -> List[str]:
    """
    Find competition links from category listing.
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
        if "/competition-category/" in href:
            continue

        full = normalise_url(category_url, href)
        if urlparse(full).netloc != category_host:
            continue

        # Strip query/fragment + normalise trailing slash
        full = full.split("#")[0].split("?")[0].rstrip("/") + "/"
        links.add(full)

    return sorted(links)


# ----------------------------
# Static extraction
# ----------------------------

def extract_comp_details_static(category: str, url: str, soup: BeautifulSoup) -> Competition:
    comp = Competition(category=category, url=url)

    # Title
    h1 = soup.select_one("h1")
    if h1 and h1.get_text(strip=True):
        comp.title = h1.get_text(" ", strip=True)

    text = soup.get_text("\n", strip=True).replace("\u00a0", " ")

    # Ticket price: sometimes "£0.33 PER ENTRY" or "£0.33 per entry"
    m_price = re.search(r"£\s*([0-9]+(?:\.[0-9]+)?)\s*PER\s*ENTRY", text, flags=re.I)
    if m_price:
        comp.ticket_price_gbp = float(m_price.group(1))

    # Tickets available: "289,995 tickets available"
    m_avail = re.search(r"([0-9][0-9,]*)\s*tickets\s*available", text, flags=re.I)
    if m_avail:
        comp.tickets_available = int(m_avail.group(1).replace(",", ""))

    # Tickets sold: "Tickets Sold: 3033 of 289995"
    m_sold = re.search(r"Tickets\s*Sold:\s*([0-9][0-9,]*)\s*of\s*([0-9][0-9,]*)", text, flags=re.I)
    if m_sold:
        comp.tickets_sold = int(m_sold.group(1).replace(",", ""))
        if comp.tickets_available is None:
            comp.tickets_available = int(m_sold.group(2).replace(",", ""))

    # Cash alternative
    m_cash = re.search(r"Cash\s*Alternative:\s*£\s*([0-9][0-9,]*\.?[0-9]*)", text, flags=re.I)
    if m_cash:
        comp.cash_alternative_gbp = float(m_cash.group(1).replace(",", ""))

    # Draw datetime
    m_draw = re.search(r"Live\s*Draw\s*([^\n]+)", text, flags=re.I)
    if m_draw:
        comp.draw_datetime = m_draw.group(1).strip()

    # Sales end datetime
    m_end = re.search(r"Ticket\s*sales\s*end\s*([^\n]+)", text, flags=re.I)
    if m_end:
        comp.sales_end_datetime = m_end.group(1).strip()

    return comp

def is_comp_complete(comp: Competition) -> bool:
    return (
        comp.title is not None and
        comp.ticket_price_gbp is not None and
        (comp.tickets_available is not None or comp.tickets_sold is not None)
    )


# ----------------------------
# Playwright fallback
# ----------------------------

async def extract_comp_details_playwright(category: str, url: str) -> Competition:
    from playwright.async_api import async_playwright

    comp = Competition(category=category, url=url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60000)

        try:
            h1 = await page.text_content("h1")
            if h1:
                comp.title = " ".join(h1.split())
        except Exception:
            pass

        full_text = (await page.inner_text("body")).replace("\u00a0", " ")

        m_price = re.search(r"£\s*([0-9]+(?:\.[0-9]+)?)\s*PER\s*ENTRY", full_text, flags=re.I)
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
# Calculations
# ----------------------------

def odds_per_ticket(comp: Competition) -> Optional[float]:
    if comp.tickets_available and comp.tickets_available > 0:
        return 1.0 / comp.tickets_available
    return None

def win_probability_for_spend(comp: Competition, spend_gbp: float) -> Optional[float]:
    """
    Approx chance with spend, assuming:
      - 1 prize
      - You buy floor(spend/price) tickets
      - tickets_available is total pool

    For single-prize: P(win) = n/N
    """
    if not (comp.ticket_price_gbp and comp.tickets_available):
        return None
    if comp.ticket_price_gbp <= 0 or comp.tickets_available <= 0:
        return None

    n = int(spend_gbp / comp.ticket_price_gbp)
    if n <= 0:
        return 0.0
    n = min(n, comp.tickets_available)
    return n / comp.tickets_available

def company_revenue_gbp(comp: Competition) -> Optional[float]:
    if comp.ticket_price_gbp is None or comp.tickets_available is None:
        return None
    return comp.ticket_price_gbp * comp.tickets_available

def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return ""
    return f"£{x:,.2f}"

def fmt_pct(p: Optional[float]) -> str:
    if p is None:
        return ""
    return f"{p*100:.4f}%"

def safe_title(comp: Competition) -> str:
    return comp.title or comp.url


# ----------------------------
# Output
# ----------------------------

OUTPUT_FIELDS = [
    "category",
    "url",
    "title",
    "ticket_price_gbp",
    "draw_datetime",
    "sales_end_datetime",
    "tickets_available",
    "tickets_sold",
    "cash_alternative_gbp",
    "odds_per_ticket",
    "win_prob_for_10gbp",
    "win_prob_for_20gbp",
    "company_revenue_gbp",
]

def write_csv(path: Path, comps: List[Competition]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        w.writeheader()
        for c in comps:
            row = asdict(c)
            row["odds_per_ticket"] = odds_per_ticket(c)
            row["win_prob_for_10gbp"] = win_probability_for_spend(c, 10.0)
            row["win_prob_for_20gbp"] = win_probability_for_spend(c, 20.0)
            row["company_revenue_gbp"] = company_revenue_gbp(c)
            w.writerow(row)

def write_json(path: Path, comps: List[Competition]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for c in comps:
        row = asdict(c)
        row["odds_per_ticket"] = odds_per_ticket(c)
        row["win_prob_for_10gbp"] = win_probability_for_spend(c, 10.0)
        row["win_prob_for_20gbp"] = win_probability_for_spend(c, 20.0)
        row["company_revenue_gbp"] = company_revenue_gbp(c)
        payload.append(row)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

def write_html(path: Path, comps: List[Competition]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    # Sort by £10 probability desc, then revenue asc (nice tie-break)
    def sort_key(c: Competition):
        p10 = win_probability_for_spend(c, 10.0) or 0.0
        rev = company_revenue_gbp(c)
        rev_val = rev if rev is not None else float("inf")
        return (-p10, rev_val)

    sorted_comps = sorted(comps, key=sort_key)

    # Build rows
    def row_html(c: Competition) -> str:
        p10 = win_probability_for_spend(c, 10.0)
        p20 = win_probability_for_spend(c, 20.0)
        rev = company_revenue_gbp(c)
        price = c.ticket_price_gbp
        avail = c.tickets_available
        sold = c.tickets_sold

        return f"""
          <tr>
            <td class="cat">{c.category}</td>
            <td class="title">
              <a href="{c.url}" target="_blank" rel="noopener noreferrer">{safe_title(c)}</a>
            </td>
            <td class="num">{fmt_money(price) if price is not None else ""}</td>
            <td class="num">{avail if avail is not None else ""}</td>
            <td class="num">{sold if sold is not None else ""}</td>
            <td class="num">{fmt_pct(p10)}</td>
            <td class="num">{fmt_pct(p20)}</td>
            <td class="num">{fmt_money(rev)}</td>
            <td class="meta">{c.sales_end_datetime or ""}</td>
          </tr>
        """

    rows = "\n".join(row_html(c) for c in sorted_comps)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Click Competitions – Best Odds</title>
  <style>
    :root {{
      --bg: #0b0f17;
      --panel: #121a26;
      --text: #e8eefc;
      --muted: #a9b6d1;
      --border: #243145;
      --accent: #7aa2ff;
      --good: #3ddc97;
      --warn: #ffcc66;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji";
      background: var(--bg);
      color: var(--text);
    }}
    .wrap {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px 16px 64px;
    }}
    header {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-bottom: 16px;
    }}
    h1 {{
      font-size: 22px;
      margin: 0;
      letter-spacing: 0.2px;
    }}
    .sub {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }}
    .controls {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 14px 0 18px;
    }}
    input, select {{
      background: var(--panel);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 10px 12px;
      border-radius: 10px;
      outline: none;
      min-width: 220px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(0,0,0,.25);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    thead th {{
      text-align: left;
      padding: 12px 12px;
      border-bottom: 1px solid var(--border);
      color: var(--muted);
      font-weight: 600;
      position: sticky;
      top: 0;
      background: rgba(18,26,38,0.98);
      backdrop-filter: blur(8px);
    }}
    tbody td {{
      padding: 12px 12px;
      border-bottom: 1px solid rgba(36,49,69,0.55);
      vertical-align: top;
    }}
    tbody tr:hover {{
      background: rgba(122,162,255,0.08);
    }}
    .title a {{
      color: var(--text);
      text-decoration: none;
    }}
    .title a:hover {{
      text-decoration: underline;
      color: var(--accent);
    }}
    .num {{
      text-align: right;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}
    .cat {{
      font-weight: 700;
      color: var(--accent);
      white-space: nowrap;
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
      max-width: 260px;
    }}
    .pill {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      color: var(--muted);
      font-size: 12px;
    }}
    .footer {{
      margin-top: 12px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 860px) {{
      thead th:nth-child(4),
      tbody td:nth-child(4),
      thead th:nth-child(5),
      tbody td:nth-child(5),
      thead th:nth-child(8),
      tbody td:nth-child(8),
      thead th:nth-child(9),
      tbody td:nth-child(9) {{
        display: none;
      }}
      input, select {{ min-width: 160px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Best odds (Click Competitions)</h1>
      <div class="sub">
        Sorted by <span class="pill">Win chance with £10</span> (desc). Also shows <span class="pill">£20</span> and estimated gross revenue per comp (price × total tickets).
      </div>
    </header>

    <div class="controls">
      <input id="q" placeholder="Search title…" />
      <select id="cat">
        <option value="">All categories</option>
      </select>
      <select id="sort">
        <option value="p10">Sort: Win % (£10) desc</option>
        <option value="p20">Sort: Win % (£20) desc</option>
        <option value="rev">Sort: Revenue asc</option>
        <option value="rev_desc">Sort: Revenue desc</option>
      </select>
    </div>

    <div class="card">
      <table id="tbl">
        <thead>
          <tr>
            <th>Category</th>
            <th>Competition</th>
            <th class="num">Ticket £</th>
            <th class="num">Tickets</th>
            <th class="num">Sold</th>
            <th class="num">Win % (£10)</th>
            <th class="num">Win % (£20)</th>
            <th class="num">Revenue</th>
            <th>Sales end</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>

    <div class="footer">
      Generated by scrape_clickcompetitions_all.py
    </div>
  </div>

  <script>
    const table = document.getElementById('tbl');
    const tbody = table.querySelector('tbody');
    const q = document.getElementById('q');
    const cat = document.getElementById('cat');
    const sort = document.getElementById('sort');

    function parsePct(s) {{
      if (!s) return 0;
      return parseFloat(String(s).replace('%','')) || 0;
    }}
    function parseMoney(s) {{
      if (!s) return NaN;
      return parseFloat(String(s).replace('£','').replaceAll(',','')) || NaN;
    }}

    // Build category dropdown from table content
    const cats = new Set();
    [...tbody.querySelectorAll('tr')].forEach(tr => {{
      cats.add(tr.children[0].textContent.trim());
    }});
    [...cats].sort().forEach(c => {{
      const opt = document.createElement('option');
      opt.value = c;
      opt.textContent = c;
      cat.appendChild(opt);
    }});

    function apply() {{
      const query = q.value.trim().toLowerCase();
      const catVal = cat.value;

      const rows = [...tbody.querySelectorAll('tr')];

      // Filter
      rows.forEach(tr => {{
        const title = tr.children[1].textContent.toLowerCase();
        const c = tr.children[0].textContent.trim();
        const okQ = !query || title.includes(query);
        const okC = !catVal || c === catVal;
        tr.style.display = (okQ && okC) ? '' : 'none';
      }});

      // Sort (only visible rows)
      const visible = rows.filter(r => r.style.display !== 'none');
      const key = sort.value;

      visible.sort((a,b) => {{
        const aP10 = parsePct(a.children[5].textContent);
        const bP10 = parsePct(b.children[5].textContent);
        const aP20 = parsePct(a.children[6].textContent);
        const bP20 = parsePct(b.children[6].textContent);
        const aRev = parseMoney(a.children[7].textContent);
        const bRev = parseMoney(b.children[7].textContent);

        if (key === 'p20') return bP20 - aP20;
        if (key === 'rev') return (aRev - bRev);
        if (key === 'rev_desc') return (bRev - aRev);
        return bP10 - aP10; // default p10
      }});

      // Re-append sorted visible rows, keep hidden ones at end
      visible.forEach(tr => tbody.appendChild(tr));
      rows.filter(r => r.style.display === 'none').forEach(tr => tbody.appendChild(tr));
    }}

    q.addEventListener('input', apply);
    cat.addEventListener('change', apply);
    sort.addEventListener('change', apply);
  </script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


# ----------------------------
# Main orchestration
# ----------------------------

def parse_category_arg(s: str) -> (str, str):
    """
    Expect: Name="https://..."
    """
    if "=" not in s:
        raise argparse.ArgumentTypeError('Category must be like Name="https://..."')
    name, url = s.split("=", 1)
    name = name.strip().strip('"').strip("'")
    url = url.strip().strip('"').strip("'")
    if not url.lower().startswith("http"):
        raise argparse.ArgumentTypeError("Category URL must start with http(s)")
    return name, url

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--category",
        action="append",
        type=parse_category_arg,
        required=True,
        help='Repeatable. Example: --category Tech="https://.../tech-competitions/"',
    )
    ap.add_argument("--out", default="combined_click_comps.csv", help="Combined output CSV path")
    ap.add_argument("--out-json", default="combined_click_comps.json", help="Combined output JSON path")
    ap.add_argument("--html", default="combined_click_comps.html", help="Output HTML path")
    ap.add_argument("--per-category-out-dir", default="", help="Optional: write category CSVs into this folder")
    ap.add_argument("--max-per-category", type=int, default=0, help="Limit per category (0=all found)")
    ap.add_argument("--use-playwright", action="store_true", help="Use Playwright for comp pages if static parse incomplete")
    ap.add_argument("--delay-min", type=float, default=0.8)
    ap.add_argument("--delay-max", type=float, default=1.8)
    args = ap.parse_args()

    session = requests.Session()

    all_comps: List[Competition] = []

    for (cat_name, cat_url) in args.category:
        print(f"\n== Category: {cat_name} ==\n{cat_url}")
        cat_soup = get_soup(cat_url, session)
        comp_links = extract_competition_links_from_category(cat_url, cat_soup)

        if args.max_per_category and args.max_per_category > 0:
            comp_links = comp_links[: args.max_per_category]

        if not comp_links:
            print("  !! No competition links found (try --use-playwright or inspect category HTML).")
            continue

        comps: List[Competition] = []
        for i, url in enumerate(comp_links, start=1):
            print(f"  [{i}/{len(comp_links)}] {url}")
            try:
                soup = get_soup(url, session)
                comp = extract_comp_details_static(cat_name, url, soup)

                if args.use_playwright and not is_comp_complete(comp):
                    import asyncio
                    comp = asyncio.run(extract_comp_details_playwright(cat_name, url))

                comps.append(comp)
            except Exception as e:
                print(f"    !! Failed: {e}")
                comps.append(Competition(category=cat_name, url=url))

            polite_sleep(args.delay_min, args.delay_max)

        # Optional per-category CSV
        if args.per_category_out_dir:
            out_dir = Path(args.per_category_out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", cat_name.strip())
            write_csv(out_dir / f"{safe}.csv", comps)

        all_comps.extend(comps)

    # Combined outputs
    out_csv = Path(args.out)
    out_json = Path(args.out_json)
    out_html = Path(args.html)

    write_csv(out_csv, all_comps)
    write_json(out_json, all_comps)
    write_html(out_html, all_comps)

    print(f"\nSaved combined CSV:  {out_csv}")
    print(f"Saved combined JSON: {out_json}")
    print(f"Saved HTML:          {out_html}")

if __name__ == "__main__":
    main()