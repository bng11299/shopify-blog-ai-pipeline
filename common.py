"""
common.py
---------
Shared, dependency-light helpers used across the pipeline. Deliberately imports
NO AI SDK — only the standard library and config — so any stage (imagegen,
final_check, editor) can reuse these without pulling in the generation stack.

Provides: the .env loader (run once at import), the image placeholder tokens,
meta-length limits and trimming, slugify, block spacing, and the internal-link
catalogue (load_links / select_internal_links).
"""

import json
import os
import re
from pathlib import Path

import config as cfg

HERE = Path(__file__).parent
KEYWORD_IN = HERE / "keyword.json"
POST_OUT = HERE / "post.json"
LINKS_FILE = HERE / "internal_links.json"
ENV_FILE = HERE / ".env"

# Placeholder tokens the generator emits and the publisher swaps for real URLs.
HERO_IMG = "{{HERO_IMAGE}}"
INBODY_IMG = "{{INBODY_IMAGE}}"

# Shopify SEO metafield lengths (soft targets — verify against your theme).
META_TITLE_MAX = 60
META_DESC_MAX = 160


# ── .env loader ───────────────────────────────────────────────────────────────

def load_dotenv() -> None:
    """Load KEY=VALUE lines from a local .env into the environment.

    Values already set in the shell win; values are never logged. Keeps API
    keys out of code and command lines without a python-dotenv dependency."""
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


load_dotenv()  # at import, so every importer gets .env for free


# ── Internal-link catalogue ─────────────────────────────────────────────────

# Fallback internal links if internal_links.json isn't present yet.
FALLBACK_LINKS = [
    {"url": p, "title": t, "description": ""}
    for p, t in cfg.SERVICE_PAGES.items()
]
if cfg.CONTACT_PATH not in cfg.SERVICE_PAGES:
    FALLBACK_LINKS.append({"url": cfg.CONTACT_PATH, "title": "Contact", "description": ""})


def load_links() -> list[dict]:
    if LINKS_FILE.exists():
        try:
            data = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
            if data:
                return data
        except (json.JSONDecodeError, ValueError):
            print(f"[common] WARNING: {LINKS_FILE.name} unreadable; using fallback links.")
    else:
        print(f"[common] NOTE: {LINKS_FILE.name} not found; using fallback links. "
              f"Run build_internal_links.py for full coverage.")
    return FALLBACK_LINKS


def select_internal_links(keyword: str, url_found: str, links: list[dict],
                          n: int = 26) -> list[dict]:
    """Pick the internal links most relevant to the keyword, plus core hubs and
    the keyword's own existing page (so the model links real, on-topic URLs)."""
    kw_tokens = set(re.findall(r"[a-z0-9]+", keyword.lower()))
    scored = []
    for lnk in links:
        hay = f"{lnk['url']} {lnk.get('title', '')} {lnk.get('description', '')}".lower()
        toks = set(re.findall(r"[a-z0-9]+", hay))
        scored.append((len(kw_tokens & toks), lnk))
    scored.sort(key=lambda x: -x[0])

    chosen, seen = [], set()

    def add(lnk):
        if lnk and lnk["url"] not in seen:
            chosen.append(lnk)
            seen.add(lnk["url"])

    if url_found and url_found.upper() != "NOT FOUND":
        add(next((l for l in links if l["url"] == url_found), None))
    for _, lnk in scored:
        if len(chosen) >= n:
            break
        add(lnk)
    for hub in cfg.CORE_HUBS:  # make sure the hubs are always on offer
        add(next((l for l in links if l["url"] == hub), {"url": hub, "description": ""}))
    return chosen


def format_links(links: list[dict]) -> str:
    lines = []
    for lnk in links:
        desc = (lnk.get("description") or lnk.get("title") or "").strip()
        lines.append(f"   {lnk['url']}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


# ── Meta-string tidying ───────────────────────────────────────────────────────

# Trailing words that make a meta read like it was cut off mid-thought.
_TRAILING_STOPWORDS = {
    "and", "or", "but", "so", "to", "for", "with", "of", "the", "a", "an",
    "in", "on", "at", "by", "from", "as", "that", "which", "your", "our",
    "is", "are", "will", "can", "how", "what", "why",
}


def tidy_meta(text: str) -> str:
    """Strip trailing punctuation and dangling stop-words so a meta doesn't end
    mid-clause (e.g. '...ship worldwide, and' -> '...ship worldwide')."""
    text = text.strip()
    prev = None
    while prev != text:
        prev = text
        text = text.rstrip(" ,.;:!?–—-")
        words = text.split()
        if words and words[-1].lower() in _TRAILING_STOPWORDS:
            text = " ".join(words[:-1])
    return text.strip()


def trim_meta(text: str, limit: int) -> str:
    """Fit a meta to <= limit chars without ending mid-phrase.

    Prefer the longest clean cut at a sentence/clause terminator (>= 100 chars to
    keep it substantial); else the last full sentence (>= 60 chars); else a word
    boundary. Always tidy dangling stop-words. Also tidies already-short metas."""
    text = text.strip()
    if len(text) <= limit:
        return tidy_meta(text)
    cut = text[:limit]
    clean = [m.end() for m in re.finditer(r"[.!?,;:]", cut)]
    long_clean = [p for p in clean if p >= 100]
    if long_clean:
        return tidy_meta(cut[: max(long_clean)])
    sentences = [m.end() for m in re.finditer(r"[.!?]", cut)]
    if sentences and max(sentences) >= 60:
        return tidy_meta(cut[: max(sentences)])
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return tidy_meta(cut)


def slugify(kw: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", kw.lower())).strip("-")


# ── HTML block spacing ────────────────────────────────────────────────────────

_BLOCK_CLOSE_RE = re.compile(
    r"(</(?:p|h1|h2|h3|h4|ul|ol|table|blockquote|figure|script)>)[ \t]*\n?", re.I)
_IMG_RE = re.compile(r"(<img\b[^>]*?>)[ \t]*\n?", re.I)


def space_blocks(html: str) -> str:
    """Insert a blank line between block-level elements for clean raw HTML."""
    html = _BLOCK_CLOSE_RE.sub(r"\1\n\n", html)
    html = _IMG_RE.sub(r"\1\n\n", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()
