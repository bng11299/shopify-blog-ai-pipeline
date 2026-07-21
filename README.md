# Shopify Blog AI Pipeline

An end-to-end pipeline that turns a target SEO keyword into a **review-ready draft**
Shopify blog article — AI-written copy (Claude), AI-generated and QA'd images
(Gemini), a deterministic SEO + AEO quality gate, and a human-in-the-loop
publish via the Shopify GraphQL Admin API.

Built for a real Singapore e-commerce store. Ported and re-architected from a
prior WordPress content pipeline. Emphasis throughout is on **safety, structured
output, and evidence-based quality gates** rather than "prompt and hope".

> **Draft-only by design.** The pipeline never publishes live. It creates the
> article as an unpublished draft; a human reviews it and clicks publish in the
> Shopify admin. This is a hard safety property, not a setting.

---

## Pipeline

```
scraper.py      pick a keyword from SerpReport (position 8–12 band)     → keyword.json
generator.py    write the article (Claude, schema-guaranteed)           → post.json
editor.py       Layer 1 deterministic checks + Layer 2 editorial review → post.json
seo_check.py    SEO + AEO scorecard (hard-minimum gate)                 → seo_report.json
imagegen.py     Gemini images + self-correcting artifact-QA loop        → images/
final_check.py  pre-publish validation + publish plan; --mark-posted
publisher.py    GraphQL Admin API: upload images + create DRAFT + SEO metafields
```

`run.py` sequences the local half; publishing is always a separate, deliberate
step. Data flows through plain JSON files (`keyword.json` → `post.json` →
`images/manifest.json`), so every stage is independently runnable and inspectable.

## Highlights

- **Structured output by construction.** The article JSON is defined by a single
  schema. In API mode Claude is forced to emit it via a strict `tool_use` call;
  in Claude Code mode the same schema drives a prep/validate round-trip. No
  parsing of free-form model text.
- **Two-layer editorial review.** Deterministic Python checks (word count, link
  counts, alt text, banned AI phrases, British/Singapore spelling, heading
  hierarchy) plus a subjective editorial pass judging tone, FAQ usefulness and
  link placement — with an auto-regenerate-on-fail loop.
- **Deterministic SEO + AEO scorecard.** `seo_check.py` scores on-page **SEO**
  (keyword placement, meta lengths, density, links, structure) and **AEO**
  (Answer Engine Optimisation — valid FAQPage JSON-LD, question-style headings,
  snippet-sized self-contained answers, extractable tables/lists). It is a hard
  gate: **SEO ≥ 97, AEO ≥ 93** (one soft warn tolerated per category; any hard
  failure blocks). These floors were **calibrated empirically** on a
  multi-keyword batch of genuinely-independent generations, not guessed.
- **Self-correcting image generation.** Every Gemini image passes a vision-model
  "photo editor" QA gate that combs for AI artifacts (garbled text, bad hands,
  warped geometry); rejects trigger regeneration, then automatic prompt revision.
- **Answer-engine-first content.** Articles are written to be quotable by AI
  answer engines: a definitional opener, question-style FAQ with matching
  `FAQPage` structured data, and concise snippet-sized answers.
- **Security built in.** Secrets only in a gitignored `.env`; site-specific
  values in a gitignored `config_local.py`; the tracked `config.py` holds generic
  placeholders. Guard/audit Git hooks block committing secrets. Least-privilege
  Shopify scopes; pinned + schema-verified Admin API version.

## Generation modes (auto-selected)

The generator and editor pick a mode based on whether `ANTHROPIC_API_KEY` is set:

- **Claude Code mode (default, no API key):** `generator.py --prep` writes a
  self-contained request (system prompt + schema + real internal-link
  allow-list); the article JSON is produced against it and validated back with
  `generator.py --ingest`. `editor.py` runs the deterministic checks and hands
  the subjective review off explicitly.
- **API mode (`ANTHROPIC_API_KEY` set):** forced `tool_use`, the full two-layer
  review with auto-regeneration, and `run.py` autopilots the local half.

Both paths share the **same** article schema, so output is identical in shape.

## Quality gate

