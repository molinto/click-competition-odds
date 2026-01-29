#!/usr/bin/env python3
"""
EastCoastRaffles scraper (multi-category -> combined CSV + HTML with odds)

Examples:
  python scrape_eastcoastraffles_all.py \
    --category AutoDraw="https://eastcoastraffles.co.uk/competition-category/auto-draw/" \
    --category Wednesday="https://eastcoastraffles.co.uk/competition-category/wednesday/" \
    --category Sunday="https://eastcoastraffles.co.uk/competition-category/sunday/" \
    --out out/ecr_comps.csv \
    --out-json out/ecr_comps.json \
    --html out/index.html

Optional:
  --max-per-category 50
  --use-playwright
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple
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
    tickets_total: Optional[int] = None
    tickets_sold: Optional[int] = None
    max_tickets_per_user: Optional[int] = None
    cash_alternative_gbp: Optional[float] = None


# ----------------------------
# Helpers
# ----------------------------

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CompScraper/1.0)"}

MONEY_ANY_RE = re.compile(r"£\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
INT_RE = re.compile(r"([0-9][0-9,]*)")

def polite_sleep(min_s: float, max_s: float) -> None:
    time.sleep(random.uniform(min_s, max_s))

def get_soup(url: str, session: requests.Session, timeout: int = 30) -> BeautifulSoup:
    r = session.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def normalise_url(base: str, href: str) -> str:
    full = urljoin(base, href)
    return full.split("#")[0].split("?")[0].rstrip("/") + "/"

def to_int(text: str) -> Optional[int]:
    if not text:
        return None
    m = INT_RE.search(text.replace("\u00a0", " "))
    if not m:
        return None
    return int(m.group(1).replace(",", ""))

def to_float_money(text: str) -> Optional[float]:
    if not text:
        return None
    m = MONEY_ANY_RE.search(text.replace("\u00a0", " "))
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


# ----------------------------
# Link extraction (category pages)
# ----------------------------

def extract_comp_links_from_category(category_url: str, soup: BeautifulSoup) -> List[str]:
    links: Set[str] = set()
    host = urlparse(category_url).netloc

    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if "/competition/" not in href:
            continue
        if "/competition-category/" in href:
            continue

        full = normalise_url(category_url, href)
        if urlparse(full).netloc != host:
            continue

        links.add(full)

    return sorted(links)


# ----------------------------
# Competition page extraction (static)
# ----------------------------

def _extract_meta_lines(soup: BeautifulSoup) -> List[str]:
    # Common pattern on raffle sites: a bullet list / inline list of key facts.
    # We'll grab list items if present, else fall back to "header area" text.
    candidates = []

    for sel in [
        ".competition-meta li",
        ".competition-meta *",
        ".entry-meta li",
        ".entry-meta *",
    ]:
        els = soup.select(sel)
        if els:
            txts = [e.get_text(" ", strip=True) for e in els]
            txts = [t for t in txts if t and len(t) < 200]
            # de-dupe while preserving order
            seen = set()
            for t in txts:
                if t not in seen:
                    candidates.append(t)
                    seen.add(t)
            if candidates:
                break

    return candidates

def _extract_sold_total_from_progress_text(text: str) -> Tuple[Optional[int], Optional[int]]:
    # e.g. "87 / 3000" or "38750 / 76500"
    m = re.search(r"([0-9][0-9,]*)\s*/\s*([0-9][0-9,]*)", text)
    if not m:
        return None, None
    sold = int(m.group(1).replace(",", ""))
    total = int(m.group(2).replace(",", ""))
    return sold, total

def _guess_ticket_price_from_page_text(full_text: str) -> Optional[float]:
    """
    East Coast pages contain multiple £ amounts:
      - ticket price (often < £10 and often has decimals like £0.49)
      - cash alternative (often bigger)
    Heuristic:
      - collect all £ amounts
      - prefer the SMALLEST positive amount that is <= 9.99
      - if any have decimals, prefer those (common for ticket price)
    """
    vals = []
    for m in MONEY_ANY_RE.finditer(full_text.replace("\u00a0", " ")):
        try:
            vals.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue

    vals = [v for v in vals if v > 0]
    if not vals:
        return None

    small = [v for v in vals if v <= 9.99]
    if not small:
        return None

    # Prefer decimal-ish (like 0.49 / 0.50)
    decimals = [v for v in small if abs(v - round(v)) > 1e-9]
    if decimals:
        return min(decimals)
    return min(small)

def extract_comp_details_static(category: str, url: str, soup: BeautifulSoup) -> Competition:
    comp = Competition(category=category, url=url)

    # Title
    h1 = soup.select_one("h1")
    if h1:
        comp.title = h1.get_text(" ", strip=True) or None

    # Full text
    full_text = soup.get_text("\n", strip=True).replace("\u00a0", " ")

    # Ticket price (heuristic)
    comp.ticket_price_gbp = _guess_ticket_price_from_page_text(full_text)

    # Progress bar (sold/total)
    progress_text = ""
    for sel in [".progress-bar", ".progress", ".wc-comps-progress", ".tickets-progress"]:
        el = soup.select_one(sel)
        if el:
            progress_text = el.get_text(" ", strip=True)
            break

    sold, total = _extract_sold_total_from_progress_text(progress_text or full_text)
    comp.tickets_sold = sold
    comp.tickets_total = total

    # Meta bullets (tickets available, max tickets, live draw, cash alt)
    meta_lines = _extract_meta_lines(soup)
    meta_blob = "\n".join(meta_lines) if meta_lines else full_text

    # tickets available
    m_total = re.search(r"([0-9][0-9,]*)\s*tickets\s*available", meta_blob, re.I)
    if m_total:
        comp.tickets_total = comp.tickets_total or int(m_total.group(1).replace(",", ""))

    # max tickets per user
    m_max = re.search(r"max\s*tickets\s*per\s*user\s*([0-9][0-9,]*)", meta_blob, re.I)
    if m_max:
        comp.max_tickets_per_user = int(m_max.group(1).replace(",", ""))

    # live draw
    m_draw = re.search(r"live\s*draw\s*([^\n]+)", meta_blob, re.I)
    if m_draw:
        comp.draw_datetime = m_draw.group(1).strip()

    # cash alternative
    m_cash = re.search(r"cash\s*alternative\s*:\s*£\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", meta_blob, re.I)
    if m_cash:
        comp.cash_alternative_gbp = float(m_cash.group(1).replace(",", ""))

    return comp

def is_comp_complete(comp: Competition) -> bool:
    return (
        comp.title is not None
        and comp.ticket_price_gbp is not None
        and comp.tickets_total is not None
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

        body_text = (await page.inner_text("body")).replace("\u00a0", " ")

        comp.ticket_price_gbp = _guess_ticket_price_from_page_text(body_text)

        # progress
        try:
            prog = await page.inner_text(".progress-bar")
        except Exception:
            prog = body_text

        sold, total = _extract_sold_total_from_progress_text(prog)
        comp.tickets_sold = sold
        comp.tickets_total = total

        # meta
        # Try to collect likely meta lines, else just parse body_text.
        meta_blob = body_text
        try:
            meta_lines = await page.eval_on_selector_all(
                ".competition-meta li",
                "els => els.map(e => e.innerText)"
            )
            if meta_lines:
                meta_blob = "\n".join([m.strip() for m in meta_lines if m and m.strip()])
        except Exception:
            pass

        m_total = re.search(r"([0-9][0-9,]*)\s*tickets\s*available", meta_blob, re.I)
        if m_total:
            comp.tickets_total = comp.tickets_total or int(m_total.group(1).replace(",", ""))

        m_max = re.search(r"max\s*tickets\s*per\s*user\s*([0-9][0-9,]*)", meta_blob, re.I)
        if m_max:
            comp.max_tickets_per_user = int(m_max.group(1).replace(",", ""))

        m_draw = re.search(r"live\s*draw\s*([^\n]+)", meta_blob, re.I)
        if m_draw:
            comp.draw_datetime = m_draw.group(1).strip()

        m_cash = re.search(r"cash\s*alternative\s*:\s*£\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", meta_blob, re.I)
        if m_cash:
            comp.cash_alternative_gbp = float(m_cash.group(1).replace(",", ""))

        await browser.close()

    return comp


# ----------------------------
# Odds calcs (same idea as your other scripts)
# ----------------------------

def win_probability_for_spend(comp: Competition, spend_gbp: float) -> Optional[float]:
    if not comp.ticket_price_gbp or not comp.tickets_total or comp.tickets_total <= 0:
        return None

    n = int(spend_gbp / comp.ticket_price_gbp)
    if n <= 0:
        return 0.0

    # honour max per user if present
    if comp.max_tickets_per_user is not None:
        n = min(n, comp.max_tickets_per_user)

    n = min(n, comp.tickets_total)
    return n / comp.tickets_total

def company_revenue_gbp(comp: Competition) -> Optional[float]:
    if comp.ticket_price_gbp is None or comp.tickets_total is None:
        return None
    return comp.ticket_price_gbp * comp.tickets_total

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
    "tickets_total",
    "tickets_sold",
    "max_tickets_per_user",
    "cash_alternative_gbp",
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
            row["win_prob_for_10gbp"] = win_probability_for_spend(c, 10.0)
            row["win_prob_for_20gbp"] = win_probability_for_spend(c, 20.0)
            row["company_revenue_gbp"] = company_revenue_gbp(c)
            w.writerow(row)

def write_json(path: Path, comps: List[Competition]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for c in comps:
        row = asdict(c)
        row["win_prob_for_10gbp"] = win_probability_for_spend(c, 10.0)
        row["win_prob_for_20gbp"] = win_probability_for_spend(c, 20.0)
        row["company_revenue_gbp"] = company_revenue_gbp(c)
        payload.append(row)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

def write_html(path: Path, comps: List[Competition]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def sort_key(c: Competition):
        p10 = win_probability_for_spend(c, 10.0) or 0.0
        rev = company_revenue_gbp(c)
        rev_val = rev if rev is not None else float("inf")
        return (-p10, rev_val)

    sorted_comps = sorted(comps, key=sort_key)

    def row_html(c: Competition) -> str:
        p10 = win_probability_for_spend(c, 10.0)
        p20 = win_probability_for_spend(c, 20.0)
        rev = company_revenue_gbp(c)

        return f"""
          <tr>
            <td class="cat">{c.category}</td>
            <td class="title">
              <a href="{c.url}" target="_blank" rel="noopener noreferrer">{safe_title(c)}</a>
            </td>
            <td class="num">{fmt_money(c.ticket_price_gbp) if c.ticket_price_gbp is not None else ""}</td>
            <td class="num">{c.tickets_total if c.tickets_total is not None else ""}</td>
            <td class="num">{c.tickets_sold if c.tickets_sold is not None else ""}</td>
            <td class="num">{fmt_pct(p10)}</td>
            <td class="num">{fmt_pct(p20)}</td>
            <td class="num">{fmt_money(rev)}</td>
            <td class="meta">{c.draw_datetime or ""}</td>
          </tr>
        """

    rows = "\n".join(row_html(c) for c in sorted_comps)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>East Coast Raffles – Best Odds</title>
  <style>
    :root {{
      --bg: #0b0f17;
      --panel: #121a26;
      --text: #e8eefc;
      --muted: #a9b6d1;
      --border: #243145;
      --accent: #7aa2ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      background: var(--bg);
      color: var(--text);
    }}
    .wrap {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px 16px 64px;
    }}
    header {{ display:flex; flex-direction:column; gap:8px; margin-bottom: 16px; }}
    h1 {{ font-size: 22px; margin: 0; }}
    .sub {{ color: var(--muted); font-size: 13px; line-height: 1.4; }}
    .controls {{ display:flex; gap:10px; flex-wrap:wrap; margin: 14px 0 18px; }}
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
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
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
    tbody tr:hover {{ background: rgba(122,162,255,0.08); }}
    .title a {{ color: var(--text); text-decoration: none; }}
    .title a:hover {{ text-decoration: underline; color: var(--accent); }}
    .num {{ text-align:right; white-space:nowrap; font-variant-numeric: tabular-nums; }}
    .cat {{ font-weight: 700; color: var(--accent); white-space: nowrap; }}
    .meta {{ color: var(--muted); font-size: 12px; max-width: 260px; }}
    @media (max-width: 860px) {{
      thead th:nth-child(4), tbody td:nth-child(4),
      thead th:nth-child(5), tbody td:nth-child(5),
      thead th:nth-child(8), tbody td:nth-child(8),
      thead th:nth-child(9), tbody td:nth-child(9) {{
        display: none;
      }}
      input, select {{ min-width: 160px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Best odds (East Coast Raffles)</h1>
      <div class="sub">Sorted by win chance with <b>£10</b> (desc). Also shows <b>£20</b> and estimated gross revenue (price × total tickets).</div>
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
            <th>Draw</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>
  </div>

  <script>
    const tbody = document.querySelector('#tbl tbody');
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

    // Categories dropdown
    const cats = new Set();
    [...tbody.querySelectorAll('tr')].forEach(tr => cats.add(tr.children[0].textContent.trim()));
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

      rows.forEach(tr => {{
        const title = tr.children[1].textContent.toLowerCase();
        const c = tr.children[0].textContent.trim();
        const okQ = !query || title.includes(query);
        const okC = !catVal || c === catVal;
        tr.style.display = (okQ && okC) ? '' : 'none';
      }});

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
        return bP10 - aP10;
      }});

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
# CLI
# ----------------------------

def parse_category_arg(s: str) -> Tuple[str, str]:
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
    ap.add_argument("--category", action="append", type=parse_category_arg, required=True)
    ap.add_argument("--out", default="ecr_comps.csv")
    ap.add_argument("--out-json", default="ecr_comps.json")
    ap.add_argument("--html", default="ecr_comps.html")
    ap.add_argument("--max-per-category", type=int, default=0)
    ap.add_argument("--use-playwright", action="store_true")
    ap.add_argument("--delay-min", type=float, default=0.8)
    ap.add_argument("--delay-max", type=float, default=1.8)
    args = ap.parse_args()

    session = requests.Session()
    all_comps: List[Competition] = []

    for (cat_name, cat_url) in args.category:
        print(f"\n== Category: {cat_name} ==\n{cat_url}")
        soup = get_soup(cat_url, session)
        links = extract_comp_links_from_category(cat_url, soup)

        if args.max_per_category and args.max_per_category > 0:
            links = links[: args.max_per_category]

        for i, url in enumerate(links, 1):
            print(f"  [{i}/{len(links)}] {url}")
            try:
                comp_soup = get_soup(url, session)
                comp = extract_comp_details_static(cat_name, url, comp_soup)

                if args.use_playwright and not is_comp_complete(comp):
                    import asyncio
                    comp = asyncio.run(extract_comp_details_playwright(cat_name, url))

                all_comps.append(comp)
            except Exception as e:
                print(f"    !! Failed: {e}")
                all_comps.append(Competition(category=cat_name, url=url))

            polite_sleep(args.delay_min, args.delay_max)

    out_csv = Path(args.out)
    out_json = Path(args.out_json)
    out_html = Path(args.html)

    write_csv(out_csv, all_comps)
    write_json(out_json, all_comps)
    write_html(out_html, all_comps)

    print(f"\nSaved CSV:  {out_csv}")
    print(f"Saved JSON: {out_json}")
    print(f"Saved HTML: {out_html}")

if __name__ == "__main__":
    main()
