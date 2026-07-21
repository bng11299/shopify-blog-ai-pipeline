"""
config.py — site profile for the Shopify blog pipeline.

This tracked file holds GENERIC PLACEHOLDER values and documents the shape of
every site-specific setting the pipeline needs. Your real values live in a
gitignored `config_local.py` (copy the block below, fill it in) which overrides
everything here at import time. Nothing client-specific belongs in this file.

To target a store: create `config_local.py` (gitignored) defining any of the
names below (SITE, SERVICE_PAGES, CORE_HUBS, CONTACT_PATH, ACRONYMS,
SITEMAP_INDEX, SHOPIFY) with real values; they override these placeholders.

Secrets (Anthropic/Gemini keys, SerpReport URL, Shopify store + admin token)
live in .env, never here — see env.example.

Scripts read settings via:  import config as cfg  ->  cfg.SITE["company"], ...
"""

# ── Brand / voice ────────────────────────────────────────────────────────────
# Everything the generator needs to write in-voice for one company/store.
SITE = {
    "company": "Example Company Pte Ltd",     # full legal name
    "short_name": "Example Co",               # how the copy refers to itself
    "author": "",                             # article byline; falls back to short_name
    "site": "example.com",                    # bare domain (no scheme); storefront
    "audience": "business decision-makers",   # who the copy addresses
    "region": "your market",                  # geography name used in headings
    "spelling": "consistent English spelling",
    # Contact details for the closing CTA block (built in Python, not by the LLM,
    # so they are always correct). Empty values are omitted from the CTA.
    "phone": "",                              # e.g. "+00 0000 0000"
    "whatsapp": "",                           # e.g. "+00 0000 0000" (often == phone)
    "email": "",                              # e.g. "enquiries@example.com"
    "address": "",                            # e.g. "1 Example St, #01-01, City 000000"
    "established": "",                        # e.g. "1993"
    # Positioning sentence dropped into the system prompt after the company name.
    "descriptor": ("a company that sells and supports its products online. "
                   "Content educates buyers and drives enquiries."),
    # Authoritative outbound sources the article should cite (prose, with URLs).
    "authority_desc": ("a recognised industry or regulatory authority "
                       "(link to its official site)"),
    # Locale/context phrase appended to image-generation prompts.
    "image_context": "professional product/lifestyle setting",
    # editor.py deterministic checks: body MUST mention every 'required' term
    # (blocker) and SHOULD mention an 'optional' one (warning). Empty = no check.
    "compliance_terms": {"required": [], "optional": []},
}

# ── Internal linking ──────────────────────────────────────────────────────────
# Curated pages (path -> anchor label). The internal-link allow-list draws from
# these plus the sitemap catalogue. Use storefront-relative paths.
SERVICE_PAGES = {
    "/collections/all": "Shop All",
    "/pages/about-us": "About Us",
    "/pages/contact": "Contact",
}
# Hubs always offered as linking options.
CORE_HUBS = {"/collections/all", "/pages/contact"}
# Primary contact/enquiry path (offered as a link; Shopify has no forced CTA block).
CONTACT_PATH = "/pages/contact"

# Uppercased when title-casing labels/eyebrows (e.g. "IT", "FAQ").
ACRONYMS = {"it", "seo", "faq", "sme", "ceo", "cto", "diy", "usb"}

# ── Data sources ──────────────────────────────────────────────────────────────
# Shopify exposes /sitemap.xml (+ sitemap_products_1.xml, sitemap_pages_1.xml,
# sitemap_blogs_1.xml). build_internal_links.py reads this index.
SITEMAP_INDEX = "https://example.com/sitemap.xml"   # build_internal_links.py
# SerpReport view URL and API keys are secrets — set them in .env, not here.

# ── Shopify publishing target ─────────────────────────────────────────────────
# Store domain + admin token are SECRETS (env: SHOPIFY_STORE, SHOPIFY_ADMIN_TOKEN).
# The values here are non-secret publishing knobs.
SHOPIFY = {
    # Pin the Admin API version in the endpoint. Shopify deprecates quarterly;
    # verify field/mutation names against THIS version's schema before trusting
    # them — don't rely on field names from memory. See docs/PUBLISHING.md.
    "api_version": "2025-01",
    # Target blog by handle; publisher.py resolves this to the blog GID at run
    # time (a store may have several blogs, e.g. "news").
    "blog_handle": "news",
    # Many Shopify themes already emit Article (BlogPosting) JSON-LD. If yours
    # does, leave this True so the generator/publisher do NOT add a duplicate
    # Article schema block. FAQ schema is separate and still emitted.
    "theme_emits_article_schema": True,
}


# ── Local override ────────────────────────────────────────────────────────────
# Real values live in config_local.py (gitignored). If present, it overrides the
# placeholders above. Missing names simply fall back to the defaults here.
try:
    from config_local import *  # noqa: F401,F403
except ModuleNotFoundError:
    import sys
    print("[config] NOTE: config_local.py not found — using placeholder values "
          "from config.py. Create config_local.py (copy the assignments from "
          "config.py) and fill in your store.", file=sys.stderr)
