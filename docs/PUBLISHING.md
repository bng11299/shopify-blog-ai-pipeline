# Publishing runbook — Shopify

The publish half of the pipeline. `publisher.py` drives the **GraphQL Admin API**;
this doc is the human-readable companion.

## 0. One-time: create the custom app + token
Shopify admin → **Settings → Apps and sales channels → Develop apps** → create an
app → **Configure Admin API scopes** → grant least privilege:

- `write_content`, `read_content` — blogs / articles / pages
- `write_files`, `read_files` — image uploads (Files API)

Install the app → copy the **Admin API access token** (`shpat_…`). Put it and the
store domain in `.env` (never commit):

```
SHOPIFY_STORE=your-store.myshopify.com
SHOPIFY_ADMIN_TOKEN=shpat_...
```

Prefer a **dev / staging store** for the first end-to-end run.

### If you only have a Partner/dev-dashboard app (client id + secret, no token)
A Partner Dashboard app exposes a **client id + client secret** ("app keys"), not
an Admin API token — the token is minted when the app is installed via OAuth.
`publisher.py` always authenticates with the **token**, never the app keys, so
mint one once:

Preferred — create a custom app *inside the store admin* (**Settings → Apps →
Develop apps**) instead; that shows the token directly and skips OAuth.

Otherwise, bootstrap the token from the app keys with `shopify_oauth.py`:
1. In the Partner app config (**App setup → URLs**) add an Allowed redirection
   URL matching `SHOPIFY_OAUTH_REDIRECT` (default `https://localhost/callback`),
   and ensure the app's scopes include the four above.
2. Put `SHOPIFY_STORE`, `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` in `.env`.
3. `python shopify_oauth.py` → open the printed URL, approve, copy the redirected
   URL from the address bar (it won't load — that's fine).
4. `python shopify_oauth.py --exchange "<pasted URL>"` → prints the offline
   Admin API token. Paste it into `SHOPIFY_ADMIN_TOKEN`; you can then clear the
   client id/secret.

## 1. Pin + verify the API version
`config.SHOPIFY["api_version"]` (default `2025-01`) is pinned into the endpoint:
`/admin/api/2025-01/graphql.json`. Shopify deprecates quarterly. **Before the
first real publish, verify every field/mutation name** used in `publisher.py`
against that version's schema at
`https://shopify.dev/docs/api/admin-graphql/2025-01/`. Names most likely to drift:

- `ArticleCreateInput` — `body` vs `bodyHtml`; the `author` shape; whether it
  accepts an `seo` input (if so, prefer it over the `global/*_tag` metafields).
- `articleCreate` availability (present from API 2024-10+; confirm for your version).
- `fileCreate` / `MediaImage` — the status field name (`fileStatus`) and how the
  CDN `image.url` becomes available (async processing → poll until `READY`).

The GraphQL Admin API is **cost-based rate-limited**; `publisher.py` backs off on
`THROTTLED`. Batch sensibly if you extend it.

## 2. Prove one draft round-trips (do this before trusting the rest)
```
python publisher.py --check     # shop query + resolve the blog GID from blog_handle
python publisher.py             # DRY RUN: prints the planned articleCreate input; no writes
python publisher.py --publish   # uploads images, creates the DRAFT, sets SEO metafields
```

## 3. What `--publish` does (in order)
1. Resolve the target blog GID from `config.SHOPIFY["blog_handle"]`.
2. For each image in `images/manifest.json`: `stagedUploadsCreate` → POST the
   bytes to the staged target → `fileCreate` → poll until `READY` → CDN URL.
3. Swap `{{HERO_IMAGE}}` / `{{INBODY_IMAGE}}` in `body_html` for the real URLs;
   set the hero as the article `image`.
4. `articleCreate` with `isPublished: false` — a **DRAFT**.
5. `metafieldsSet` for `global/title_tag` and `global/description_tag` (SEO).

## 4. Human-in-the-loop (the safety property)
Nothing is live yet. Open the draft in Shopify admin → **Blog posts** → review
copy, images, links, and the SEO preview → **publish there**. Only after the
draft exists:

```
python final_check.py --mark-posted   # records the keyword so the scraper skips it
```

## Theme / schema notes
- Check the theme first: most Shopify themes already emit `BlogPosting` (Article)
  JSON-LD. `config.SHOPIFY["theme_emits_article_schema"]` (default `True`) tells
  the generator NOT to add a duplicate. FAQ (`FAQPage`) JSON-LD is separate and
  is always emitted inline.
- Every `<img>` keeps an alt attribute; the hero alt contains the focus keyword.

## Handle collisions
If `/<handle>` already exists on the blog, Shopify rejects or suffixes it. Rename
in `post.json` (or the admin) and re-run.
