# Threat sketch — Shopify blog pipeline

One honest paragraph, per the "new project = threat sketch before first feature"
rule. Revisit when the trust boundaries change.

## Assets
The **Shopify Admin API token** (`shpat_…`, store-wide write to content + files)
is the crown jewel; after it, the **Anthropic** and **Gemini** API keys (billable),
the **SerpReport view_key** (read access to the whole keyword project), and the
store's own blog content/reputation.

## Entry points & trust boundaries
This is a local, single-operator CLI — no inbound network surface. Untrusted /
semi-trusted inputs cross three boundaries: (1) **SerpReport HTML** scraped by
Playwright (attacker-influenceable keyword text flows into the Claude prompt);
(2) **LLM output** from Claude/Gemini (HTML body, image bytes) that we then send
to Shopify; (3) **the Shopify Admin API** we authenticate to with the token.
Secrets live only in `.env` (gitignored); `config.py` is generic, real values in
gitignored `config_local.py`.

## Top 2 realistic threats
1. **Secret leakage / accidental commit.** The Admin token or API keys get
   committed, printed in logs, or pasted into an error. *Mitigations:* `.env`
   gitignored, the `.claude/hooks/guard.py` guard blocks editing `.env` and
   staging/committing `.env`/`config_local.py`, the audit hook redacts
   `shpat_`/`sk-ant-` patterns, keys never printed, least-privilege scopes, dev
   store first.
2. **Prompt-injection → unsafe/undesired published content.** A malicious or
   junk keyword (or a jailbroken image prompt) steers Claude to emit content
   that embeds hostile links, off-brand claims, or broken HTML — which then
   reaches a live store. *Mitigations:* the internal-link **allow-list** (model
   may only link real, catalogued store paths; off-list links are a review
   blocker), the two-layer editor gate (deterministic checks + LLM review with
   regenerate-on-fail), and above all **draft-only publishing with a human
   reviewer** — nothing goes live without a person clicking publish in the admin.

## Explicitly out of scope (for now)
Multi-tenant/hosted operation, supply-chain pinning of the AI SDKs, and defence
against a compromised operator machine. If this ever runs unattended (cron), the
draft-only human gate is the control that must be re-examined first.
