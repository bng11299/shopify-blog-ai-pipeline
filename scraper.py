"""
scraper.py
----------
Step 1 of the SEO article pipeline.

Loads the SerpReport project via its public *view key* URL (no login needed),
reads the full keyword table (paginating through all pages), keeps only
keywords whose **Latest Position** is in the 8-12 band, scores them, drops any
keyword already written about, and writes the single best candidate to
keyword.json for generator.py to consume.

Unchanged from the WordPress pipeline — SerpReport is the same keyword source
regardless of where the article is published.

Scoring
  trend  (from the "Change" column sign):
      Change < 0  -> position improved (up)    +3
      Change = 0  -> stable                     +1
      Change > 0  -> position dropped          -2
  local volume bonus: + min(L-Vol / 100, 10)
  position tiebreak : + (12 - latest) * 0.1   (closer to page 1 wins ties)

Exclusions
  - Latest Position outside 8-12 (inclusive)
  - keyword already in posted_keywords.json

Dependencies
  pip install playwright
  playwright install chromium

Usage
  python scraper.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

# Ensure UTF-8 stdout so status output (arrows, checkmarks) survives the console.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# ── Config ──────────────────────────────────────────────────────────────────

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a local .env into the environment (shell wins).
    Same lightweight loader as common.py; keeps secrets out of the code."""
    if not ENV_FILE.exists():
        return
    try:
        lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()

# The SerpReport view-key URL is a SHARED SECRET (the view_key grants read
# access to the whole project) — it lives in .env, never in code or git.
# .env line:  SERPREPORT_VIEW_URL=https://serpreports.com/app/project/<id>?view_key=<token>
SERP_VIEW_URL = os.environ.get("SERPREPORT_VIEW_URL", "").strip()
# Tolerate a scheme-less URL in .env (Playwright rejects "serpreports.com/...").
if SERP_VIEW_URL and not SERP_VIEW_URL.startswith(("http://", "https://")):
    SERP_VIEW_URL = "https://" + SERP_VIEW_URL

# Latest-Position band we target (inclusive).
MIN_POSITION = 8
MAX_POSITION = 12

# Ignore keywords with negligible local search demand.
MIN_LOCAL_VOL = 10

POSTED_LOG = HERE / "posted_keywords.json"   # keywords already published
KEYWORD_OUT = HERE / "keyword.json"          # output for generator.py

# Column indices within each grid row (div.cdt-row > direct child divs).
# 0:checkbox 1:Keyword 2:Change 3:Latest 4:Best 5:First 6:L-Vol 7:G-Vol
# 8:L-CPC 9:G-CPC 10:L-CMP 11:G-CMP 12:Updated 13:URL Found 14:Actions
COL_KEYWORD = 1
COL_CHANGE = 2
COL_LATEST = 3
COL_LVOL = 6
COL_URL = 13

MAX_PAGES = 30  # safety cap for the pagination loop

# Material-icon ligature tokens that leak into cell text and must be stripped.
ICON_TOKENS = {
    "favorite", "grade", "star", "trending_up", "trending_down",
    "trending_flat", "check", "search", "equalizer", "remove",
}


# ── Small parsing helpers ───────────────────────────────────────────────────

def strip_icons(text: str) -> str:
    """Remove Material-icon ligature words that render as icons in a cell."""
    tokens = [t for t in text.split() if t not in ICON_TOKENS]
    return " ".join(tokens).strip()


def parse_int(text: str):
    """First (optionally signed) integer in the cell, or None if absent."""
    m = re.search(r"-?\d+", text.replace(",", ""))
    return int(m.group()) if m else None


def load_posted() -> set:
    if POSTED_LOG.exists():
        try:
            return {k.lower().strip() for k in json.loads(POSTED_LOG.read_text())}
        except (json.JSONDecodeError, ValueError):
            print(f"[scraper] Warning: {POSTED_LOG.name} is unreadable; treating as empty.")
    return set()


def trend_score(change) -> float:
    if change is None:
        return 0.0          # unknown (e.g. "error"/"N/A") -> neutral
    if change < 0:
        return 3.0          # moved up / improved
    if change > 0:
        return -2.0         # moved down / declined
    return 1.0              # stable


def score_keyword(latest: int, change, local_vol: int) -> float:
    score = trend_score(change)
    score += min(local_vol / 100, 10)                 # local volume bonus
    score += (MAX_POSITION - latest) * 0.1            # tiebreak toward page 1
    return score


