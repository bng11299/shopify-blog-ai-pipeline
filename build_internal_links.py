"""
build_internal_links.py
------------------------
Reads the configured store's sitemap (config SITEMAP_INDEX) and builds
internal_links.json — a catalogue of every linkable page/product/collection/
article with a short, human-readable description derived from its URL slug.
generator.py reads this file to choose real, relevant internal links (no
hallucinated URLs).

Shopify's /sitemap.xml is a sitemap index that references child sitemaps:
  sitemap_products_1.xml, sitemap_collections_1.xml, sitemap_pages_1.xml,
  sitemap_blogs_1.xml
Unlike the WordPress pipeline (which linked services only), a store WANTS to
link to products and collections, so those are included here.

The <loc> URLs are static and fetch fine without a browser. Slugs are
descriptive enough to derive a title; if you ever need richer titles, pull them
from the Admin API instead (see docs/PUBLISHING.md).

Run occasionally (e.g. monthly, or whenever the catalogue changes):
  python build_internal_links.py

Output: internal_links.json  ->  [{"url": "/slug/", "title": "...", "description": "..."}]

Standard library only, plus config.py for the sitemap URL.
"""

import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import config as cfg

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

SITEMAP_INDEX = cfg.SITEMAP_INDEX
OUT = Path(__file__).parent / "internal_links.json"

# Which Shopify child sitemaps to harvest. Products & collections are good link
# targets for a store; pages and blog articles add supporting/topical links.
INCLUDE_SITEMAP_RE = re.compile(
    r"sitemap_(products|collections|pages|blogs)_\d+\.xml$", re.I)

# Exclude cart/checkout/account/policy/utility paths — never useful link targets.
EXCLUDE_PATH_RE = re.compile(
    r"/(cart|checkout|account|orders?|search|policies|"
    r"privacy-policy|terms-of-service|refund-policy|shipping-policy)(/|$)", re.I)
EXCLUDE_EXACT = {"/", "/collections", "/pages", "/blogs"}

# Auto-generated / duplicate slugs with no topical meaning (pure numeric IDs).
JUNK_SLUG_RE = re.compile(r"^\d+(-\d+)?$")

TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0 (shopify-blog-pipeline internal-link builder)"}

# Expand common abbreviations so slug-derived text reads naturally.
ABBREV = {
    "it": "IT", "diy": "DIY", "usb": "USB", "faq": "FAQ", "ai": "AI",
    "sg": "Singapore", "hd": "HD", "led": "LED", "pc": "PC", "tv": "TV",
}


def _get(url: str) -> str:
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def _locs(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)


def collect_urls() -> list[str]:
    index = _get(SITEMAP_INDEX)
    child_sitemaps = [u for u in _locs(index)
                      if INCLUDE_SITEMAP_RE.search(u.rsplit("/", 1)[-1])]
    if not child_sitemaps:
        # Some small stores have a flat /sitemap.xml with the URLs directly.
        child_sitemaps = [SITEMAP_INDEX]
    urls: list[str] = []
    for sm in child_sitemaps:
        try:
            urls += _locs(_get(sm))
        except Exception as e:  # noqa: BLE001
            print(f"[links]   WARN: could not fetch {sm}: {e}")
    # Dedupe, keep only this store's URLs, drop excluded/junk paths.
    seen, keep = set(), []
    for u in urls:
        path = urlparse(u).path or "/"
        if cfg.SITE["site"] not in u:
            continue
        if path in EXCLUDE_EXACT or EXCLUDE_PATH_RE.search(path):
            continue
        last = path.strip("/").split("/")[-1]
        if JUNK_SLUG_RE.match(last):
            continue
        if path in seen:
            continue
        seen.add(path)
        keep.append(path)
    return keep


def describe(path: str) -> dict:
    """Turn a slug into a readable title + short description."""
    slug = path.strip("/").split("/")[-1]
    words = [ABBREV.get(w, w) for w in slug.split("-") if w]
    if not words:
        return {"url": path, "title": path, "description": path}
    titled = [w if (w.isupper() or w[0].isdigit()) else w.capitalize()
              for w in words]
    title = " ".join(titled)
    return {"url": path, "title": title, "description": title}


def _paths_from_existing() -> list[str]:
    """Fall back to URLs already captured in internal_links.json (e.g. when the
    store's sitemap fetch is throttling)."""
    if not OUT.exists():
        return []
    data = json.loads(OUT.read_text(encoding="utf-8"))
    keep = []
    for rec in data:
        path = rec.get("url", "")
        if not path or path in EXCLUDE_EXACT or EXCLUDE_PATH_RE.search(path):
            continue
        last = path.strip("/").split("/")[-1]
        if JUNK_SLUG_RE.match(last):
            continue
        keep.append(path)
    return keep


def main() -> int:
    print(f"[links] Reading sitemap: {SITEMAP_INDEX}")
    try:
        paths = collect_urls()
    except Exception as e:  # noqa: BLE001
        print(f"[links] Sitemap fetch failed ({e}).")
        paths = _paths_from_existing()
        if paths:
            print(f"[links] Falling back to {len(paths)} URLs already in "
                  f"{OUT.name} (re-deriving descriptions).")
        else:
            print("[links] No existing URLs to fall back to. Aborting.")
            return 1
    results = [describe(p) for p in paths]
    results.sort(key=lambda r: r["url"])
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"[links] Wrote {len(results)} linkable entries to {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