| Layer | What it enforces |
|-------|------------------|
| `editor.py` L1 | word count, ≥4 internal + ≥1 authority link, alt text, no stray `<h1>`, banned phrases, British/Singapore spelling, keyword placement |
| `editor.py` L2 | tone, heading hierarchy, FAQ usefulness, contextual link placement, answer-engine readiness |
| `seo_check.py` | **SEO ≥ 97** and **AEO ≥ 93** hard gate; keyword density 0.5–2.5%; valid FAQPage schema; snippet-sized answers |

## Security & safety

- **Draft-only publishing** with a human reviewer — the pipeline sets
  `isPublished: false` and never goes live on its own.
- **Secrets** (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `SERPREPORT_VIEW_URL`,
  `SHOPIFY_STORE`, `SHOPIFY_ADMIN_TOKEN`) live only in a gitignored `.env`.
- **Client/site config** in a gitignored `config_local.py`; the repo ships only
  generic placeholders.
- **Guard hook** (`.claude/hooks/guard.py`) blocks editing `.env` and
  staging/committing secret files; an audit hook logs tool calls with tokens
  redacted.
- **Least privilege**: the Shopify app needs only `write_content`,
  `read_content`, `write_files`, `read_files`.
- **Threat model** and design records in [`docs/`](docs/).

## Setup

Requires Python 3.13 (Windows / PowerShell friendly; no Node needed).

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium        # scraper browser
Copy-Item env.example .env                    # then fill in real secrets
# create config_local.py from config.py's placeholders and set your store/brand
```

## Usage

**Claude Code mode (no API key):**
```powershell
python run.py                  # scraper → generator --prep, then stops
# ...produce the article JSON, then:
python generator.py --ingest   # validate → post.json
python editor.py               # deterministic checks + editorial review
python run.py --finish         # seo_check → imagegen → final_check
python publisher.py --check    # verify Shopify token + resolve blog
python publisher.py --publish  # upload images + create the DRAFT
# ...review & publish the draft in the Shopify admin, then:
python final_check.py --mark-posted
```

**API mode (`ANTHROPIC_API_KEY` set):** `python run.py` runs the whole local
half unattended, then publish as above.

Refresh the internal-link catalogue occasionally with
`python build_internal_links.py`.

## Testing

```powershell
python -m pytest
```

An offline suite (`tests/test_pipeline.py`) covers the pure logic and negative
tests at the trust boundaries — `.env` precedence, publish-gate blockers,
draft-only enforcement, the SEO/AEO scorer (natural vs stuffed density, valid vs
invalid FAQ schema). SDK-dependent tests `importorskip` so the suite runs on a
fresh checkout.

## Project structure

```
scraper.py               SerpReport keyword picker
generator.py             Claude article generation (schema-driven, two modes)
editor.py                two-layer editorial review + regenerate loop
seo_check.py             deterministic SEO + AEO scorecard / hard gate
imagegen.py              Gemini image generation + artifact-QA loop
final_check.py           pre-publish validation + publish plan
publisher.py             Shopify GraphQL Admin API (draft articles, images, SEO)
shopify_oauth.py         one-time OAuth bootstrap to mint an Admin API token
build_internal_links.py  sitemap → internal-link catalogue
common.py                shared helpers (.env loader, slugify, meta trimming)
config.py                generic placeholder config (real values in config_local.py)
run.py                   local-half sequencer
docs/                    THREAT_MODEL.md, PUBLISHING.md, adr/
tests/                   pytest suite
.claude/                 guard + audit hooks, settings
```

## Documentation

- [`docs/PUBLISHING.md`](docs/PUBLISHING.md) — Shopify app setup, API-version
  pinning, the publish flow (incl. the Dev-Dashboard OAuth path)
- [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) — assets, trust boundaries, top threats
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`CLAUDE.md`](CLAUDE.md) — working rules and non-negotiables for the repo

## Tech stack

Python 3.13 · [Anthropic Claude](https://www.anthropic.com) (Messages API,
`tool_use` structured output) · Google Gemini (image generation + vision QA) ·
Playwright (scraping) · Shopify GraphQL Admin API · pytest.