# ── Browser scrape ──────────────────────────────────────────────────────────

# JS: read every data row on the current page as an array of cell texts.
_JS_READ_ROWS = r"""
() => {
  const grid = document.querySelector('.custom-div-table');
  if (!grid) return [];
  const rows = grid.querySelectorAll('.cdt-row:not(.header)');
  return Array.from(rows).map(r =>
    Array.from(r.children).map(c => c.innerText.trim())
  );
}
"""

# JS: keyword text of the first data row (used to detect a real page turn).
_JS_FIRST_KEYWORD = r"""
() => {
  const row = document.querySelector('.custom-div-table .cdt-row:not(.header)');
  if (!row) return null;
  const cell = row.children[1];
  return cell ? cell.innerText.trim() : null;
}
"""

# JS: the visible "N - M of TOTAL" pagination indicator, or null.
_JS_RANGE = r"""
() => {
  for (const e of document.querySelectorAll('div,span,button')) {
    const t = e.textContent.trim();
    if (/^\d+\s*-\s*\d+\s*of\s*\d+$/.test(t) && e.offsetParent !== null) return t;
  }
  return null;
}
"""

# JS: click the chevron_right nearest the range indicator (the table's pager,
# not the graph's). Returns true if a button was clicked.
_JS_NEXT = r"""
() => {
  let rangeY = null;
  for (const e of document.querySelectorAll('div,span,button')) {
    if (/^\d+\s*-\s*\d+\s*of\s*\d+$/.test(e.textContent.trim()) && e.offsetParent !== null) {
      rangeY = e.getBoundingClientRect().y; break;
    }
  }
  if (rangeY === null) return false;
  let best = null, bestD = 1e9;
  for (const b of document.querySelectorAll('button')) {
    if (b.textContent.trim() === 'chevron_right' && b.offsetParent !== null) {
      const d = Math.abs(b.getBoundingClientRect().y - rangeY);
      if (d < bestD) { bestD = d; best = b; }
    }
  }
  if (best) { best.click(); return true; }
  return false;
}
"""


