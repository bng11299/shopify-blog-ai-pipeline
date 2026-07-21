# CLAUDE.md — Shopify blog pipeline

Project-specific instructions. Global rules in `~/.claude/CLAUDE.md` still apply
(quality bar, security-built-in, memory protocol, PowerShell-first on Windows).

## What this is
A local pipeline that turns a SerpReport keyword into a **draft** Shopify blog
article, with AI-written copy (Claude), AI-generated + QA'd images (Gemini), a
two-layer quality gate, and a human-in-the-loop publish. Ported from the
WordPress "Page Maker" (github.com/bng11299/WordPress-Page-Maker).

## Pipeline (each stage is independently runnable)
`scraper.py` → `generator.py` → `editor.py` → `seo_check.py` → `imagegen.py` →
`final_check.py` → `publisher.py`. `run.py` sequences the local half (everything
before publisher). `seo_check.py` is a deterministic SEO + AEO scorecard
(keyword placement, meta, links, structure; FAQ schema, question headings,
snippet-sized answers). Hard-minimum gate: **SEO >= 97, AEO >= 93** (one SEO
soft-warn allowed, one AEO soft-warn allowed; any hard fail blocks). Floors were
calibrated on a 13-article workflow test of genuine generations: SEO clustered
99.1 (min 97), AEO clustered 93 (a thorough-FAQ-answer warn is common), so 93 is
the realistic AEO floor. Keyword frequency is density-based (0.5-2.5%). Tune via
`MIN_SEO_SCORE` / `MIN_AEO_SCORE` (set 100 for zero tolerance). Data flows through JSON files: `keyword.json` → `post.json` →
`images/manifest.json`.

## Non-negotiables (do not regress these)
- **Draft only.** `publisher.py` creates articles with `isPublished: false`. A
  human reviews and publishes in the Shopify admin. Never auto-publish live.
- **Secrets in `.env`** (gitignored): `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
  `SERPREPORT_VIEW_URL`, `SHOPIFY_STORE`, `SHOPIFY_ADMIN_TOKEN`. Never in code,
  logs, or error messages. `config.py` holds only generic placeholders; real
  site values live in gitignored `config_local.py`.
- **Least privilege.** The Shopify custom app needs only `write_content`,
  `read_content`, `write_files`, `read_files`. Prefer a dev/staging store first.
- **Pin the API version** (`config.SHOPIFY["api_version"]`) and verify GraphQL
  field/mutation names against that version's schema — do not trust names from
  memory (see `docs/PUBLISHING.md`). The GraphQL Admin API is cost-based
  rate-limited; batch sensibly.
- **Structured output by construction.** The article JSON is defined by one
  schema (`ARTICLE_TOOL["input_schema"]`). API mode forces a `tool_use` call
  against it; Claude Code mode ships the same schema in `generation_request.json`
  and validates the response in `do_ingest`. Keep both paths schema-driven.

## Generation modes (auto-selected by `ANTHROPIC_API_KEY`)
- **Claude Code mode (default, no key):** `generator.py --prep` → Claude Code
  writes `generation_response.json` → `generator.py --ingest` → `post.json`.
  `editor.py` runs the deterministic Layer-1 checks + inline fixes, then writes
  `review_request.json` (draft + `REVIEWER_SYSTEM` criteria) and **requires Claude
  Code to perform the Layer-2 editorial review as the editor** (tone, heading
  hierarchy, FAQ usefulness, link placement, CTA) and give a PASS/FAIL before
  moving on. Regeneration on FAIL is driven conversationally.
- **API mode (key set):** forced `tool_use` + two-layer review + auto-regenerate;
  `run.py` autopilots the local half.

## Conventions
- Windows / PowerShell 5.1 (no `&&`, no ternary). Python 3.13. Git Bash for
  POSIX. Node is not installed (not needed — this is pure Python).
- Use the `claude-api` skill before touching `generator.py`/`editor.py`
  (Anthropic SDK usage, model IDs, structured output).
- `secure-software-assurance` defaults apply automatically to `publisher.py`
  (network + token) and the `.env`/config boundary.
- Architecture/threat docs: `docs/THREAT_MODEL.md`, `docs/adr/`. Re-run
  `/graphify` if the stage graph changes.

## Testing
`python -m pytest` runs the offline suite (`tests/test_pipeline.py`). Tests that
need the `anthropic`/`requests` SDKs `importorskip` so the suite runs on a fresh
checkout. Add negative tests at trust boundaries (`.env` precedence, publish
gate blockers, draft-only) when you extend a stage.

## Setup note
`.claude/settings.json` + `.claude/hooks/` here are a minimal guard/audit
starter. Reconcile them with the canonical project-template from the WorkSetup
repo before serious work.
