# Shopify Blog Pipeline — kickoff brief

Port of the WordPress "Page Maker" pipeline to **Shopify blog articles**. Open
this in the new session (or paste it as the first message) to carry context.

## Goal
Same shape as the WordPress pipeline: SerpReport keyword → AI-written blog
article → quality gate → AI images → publish a **draft** article to Shopify for
human review. Differences: **Claude** writes the copy (not Gemini), output is
**plain semantic HTML** (not a WPBakery tree), and publishing is via a **custom
Shopify app + Admin API** (not Novamira/WordPress).

## Reuse from the WordPress repo (github.com/bng11299/WordPress-Page-Maker)
Copy and adapt — the architecture and discipline transfer directly:
- `scraper.py` — SerpReport keyword picker. **Unchanged.**
- `config.py` + `config_local.py` — the generic/placeholder + gitignored-real
  override pattern. Keep it.
- `common.py` — dotenv loader, meta trimming, slugify, block spacing, link
  catalogue. Mostly reusable.
- `imagegen.py` — Gemini image gen + self-correcting artifact-QA loop. Reuse
  almost as-is; only the upload target changes (Shopify Files API, below).
- `final_check.py` gate + `posted_keywords.json` dedup + the pytest/ADR/README
  discipline + secrets-in-`.env` + human-in-the-loop (create as **draft**).

## What changes

### 1. Text generation: Gemini → Claude
- Use the **Anthropic Messages API** (`anthropic` SDK). Load the **claude-api
  skill** first for current model IDs, params, and structured-output guidance.
- Claude handles forced schemas well — use **tool_use** (a tool with an
  `input_schema`) to guarantee the article JSON shape, instead of hoping for
  clean JSON. (This sidesteps the response_schema loop that plagued Gemini.)
- Suggested output fields: `title`, `body_html` (semantic HTML), `summary_html`
  (excerpt), `tags` (list), `handle` (slug), `seo_title`, `seo_description`,
  `image_prompts` (hero + in-body).

### 2. Output format: plain HTML (much simpler than WPBakery)
- Shopify article body is just HTML — no shortcodes, no base64 raw-html, no
  vc_row tree. The generator's existing clean-HTML output maps almost directly.
- Comparison table + FAQ go **inline** as HTML; add FAQ structured data as a
  `<script type="application/ld+json">` FAQPage block. **Check the theme first**
  — many Shopify themes already emit Article schema; don't duplicate/conflict.
- Every `<img>` keeps an alt attribute.

### 3. SEO: no RankMath
- Shopify SEO = the article's **SEO title/description**, stored as metafields
  `namespace: global`, keys `title_tag` / `description_tag` (verify whether the
  current `articleCreate` input also takes an `seo` field). No 0–100 score.
- Adapt `editor.py`'s deterministic checks to Shopify-appropriate heuristics:
  title ≤ ~60 chars, description ≤ ~160, one H1, focus keyword in the first
  paragraph + a couple of headings, ≥N internal links, alt text present,
  reasonable length. Drop the "RankMath 85+" target and the WordPress/PDPA
  footer-leak checks that don't apply.

### 4. Publishing: custom Shopify app + Admin API
- **Create a custom app:** Shopify admin → Settings → Apps and sales channels →
  Develop apps → create app → configure **Admin API scopes** → install → copy
  the **Admin API access token** (`shpat_…`). (The Partner dev dashboard also
  works for a dev store.)
- **Scopes:** `write_content` (blogs/articles/pages) + `read_content`, and
  `write_files`/`read_files` for image uploads. Grant least privilege.
- **Secrets in `.env`** (gitignored, same as before):
  `SHOPIFY_STORE=xxx.myshopify.com`, `SHOPIFY_ADMIN_TOKEN=shpat_…`. Never commit.
- **Use the GraphQL Admin API**, not REST — Shopify's REST article/blog
  endpoints are legacy. Mutations: `articleCreate` / `articleUpdate` (confirm
  they're in your pinned API version, e.g. `2025-01`; the model is
  Store → Blog(s) → Article, so you need the target **blog GID**).
- **Draft = human-in-the-loop:** create the article **unpublished**
  (`isPublished: false` / no `publishedAt`) so a person reviews and publishes in
  the Shopify admin. Preserve this safety property.
- **Images:** generate with `imagegen.py`, then upload via the Files API
  (`stagedUploadsCreate` → PUT the bytes → `fileCreate`), and set the hero as the
  article's `image`. (Or attach via base64 on the article image field — verify
  what the API version supports.)

### 5. Internal-link catalogue
- Shopify exposes `/sitemap.xml` (+ `sitemap_products_1.xml`, `sitemap_pages`,
  `sitemap_blogs`). Reuse `build_internal_links.py` against it, **or** pull
  products/collections/pages/articles via the Admin API for richer titles.

## First steps in the new session
1. Scaffold the new repo; copy `scraper.py`, `config.py`/`config_local.py`,
   `common.py`, `imagegen.py`; strip WordPress specifics.
2. Create the custom app; put token + store in `.env`; prove one **draft**
   `articleCreate` round-trips before building anything else.
3. Swap the generator to Claude (Messages API + tool schema) emitting the
   Shopify article JSON above.
4. Build a small `publisher.py`: `articleCreate` (draft) → upload image → set
   SEO metafields. Add a `final_check.py`-style gate + `--mark-posted`.
5. Adapt `editor.py` checks to Shopify SEO; keep the two-layer (code + LLM)
   review and the regenerate-on-fail loop.

## Gotchas
- **Pin the API version** in the endpoint (`/admin/api/2025-01/graphql.json`);
  Shopify deprecates quarterly. Verify exact field/mutation names against that
  version's schema — don't trust field names from memory (incl. this brief).
- GraphQL Admin API is **cost-based rate-limited** — batch sensibly.
- The Admin token is powerful (store-wide) — same hygiene as the WP work:
  `.env`, gitignore, least scope; consider a dev/staging store first.
- Two Gemini uses remain (image gen); text is Anthropic. Keep both keys in
  `.env`: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, plus `SERPREPORT_VIEW_URL`.