def _parse_range(text: str):
    """'51 - 100 of 410' -> (start, end, total)."""
    m = re.match(r"(\d+)\s*-\s*(\d+)\s*of\s*(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (None, None, None)


def _wait_settled(page, prev_first: str, timeout_s: float = 15.0) -> bool:
    """Wait until the grid shows a settled *new* page.

    During a page turn the grid briefly empties (first keyword -> null) before
    the new rows render, so we require the first keyword to be non-null, changed
    from ``prev_first``, and stable across two consecutive reads.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.4)
        first = page.evaluate(_JS_FIRST_KEYWORD)
        if first and first != prev_first:
            time.sleep(0.4)
            if page.evaluate(_JS_FIRST_KEYWORD) == first:  # stable
                return True
    return False


def scrape_all_rows(page) -> list[list[str]]:
    """Return raw cell-arrays for every keyword across all pages."""
    all_rows: list[list[str]] = []
    seen_keywords: set[str] = set()

    def collect() -> int:
        """Read the current grid and append not-yet-seen rows; return new count."""
        added = 0
        for cells in page.evaluate(_JS_READ_ROWS):
            if len(cells) <= COL_URL:
                continue
            kw = strip_icons(cells[COL_KEYWORD]).lower()
            if kw and kw not in seen_keywords:
                seen_keywords.add(kw)
                all_rows.append(cells)
                added += 1
        return added

    for page_no in range(1, MAX_PAGES + 1):
        rng = page.evaluate(_JS_RANGE)
        start, end, total = _parse_range(rng)
        print(f"[scraper] Page {page_no}: {rng or '(no range indicator)'}")

        # Defensive: if a settled page still yields nothing new, re-read a few
        # times before trusting it (guards against a slow final render).
        new = collect()
        for _ in range(4):
            if new > 0 or page_no == 1:
                break
            time.sleep(0.6)
            new = collect()
        print(f"[scraper]   +{new} new rows (total collected: {len(all_rows)})")

        # Stop once the range indicator says we've reached the last row.
        if end is not None and total is not None and end >= total:
            break

        prev_first = page.evaluate(_JS_FIRST_KEYWORD)
        if not page.evaluate(_JS_NEXT):
            print("[scraper]   No next-page control found; stopping.")
            break
        if not _wait_settled(page, prev_first):
            print("[scraper]   Page did not advance; stopping.")
            break

    return all_rows


def scrape_keywords() -> list[dict]:
    """Load the SerpReport view and return parsed keyword dicts."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print(f"[scraper] Loading SerpReport view: {SERP_VIEW_URL}")
        page.goto(SERP_VIEW_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_selector(".custom-div-table .cdt-row:not(.header)", timeout=30000)

        # SerpReport's shareable link (…/app/login?view_key=…) lands on the
        # project LIST, which uses the SAME .custom-div-table/.cdt-row classes but
        # with fewer columns. If this isn't the keyword grid (a real row has more
        # than COL_URL cells), open the project — re-appending the view_key so the
        # project view authenticates in this fresh browser session.
        ncols = page.evaluate(
            "() => { const r = document.querySelector("
            "'.custom-div-table .cdt-row:not(.header)'); return r ? r.children.length : 0; }")
        if ncols <= COL_URL:
            href = page.get_attribute("a[href*='/app/project/']", "href")
            m = re.search(r"/app/project/(\d+)", href or "")
            if m:
                vk = re.search(r"view_key=([0-9a-fA-F]+)", SERP_VIEW_URL)
                url = f"https://serpreports.com/app/project/{m.group(1)}"
                if vk:
                    url += f"?view_key={vk.group(1)}"
                print(f"[scraper] Shareable link is a project list; opening "
                      f"project {m.group(1)}...")
                page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_selector(
                    ".custom-div-table .cdt-row:not(.header)", timeout=30000)
            else:
                print("[scraper] WARNING: on a project list but found no project "
                      "link to open; parsing may fail.")
        time.sleep(2)

        raw_rows = scrape_all_rows(page)
        browser.close()

    keywords: list[dict] = []
    for cells in raw_rows:
        keyword = strip_icons(cells[COL_KEYWORD])
        latest = parse_int(cells[COL_LATEST])
        change = parse_int(cells[COL_CHANGE])
        local_vol = parse_int(cells[COL_LVOL]) or 0
        url_found = cells[COL_URL].strip() if len(cells) > COL_URL else ""

        if not keyword or latest is None:
            continue

        keywords.append({
            "keyword": keyword,
            "position": latest,
            "change": change,
            "local_vol": local_vol,
            "url_found": url_found,
        })

    return keywords


# ── Selection ───────────────────────────────────────────────────────────────

def pick_keyword(keywords: list[dict]) -> dict | None:
    posted = load_posted()
    candidates = []

    for kw in keywords:
        if not (MIN_POSITION <= kw["position"] <= MAX_POSITION):
            continue
        if kw["keyword"].lower().strip() in posted:
            continue
        if kw["local_vol"] < MIN_LOCAL_VOL:
            continue
        kw["score"] = round(score_keyword(kw["position"], kw["change"], kw["local_vol"]), 2)
        candidates.append(kw)

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n[scraper] {len(candidates)} eligible candidate(s) in positions "
          f"{MIN_POSITION}-{MAX_POSITION}. Top {min(5, len(candidates))}:")
    for c in candidates[:5]:
        print(f"  {c['keyword']:<38} pos={c['position']:>2}  "
              f"change={str(c['change']):>4}  vol={c['local_vol']:>5}  "
              f"score={c['score']:.2f}")

    return candidates[0]


# ── Entry point ─────────────────────────────────────────────────────────────

def main() -> int:
    if not SERP_VIEW_URL:
        print("[scraper] ERROR: SERPREPORT_VIEW_URL not set. Add it to .env "
              "(SERPREPORT_VIEW_URL=https://serpreports.com/app/project/<id>"
              "?view_key=<token>).")
        return 1
    keywords = scrape_keywords()
    if not keywords:
        print("[scraper] ERROR: No keyword rows parsed. Page layout may have changed.")
        return 1

    print(f"\n[scraper] Parsed {len(keywords)} total keywords.")
    best = pick_keyword(keywords)

    if not best:
        print("[scraper] No eligible keyword this run "
              f"(none in positions {MIN_POSITION}-{MAX_POSITION} after exclusions).")
        return 2

    output = {
        "keyword": best["keyword"],
        "position": best["position"],
        "change": best["change"],
        "local_vol": best["local_vol"],
        "url_found": best["url_found"],
        "score": best["score"],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    KEYWORD_OUT.write_text(json.dumps(output, indent=2))
    print(f"\n[scraper] Chosen keyword: '{best['keyword']}' "
          f"(pos {best['position']}, vol {best['local_vol']}, score {best['score']:.2f})")
    print(f"[scraper] Written to {KEYWORD_OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
