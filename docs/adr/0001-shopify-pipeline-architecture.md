# ADR 0001 — Shopify blog pipeline architecture

Status: Accepted · Date: 2026-07-16

## Context
We are porting the WordPress "Page Maker" pipeline to publish **Shopify blog
articles**. The WordPress version's architecture (staged scripts, JSON hand-off
files, config/config_local split, secrets in `.env`, a two-layer quality gate,
draft + human-in-the-loop publish) worked well and transfers directly. Three
things must change for Shopify: the copywriter, the output format, and the
publish mechanism.

## Options considered
- **Copy model:** keep Gemini vs. switch to Claude. The WordPress generator
  fought Gemini's `response_schema` (token-repetition death-loop) and settled on
  plain-JSON mode. Claude's forced `tool_use` with a strict `input_schema`
  guarantees the article shape at the API layer.
- **Output format:** WPBakery element tree (as before) vs. plain semantic HTML.
  Shopify article bodies are just HTML.
- **Publish transport:** REST Admin API vs. GraphQL Admin API. Shopify's REST
  article/blog endpoints are legacy.
- **Structured output:** forced `tool_use` vs. `output_config.format` (JSON
  schema). Both guarantee shape and both work with Claude.

## Decision
- **Claude** (Anthropic Messages API, `claude-opus-4-8`) writes the copy, via a
  **forced `tool_use`** call (`emit_article` / `emit_review`) with `strict: true`.
  Chosen over `output_config.format` to match the kickoff brief and because a
  named tool reads naturally for "emit the article" / "emit the review"; the
  guarantee is equivalent.
- **Plain semantic HTML** output. FAQ ships inline plus a `FAQPage` JSON-LD
  block; BlogPosting/Article schema is left to the theme (config-gated) to avoid
  duplicate/conflicting schema.
- **GraphQL Admin API**, version pinned in `config.SHOPIFY["api_version"]`.
  `articleCreate` with `isPublished: false`; images via the Files API
  (`stagedUploadsCreate` → PUT → `fileCreate`); SEO stored as `global/title_tag`
  and `global/description_tag` metafields.
- **Reused unchanged/adapted:** `scraper.py` (SerpReport, unchanged), `common.py`,
  `config.py`/`config_local.py`, `imagegen.py`, and the `final_check` gate +
  `posted_keywords.json` dedup + pytest/README/`.env` discipline + draft +
  human-in-the-loop.

## Consequences
- Two AI vendors remain (Anthropic for text, Gemini for images); both keys in
  `.env`.
- GraphQL field/mutation names are version-sensitive and **must be verified**
  against the pinned API version before the first real publish (see
  `docs/PUBLISHING.md`). `publisher.py` defaults to a read-only dry run and gates
  all writes behind `--publish`.
- Dropping the WPBakery tree removes a whole class of encoding complexity
  (base64/rawurlencode, vc_* nodes) — the generator's clean HTML maps directly.
- No RankMath score; SEO is deterministic heuristics in `editor.py`
  (title/description length, one H1 via the title, keyword placement, internal
  links, alt text, length).
